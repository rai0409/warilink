"""純残高の算出と送金回数の最適化。

## 最適化の前提(API の `assumptions` として返す)
- 誰から誰へでも送金できる(送金先の制限なし)
- 単一通貨(JPY)・整数円。通貨換算はしない
- 送金手数料は考慮しない(手数料は精算額に含めない)
- 1件あたりの送金上限額はない
- 確定済み(送金申告済み・受取確認済み・照合済み)の送金は固定し、残りの残高だけを最適化する

## 厳密解法
非ゼロ残高の人数を n、全体を「合計 0 の部分集合」に分割したときの最大個数を k とすると、
最小送金件数は n - k である(各部分集合 S は |S|-1 件で解消でき、逆に任意の送金計画の
連結成分は合計 0 の部分集合をなすため)。k を部分集合 DP (O(n·2^n)) で求める。
DP の前に「+a と -a の組」を独立した部分集合として先に取り出す(常に最適解に含められる)。

n または計算時間が上限を超えた場合は貪欲法(最大債務者→最大債権者)にフォールバックする。
貪欲法は必ず残高を 0 にするが、最小件数は保証しない(`is_exact_minimum=False`)。
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .money import InvariantViolation, MoneyError, apportion, ensure_int

DEFAULT_EXACT_MAX_PARTICIPANTS = 20
DEFAULT_TIME_LIMIT_MS = 1500

ASSUMPTIONS: dict[str, object] = {
    "currency": "JPY",
    "integer_minor_units": True,
    "any_member_can_pay_any_member": True,
    "transfer_fees_considered": False,
    "per_transfer_limit_minor": None,
    "locked_transfers_preserved": True,
}


@dataclass(frozen=True)
class Transfer:
    from_member: str
    to_member: str
    amount_minor: int


@dataclass(frozen=True)
class ExpenseLedgerEntry:
    expense_id: str
    payers: Mapping[str, int]
    shares: Mapping[str, int]


@dataclass
class OptimizationResult:
    transfers: list[Transfer]
    method: str  # "exact_subset_dp" | "greedy_fallback" | "none"
    is_exact_minimum: bool
    fallback_reason: str | None
    participant_count: int  # 非ゼロ残高の人数
    minimum_transfer_count: int | None  # 厳密解のときのみ確定値
    elapsed_ms: float
    assumptions: dict[str, object] = field(default_factory=lambda: dict(ASSUMPTIONS))


# ---------------------------------------------------------------- 残高


def compute_balances(
    members: Iterable[str],
    expenses: Iterable[ExpenseLedgerEntry],
    applied_transfers: Iterable[Transfer] = (),
) -> dict[str, int]:
    """純残高 = 立替額 - 負担額 + 送金済み額 - 受取済み額。正なら受け取る側。"""
    bal = {m: 0 for m in members}
    for e in expenses:
        if sum(e.payers.values()) != sum(e.shares.values()):
            raise InvariantViolation("支出の立替合計と負担合計が一致しません", expense=e.expense_id)
        for m, v in e.payers.items():
            bal[m] = bal.get(m, 0) + ensure_int(v, "paid")
        for m, v in e.shares.items():
            bal[m] = bal.get(m, 0) - ensure_int(v, "share")
    for t in applied_transfers:
        bal[t.from_member] = bal.get(t.from_member, 0) + t.amount_minor
        bal[t.to_member] = bal.get(t.to_member, 0) - t.amount_minor
    if sum(bal.values()) != 0:
        raise InvariantViolation("純残高の総和が0になりません", total=sum(bal.values()))
    return bal


def naive_transfer_count(expenses: Iterable[ExpenseLedgerEntry]) -> int:
    """最適化前の送金件数: 各支出の負担者→立替者の債務を、2者間だけで相殺した件数。"""
    pair: dict[tuple[str, str], int] = {}
    for e in expenses:
        payer_weights = {p: abs(v) for p, v in e.payers.items() if v != 0}
        if not payer_weights:
            continue
        for m, share in e.shares.items():
            if share == 0:
                continue
            parts = apportion(share, payer_weights, f"naive:{e.expense_id}:{m}")
            for p, part in parts.items():
                if p == m or part == 0:
                    continue
                a, b = sorted((m, p))
                # (a,b) 方向の正 = a が b へ払う
                pair[(a, b)] = pair.get((a, b), 0) + (part if a == m else -part)
    return sum(1 for v in pair.values() if v != 0)


# ---------------------------------------------------------------- 検証


def verify_transfers(balances: Mapping[str, int], transfers: Iterable[Transfer]) -> None:
    rest = dict(balances)
    for t in transfers:
        if isinstance(t.amount_minor, bool) or not isinstance(t.amount_minor, int) or t.amount_minor <= 0:
            raise InvariantViolation("送金額は正の整数でなければなりません")
        if t.from_member == t.to_member:
            raise InvariantViolation("自分自身への送金があります")
        if t.from_member not in rest or t.to_member not in rest:
            raise InvariantViolation("未知のメンバーへの送金があります")
        rest[t.from_member] += t.amount_minor
        rest[t.to_member] -= t.amount_minor
    nonzero = {m: v for m, v in rest.items() if v != 0}
    if nonzero:
        raise InvariantViolation("送金後に残高が0になりません", remaining=nonzero)


# ---------------------------------------------------------------- アルゴリズム


def _settle_group(members: list[tuple[str, int]]) -> list[Transfer]:
    """合計 0 のグループを |group|-1 件以下で解消する(2ポインタ)。"""
    debtors = sorted(((m, -v) for m, v in members if v < 0), key=lambda x: (-x[1], x[0]))
    creditors = sorted(((m, v) for m, v in members if v > 0), key=lambda x: (-x[1], x[0]))
    d_names = [m for m, _ in debtors]
    d_left = [v for _, v in debtors]
    c_names = [m for m, _ in creditors]
    c_left = [v for _, v in creditors]
    out: list[Transfer] = []
    i = j = 0
    while i < len(d_left) and j < len(c_left):
        amt = min(d_left[i], c_left[j])
        out.append(Transfer(d_names[i], c_names[j], amt))
        d_left[i] -= amt
        c_left[j] -= amt
        if d_left[i] == 0:
            i += 1
        if c_left[j] == 0:
            j += 1
    return out


def greedy_transfers(balances: Mapping[str, int]) -> list[Transfer]:
    """最大債務者から最大債権者へ。残高は必ず0になるが件数最小は保証しない。"""
    import heapq

    # min-heap に (-絶対額, member) を積み、絶対額の大きい順・同額は member 順に取り出す
    dh = [(v, m) for m, v in balances.items() if v < 0]
    ch = [(-v, m) for m, v in balances.items() if v > 0]
    heapq.heapify(dh)
    heapq.heapify(ch)
    out: list[Transfer] = []
    while dh and ch:
        da, dm = heapq.heappop(dh)
        ca, cm = heapq.heappop(ch)
        da, ca = -da, -ca
        amt = min(da, ca)
        out.append(Transfer(dm, cm, amt))
        if da > amt:
            heapq.heappush(dh, (-(da - amt), dm))
        if ca > amt:
            heapq.heappush(ch, (-(ca - amt), cm))
    return out


class _Timeout(Exception):
    pass


def _max_zero_sum_partition(values: list[int], deadline: float) -> list[list[int]]:
    """values(合計0)を合計0の部分集合へ最大個数で分割し、インデックスのグループを返す。"""
    n = len(values)
    full = (1 << n) - 1
    sums = [0] * (1 << n)
    for mask in range(1, 1 << n):
        low = mask & -mask
        sums[mask] = sums[mask ^ low] + values[low.bit_length() - 1]
    dp = [0] * (1 << n)
    for mask in range(1, 1 << n):
        if (mask & 0x3FFF) == 0 and time.perf_counter() > deadline:
            raise _Timeout
        best = 0
        m = mask
        while m:
            low = m & -m
            v = dp[mask ^ low]
            if v > best:
                best = v
            m ^= low
        dp[mask] = best + (1 if sums[mask] == 0 else 0)

    # 復元: 1要素ずつ除去する経路を辿り、逆順の接頭辞和が0になる位置で区切る
    order: list[int] = []
    mask = full
    while mask:
        target = dp[mask] - (1 if sums[mask] == 0 else 0)
        m = mask
        while m:
            low = m & -m
            if dp[mask ^ low] == target:
                order.append(low.bit_length() - 1)
                mask ^= low
                break
            m ^= low
        else:  # pragma: no cover - DP の定義上到達しない
            raise InvariantViolation("最適化の復元に失敗しました")
    order.reverse()
    groups: list[list[int]] = []
    cur: list[int] = []
    acc = 0
    for idx in order:
        cur.append(idx)
        acc += values[idx]
        if acc == 0:
            groups.append(cur)
            cur = []
    if cur or len(groups) != dp[full]:
        raise InvariantViolation("最適化の復元結果が不正です")
    return groups


def optimize_transfers(
    balances: Mapping[str, int],
    *,
    exact_max_participants: int = DEFAULT_EXACT_MAX_PARTICIPANTS,
    time_limit_ms: int = DEFAULT_TIME_LIMIT_MS,
) -> OptimizationResult:
    start = time.perf_counter()
    for m, v in balances.items():
        ensure_int(v, f"balance[{m}]")
    if sum(balances.values()) != 0:
        raise MoneyError("BALANCE_SUM_NONZERO", "純残高の総和が0ではありません")

    nonzero = sorted(((m, v) for m, v in balances.items() if v != 0), key=lambda x: x[0])
    n = len(nonzero)

    def finish(transfers: list[Transfer], method: str, exact: bool, reason: str | None, lb: int | None):
        verify_transfers(balances, transfers)
        if exact and len(transfers) != lb:
            raise InvariantViolation("厳密解の件数が理論値と一致しません")
        return OptimizationResult(
            transfers=transfers,
            method=method,
            is_exact_minimum=exact,
            fallback_reason=reason,
            participant_count=n,
            minimum_transfer_count=lb,
            elapsed_ms=round((time.perf_counter() - start) * 1000, 3),
        )

    if n == 0:
        return finish([], "none", True, None, 0)

    # 前処理: +a / -a の組を先に独立グループとして確定(最適性を損なわない)
    by_amount: dict[int, list[str]] = {}
    for m, v in nonzero:
        by_amount.setdefault(v, []).append(m)
    pair_groups: list[list[tuple[str, int]]] = []
    used: set[str] = set()
    for v in sorted(a for a in by_amount if a > 0):
        pos, neg = by_amount.get(v, []), by_amount.get(-v, [])
        for p, q in zip(pos, neg, strict=False):
            pair_groups.append([(q, -v), (p, v)])
            used.update((p, q))
    rest = [(m, v) for m, v in nonzero if m not in used]

    if len(rest) > exact_max_participants:
        return finish(
            greedy_transfers(balances), "greedy_fallback", False, "participant_limit", None
        )

    deadline = start + time_limit_ms / 1000
    try:
        idx_groups = _max_zero_sum_partition([v for _, v in rest], deadline) if rest else []
    except _Timeout:
        return finish(greedy_transfers(balances), "greedy_fallback", False, "time_limit", None)

    groups = pair_groups + [[rest[i] for i in g] for g in idx_groups]
    transfers: list[Transfer] = []
    for g in groups:
        transfers.extend(_settle_group(g))
    return finish(transfers, "exact_subset_dp", True, None, n - len(groups))
