"""API 入力 → ドメイン仕様への変換と、計算結果の JSON 化。"""

from __future__ import annotations

from collections.abc import Iterable

from app.domain.allocation import (
    Adjustment,
    AdjustmentRule,
    ExpenseResult,
    ExpenseSpec,
    Item,
    ItemAllocation,
    ReceiptSpec,
    compute_expense,
)
from app.domain.money import MoneyError

from .schemas import AllocationOut, BreakdownOut, ExpenseIn, IssueOut, MemberValue


def _to_map(values: Iterable[MemberValue], where: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        if v.member_id in out:
            raise MoneyError("DUPLICATE_MEMBER", "同じメンバーが重複して指定されています", where=where)
        out[v.member_id] = v.value
    return out


def to_spec(body: ExpenseIn) -> ExpenseSpec:
    payers: dict[str, int] = {}
    for p in body.payers:
        if p.member_id in payers:
            raise MoneyError("DUPLICATE_MEMBER", "立替者が重複しています", where="payers")
        payers[p.member_id] = p.amount_minor
    receipt = None
    if body.split.receipt is not None:
        r = body.split.receipt
        receipt = ReceiptSpec(
            items=[
                Item(
                    id=i.id, name=i.name, line_minor=i.line_minor, quantity=i.quantity, tax_group=i.tax_group,
                    allocation=(ItemAllocation(i.allocation.mode, _to_map(i.allocation.shares, f"item:{i.id}"))
                                if i.allocation else None),
                )
                for i in r.items
            ],
            adjustments=[
                Adjustment(
                    id=a.id, kind=a.kind, amount_minor=a.amount_minor, label=a.label,
                    rule=AdjustmentRule(
                        type=a.rule.type,
                        scope_item_ids=tuple(a.rule.scope_item_ids) if a.rule.scope_item_ids is not None else None,
                        scope_tax_group=a.rule.scope_tax_group,
                        member_ids=tuple(a.rule.member_ids) if a.rule.member_ids is not None else None,
                        amounts=_to_map(a.rule.amounts, f"adj:{a.id}") if a.rule.amounts is not None else None,
                    ),
                )
                for a in r.adjustments
            ],
            receipt_total_minor=r.receipt_total_minor,
        )
    return ExpenseSpec(
        amount_minor=body.amount_minor, payers=payers, mode=body.split.mode,
        shares=_to_map(body.split.shares, "shares"), receipt=receipt,
    )


def compute(body: ExpenseIn, seed: str, member_ids: set[str]) -> ExpenseResult:
    return compute_expense(to_spec(body), seed, member_ids)


def breakdown(res: ExpenseResult) -> BreakdownOut:
    issues = list(res.issues)
    out = BreakdownOut(ok=False, issues=[], shares=res.shares)
    if res.receipt is not None:
        rr = res.receipt
        issues = list(rr.issues) + issues
        out.item_allocations = [AllocationOut(id=k, allocations=v) for k, v in rr.item_allocations.items()]
        out.adjustment_allocations = [AllocationOut(id=k, allocations=v)
                                      for k, v in rr.adjustment_allocations.items()]
        out.breakdown_total_minor = rr.breakdown_total_minor
        out.receipt_total_minor = rr.receipt_total_minor
    out.issues = [IssueOut(code=i.code, message=i.message, target=i.target, detail=dict(i.detail)) for i in issues]
    out.ok = not issues
    return out
