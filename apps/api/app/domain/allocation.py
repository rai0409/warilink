"""支出の負担配分エンジン(均等・比率・割合・金額指定・数量・商品別)。

レシート明細(商品別)では:
  - 各商品の行金額を、その商品の割当(均等/比率/数量/金額指定)で整数配分する
  - 税・割引・クーポン・サービス料などの調整額は、明示した配分ルールで整数配分する
  - 確定内訳(商品+調整額)とレシート総額が一致しない場合は**補正せず**問題として返す

`compute_*` はプレビュー用に「問題(issues)」を返し、`require_valid()` は確定用に例外を送出する。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from .money import InvariantViolation, MoneyError, apportion, ensure_int

SplitMode = Literal["equal", "weight", "percentage", "exact", "units", "itemized"]
ItemMode = Literal["equal", "weight", "units", "exact"]
AdjustmentKind = Literal["tax", "discount", "coupon", "service_charge", "rounding", "other"]
AdjustmentRuleType = Literal["proportional", "equal", "members", "exact"]

PERCENT_TOTAL_BP = 10_000  # 割合は basis point (0.01%) の整数で受け取る

# 調整額の種類ごとの符号制約。二重否定(割引を正で入力等)の取り違えを防ぐ。
_SIGN_RULES: dict[str, int] = {"tax": 1, "service_charge": 1, "discount": -1, "coupon": -1}


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    target: str | None = None
    detail: Mapping[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------- 単純な分割


def split_simple(
    total: int, mode: SplitMode, shares: Mapping[str, int], seed: str
) -> dict[str, int]:
    """itemized 以外の分割方式。`shares` の意味は mode による。

    equal: 値は無視(キー=参加者) / weight, units: 正の整数比 / percentage: basis point 合計10000
    exact: 各人の金額(合計 == total 必須)
    """
    ensure_int(total, "total")
    if not shares:
        raise MoneyError("NO_PARTICIPANTS", "負担者を1人以上選んでください")
    for k, v in shares.items():
        ensure_int(v, f"shares[{k}]")

    if mode == "equal":
        return apportion(total, {k: 1 for k in shares}, seed)
    if mode in ("weight", "units"):
        if any(v < 0 for v in shares.values()):
            raise MoneyError("INVALID_WEIGHT", "比率・数量は0以上で指定してください")
        return apportion(total, dict(shares), seed)
    if mode == "percentage":
        if any(v < 0 for v in shares.values()):
            raise MoneyError("INVALID_PERCENTAGE", "割合は0以上で指定してください")
        if sum(shares.values()) != PERCENT_TOTAL_BP:
            raise MoneyError(
                "PERCENTAGE_SUM_MISMATCH",
                "割合の合計が100%になっていません",
                sum_bp=sum(shares.values()),
            )
        return apportion(total, dict(shares), seed)
    if mode == "exact":
        s = sum(shares.values())
        if s != total:
            raise MoneyError(
                "SHARE_SUM_MISMATCH", "指定額の合計が支出額と一致しません", expected=total, actual=s
            )
        return dict(shares)
    raise MoneyError("INVALID_SPLIT_MODE", f"未対応の分割方式です: {mode}")


# ---------------------------------------------------------------- レシート明細


@dataclass(frozen=True)
class ItemAllocation:
    mode: ItemMode
    # equal: {member: 任意値(1)}, weight: 比率, units: 個数, exact: 金額
    shares: Mapping[str, int]


@dataclass(frozen=True)
class Item:
    id: str
    name: str
    line_minor: int  # 行金額(返品は負)
    quantity: int = 1
    tax_group: str | None = None  # 外税の税率グループ ("8", "10") など。内税なら None 可
    allocation: ItemAllocation | None = None


@dataclass(frozen=True)
class AdjustmentRule:
    type: AdjustmentRuleType = "proportional"
    scope_item_ids: tuple[str, ...] | None = None
    scope_tax_group: str | None = None
    member_ids: tuple[str, ...] | None = None
    amounts: Mapping[str, int] | None = None


@dataclass(frozen=True)
class Adjustment:
    id: str
    kind: AdjustmentKind
    amount_minor: int
    label: str = ""
    rule: AdjustmentRule = field(default_factory=AdjustmentRule)


@dataclass(frozen=True)
class ReceiptSpec:
    items: Sequence[Item]
    adjustments: Sequence[Adjustment]
    receipt_total_minor: int


@dataclass
class ReceiptResult:
    item_allocations: dict[str, dict[str, int]]
    adjustment_allocations: dict[str, dict[str, int]]
    member_totals: dict[str, int]
    member_item_totals: dict[str, int]
    breakdown_total_minor: int
    receipt_total_minor: int
    issues: list[Issue]

    @property
    def ok(self) -> bool:
        return not self.issues

    def require_valid(self) -> None:
        if self.issues:
            first = self.issues[0]
            raise MoneyError(
                first.code,
                first.message,
                issues=[{"code": i.code, "message": i.message, "target": i.target} for i in self.issues],
            )


def _allocate_item(item: Item, seed: str) -> dict[str, int]:
    alloc = item.allocation
    assert alloc is not None
    if not alloc.shares:
        raise MoneyError("ITEM_UNALLOCATED", "負担者が選ばれていません")
    if alloc.mode == "units":
        units = sum(alloc.shares.values())
        if any(v < 0 for v in alloc.shares.values()):
            raise MoneyError("INVALID_UNITS", "数量は0以上で指定してください")
        if units != item.quantity:
            raise MoneyError(
                "UNITS_MISMATCH",
                f"割り当てた数量({units})が商品数量({item.quantity})と一致しません",
                expected=item.quantity,
                actual=units,
            )
    return split_simple(item.line_minor, alloc.mode, alloc.shares, seed)


def _scope_items(spec: ReceiptSpec, rule: AdjustmentRule) -> list[Item]:
    if rule.scope_item_ids is not None:
        wanted = set(rule.scope_item_ids)
        return [i for i in spec.items if i.id in wanted]
    if rule.scope_tax_group is not None:
        return [i for i in spec.items if i.tax_group == rule.scope_tax_group]
    return list(spec.items)


def _allocate_adjustment(
    adj: Adjustment, spec: ReceiptSpec, item_allocs: Mapping[str, Mapping[str, int]], seed: str
) -> dict[str, int]:
    rule = adj.rule
    if rule.type == "exact":
        return split_simple(adj.amount_minor, "exact", dict(rule.amounts or {}), seed)
    if rule.type == "members":
        if not rule.member_ids:
            raise MoneyError("ADJUSTMENT_RULE_INVALID", "配分先のメンバーを選んでください")
        return apportion(adj.amount_minor, {m: 1 for m in rule.member_ids}, seed)

    scope = _scope_items(spec, rule)
    if not scope:
        raise MoneyError("ADJUSTMENT_RULE_INVALID", "配分対象の商品がありません")
    missing = [i.id for i in scope if i.id not in item_allocs]
    if missing:
        raise MoneyError(
            "ADJUSTMENT_DEPENDS_ON_UNALLOCATED", "対象商品に未割当があるため計算できません", items=missing
        )
    bases: dict[str, int] = {}
    for it in scope:
        for m, amt in item_allocs[it.id].items():
            bases[m] = bases.get(m, 0) + amt
    if rule.type == "equal":
        members = [m for m, b in bases.items() if b != 0] or list(bases)
        return apportion(adj.amount_minor, {m: 1 for m in members}, seed)
    # proportional: 対象商品の負担額に比例。負の基準額(返品超過)は比例配分できないため明示ルールを要求する。
    if any(b < 0 for b in bases.values()) or sum(bases.values()) <= 0:
        raise MoneyError(
            "ADJUSTMENT_RULE_INVALID",
            "返品等で対象商品の金額が負になるため、比例配分できません。均等・メンバー指定・金額指定を選んでください",
        )
    return apportion(adj.amount_minor, bases, seed)


def compute_receipt(spec: ReceiptSpec, seed: str) -> ReceiptResult:
    issues: list[Issue] = []
    ensure_int(spec.receipt_total_minor, "receipt_total_minor")

    seen: set[str] = set()
    for it in spec.items:
        ensure_int(it.line_minor, f"items[{it.id}].line_minor")
        if it.id in seen:
            issues.append(Issue("DUPLICATE_ID", "商品IDが重複しています", it.id))
        seen.add(it.id)
        if isinstance(it.quantity, bool) or not isinstance(it.quantity, int) or it.quantity < 1:
            issues.append(Issue("INVALID_QUANTITY", "数量は1以上の整数で入力してください", it.id))
    for adj in spec.adjustments:
        ensure_int(adj.amount_minor, f"adjustments[{adj.id}].amount_minor")
        if adj.id in seen:
            issues.append(Issue("DUPLICATE_ID", "IDが重複しています", adj.id))
        seen.add(adj.id)
        want = _SIGN_RULES.get(adj.kind)
        if want is not None and adj.amount_minor * want < 0:
            issues.append(
                Issue(
                    "ADJUSTMENT_SIGN_INVALID",
                    "税・サービス料は0以上、割引・クーポンは0以下で入力してください",
                    adj.id,
                )
            )

    breakdown = sum(i.line_minor for i in spec.items) + sum(a.amount_minor for a in spec.adjustments)
    if breakdown != spec.receipt_total_minor:
        # 勝手に補正しない。差額をユーザーへ提示し、訂正または明示的な調整行の追加を求める。
        issues.append(
            Issue(
                "RECEIPT_TOTAL_MISMATCH",
                "明細と調整額の合計がレシートの合計金額と一致しません",
                None,
                {"breakdown_total": breakdown, "receipt_total": spec.receipt_total_minor,
                 "difference": spec.receipt_total_minor - breakdown},
            )
        )

    item_allocs: dict[str, dict[str, int]] = {}
    for it in spec.items:
        if it.allocation is None or not it.allocation.shares:
            issues.append(Issue("ITEM_UNALLOCATED", "負担者が選ばれていない商品があります", it.id))
            continue
        try:
            item_allocs[it.id] = _allocate_item(it, f"{seed}:item:{it.id}")
        except MoneyError as e:
            issues.append(Issue(e.code, e.message, it.id, e.detail))

    adj_allocs: dict[str, dict[str, int]] = {}
    for adj in spec.adjustments:
        try:
            adj_allocs[adj.id] = _allocate_adjustment(adj, spec, item_allocs, f"{seed}:adj:{adj.id}")
        except MoneyError as e:
            issues.append(Issue(e.code, e.message, adj.id, e.detail))

    member_item_totals: dict[str, int] = {}
    member_totals: dict[str, int] = {}
    for alloc in item_allocs.values():
        for m, v in alloc.items():
            member_item_totals[m] = member_item_totals.get(m, 0) + v
            member_totals[m] = member_totals.get(m, 0) + v
    for alloc in adj_allocs.values():
        for m, v in alloc.items():
            member_totals[m] = member_totals.get(m, 0) + v

    result = ReceiptResult(
        item_allocations=item_allocs,
        adjustment_allocations=adj_allocs,
        member_totals=member_totals,
        member_item_totals=member_item_totals,
        breakdown_total_minor=breakdown,
        receipt_total_minor=spec.receipt_total_minor,
        issues=issues,
    )
    if not issues:
        verify_receipt(spec, result)
    return result


def verify_receipt(spec: ReceiptSpec, result: ReceiptResult) -> None:
    """確定前の不変条件検証。1つでも破れたら InvariantViolation。"""
    for it in spec.items:
        s = sum(result.item_allocations[it.id].values())
        if s != it.line_minor:
            raise InvariantViolation("商品の割当合計が商品金額と一致しません", item=it.id)
    for adj in spec.adjustments:
        s = sum(result.adjustment_allocations[adj.id].values())
        if s != adj.amount_minor:
            raise InvariantViolation("調整額の配分合計が一致しません", adjustment=adj.id)
    if result.breakdown_total_minor != spec.receipt_total_minor:
        raise InvariantViolation("内訳合計がレシート総額と一致しません")
    if sum(result.member_totals.values()) != spec.receipt_total_minor:
        raise InvariantViolation("負担額合計がレシート総額と一致しません")


# ---------------------------------------------------------------- 支出全体


@dataclass(frozen=True)
class ExpenseSpec:
    amount_minor: int
    payers: Mapping[str, int]  # 立替額(複数立替可)
    mode: SplitMode
    shares: Mapping[str, int] = field(default_factory=dict)
    receipt: ReceiptSpec | None = None


@dataclass
class ExpenseResult:
    shares: dict[str, int]
    receipt: ReceiptResult | None
    issues: list[Issue]

    @property
    def ok(self) -> bool:
        return not self.issues

    def require_valid(self) -> None:
        if self.receipt is not None and self.receipt.issues:
            self.receipt.require_valid()
        if self.issues:
            i = self.issues[0]
            raise MoneyError(i.code, i.message, **dict(i.detail))


def compute_expense(spec: ExpenseSpec, seed: str, members: set[str]) -> ExpenseResult:
    """支出の負担額を計算する。`members` はグループの有効なメンバーID集合。"""
    issues: list[Issue] = []
    ensure_int(spec.amount_minor, "amount_minor")

    referenced = set(spec.payers) | set(spec.shares)
    if spec.receipt is not None:
        for it in spec.receipt.items:
            if it.allocation:
                referenced |= set(it.allocation.shares)
        for a in spec.receipt.adjustments:
            referenced |= set(a.rule.member_ids or ()) | set(a.rule.amounts or {})
    unknown = sorted(referenced - members)
    if unknown:
        raise MoneyError("UNKNOWN_MEMBER", "グループに存在しないメンバーが指定されています", members=unknown)

    if not spec.payers:
        issues.append(Issue("NO_PAYER", "立て替えた人を選んでください"))
    for k, v in spec.payers.items():
        ensure_int(v, f"payers[{k}]")
    paid = sum(spec.payers.values())
    if spec.payers and paid != spec.amount_minor:
        issues.append(
            Issue("PAYER_SUM_MISMATCH", "立替額の合計が支出額と一致しません", None,
                  {"expected": spec.amount_minor, "actual": paid})
        )

    receipt_result: ReceiptResult | None = None
    shares: dict[str, int] = {}
    if spec.mode == "itemized":
        if spec.receipt is None:
            raise MoneyError("RECEIPT_REQUIRED", "商品別の割り勘には明細が必要です")
        if spec.receipt.receipt_total_minor != spec.amount_minor:
            issues.append(Issue("RECEIPT_TOTAL_MISMATCH", "支出額とレシート合計が一致しません"))
        receipt_result = compute_receipt(spec.receipt, seed)
        shares = dict(receipt_result.member_totals)
    else:
        try:
            shares = split_simple(spec.amount_minor, spec.mode, spec.shares, seed)
        except MoneyError as e:
            issues.append(Issue(e.code, e.message, None, e.detail))

    shares = {m: v for m, v in shares.items() if v != 0}
    result = ExpenseResult(shares=shares, receipt=receipt_result, issues=issues)
    if result.ok and (receipt_result is None or receipt_result.ok):
        if sum(shares.values()) != spec.amount_minor:
            raise InvariantViolation("負担額合計が支出額と一致しません")
    return result
