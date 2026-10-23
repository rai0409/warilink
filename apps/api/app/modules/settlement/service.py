"""グループ台帳(支出・ロック済み送金)から純残高と精算計画を作る。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.domain.settlement import (
    ExpenseLedgerEntry,
    OptimizationResult,
    Transfer,
    compute_balances,
    naive_transfer_count,
    optimize_transfers,
)
from app.models.tables import (
    Expense,
    ExpensePayer,
    ExpenseShare,
    Group,
    Membership,
    SettlementBatch,
    SettlementTransfer,
)

# 再最適化で変更してはならない送金(送金申告済み・受取確認済み)。
# Phase 5 で bank evidence が matched のものもここに含める。
LOCKED_STATUSES = ("reported_paid", "recipient_confirmed")

LABEL_EXACT = "最小送金回数"
LABEL_REDUCED = "送金回数を削減"
LABEL_NONE = "精算の必要はありません"


@dataclass
class Ledger:
    member_ids: list[str]
    entries: list[ExpenseLedgerEntry]
    locked: list[SettlementTransfer]

    def locked_transfers(self) -> list[Transfer]:
        return [Transfer(str(t.from_member_id), str(t.to_member_id), t.amount_minor) for t in self.locked]


def load_ledger(db: Session, group: Group) -> Ledger:
    members = [str(m) for m in db.execute(
        select(Membership.id).where(Membership.group_id == group.id)).scalars()]
    expenses = list(db.execute(
        select(Expense.id).where(Expense.group_id == group.id, Expense.state == "active")).scalars())
    payers: dict[uuid.UUID, dict[str, int]] = {e: {} for e in expenses}
    shares: dict[uuid.UUID, dict[str, int]] = {e: {} for e in expenses}
    if expenses:
        for p in db.execute(select(ExpensePayer).where(ExpensePayer.expense_id.in_(expenses))).scalars():
            payers[p.expense_id][str(p.member_id)] = p.amount_minor
        for s in db.execute(select(ExpenseShare).where(ExpenseShare.expense_id.in_(expenses))).scalars():
            shares[s.expense_id][str(s.member_id)] = s.amount_minor
    locked = list(db.execute(
        select(SettlementTransfer).where(SettlementTransfer.group_id == group.id,
                                         SettlementTransfer.status.in_(LOCKED_STATUSES))
        .order_by(SettlementTransfer.created_at, SettlementTransfer.id)).scalars())
    entries = [ExpenseLedgerEntry(str(e), payers[e], shares[e]) for e in expenses]
    return Ledger(members, entries, locked)


def balance_rows(ledger: Ledger) -> tuple[dict[str, int], list[dict]]:
    """純残高(ロック済み送金を反映)と、メンバーごとの内訳行。"""
    net = compute_balances(ledger.member_ids, ledger.entries, ledger.locked_transfers())
    paid = dict.fromkeys(ledger.member_ids, 0)
    owed = dict.fromkeys(ledger.member_ids, 0)
    sent = dict.fromkeys(ledger.member_ids, 0)
    received = dict.fromkeys(ledger.member_ids, 0)
    for e in ledger.entries:
        for m, v in e.payers.items():
            paid[m] += v
        for m, v in e.shares.items():
            owed[m] += v
    for t in ledger.locked:
        sent[str(t.from_member_id)] += t.amount_minor
        received[str(t.to_member_id)] += t.amount_minor
    rows = [
        {"member_id": m, "paid_minor": paid[m], "share_minor": owed[m], "settled_out_minor": sent[m],
         "settled_in_minor": received[m], "net_minor": net[m]}
        for m in ledger.member_ids
    ]
    return net, rows


def optimize(net: dict[str, int]) -> OptimizationResult:
    s = get_settings()
    return optimize_transfers(net, exact_max_participants=s.settlement_exact_max_participants,
                              time_limit_ms=s.settlement_time_limit_ms)


def plan_out(ledger_version: int, ledger: Ledger, rows: list[dict], res: OptimizationResult) -> dict:
    if not res.transfers:
        label = LABEL_NONE
    else:
        label = LABEL_EXACT if res.is_exact_minimum else LABEL_REDUCED
    return {
        "ledger_version": ledger_version,
        "currency": "JPY",
        "balances": rows,
        "balance_sum_minor": sum(r["net_minor"] for r in rows),
        "transfers": [{"from_member_id": t.from_member, "to_member_id": t.to_member,
                       "amount_minor": t.amount_minor} for t in res.transfers],
        "optimization": {
            "method": res.method,
            "is_exact_minimum": res.is_exact_minimum,
            "display_label": label,
            "fallback_reason": res.fallback_reason,
            "naive_transfer_count": naive_transfer_count(ledger.entries),
            "optimized_transfer_count": len(res.transfers),
            "minimum_transfer_count": res.minimum_transfer_count,
            "participant_count": res.participant_count,
            "elapsed_ms": res.elapsed_ms,
            "assumptions": res.assumptions,
        },
        "locked_transfers": [transfer_out(t) for t in ledger.locked],
    }


def transfer_out(t: SettlementTransfer) -> dict:
    return {"id": str(t.id), "batch_id": str(t.batch_id), "from_member_id": str(t.from_member_id),
            "to_member_id": str(t.to_member_id), "amount_minor": t.amount_minor, "currency": t.currency,
            "status": t.status, "version": t.version}


def batch_out(db: Session, group: Group, batch: SettlementBatch) -> dict:
    transfers = list(db.execute(select(SettlementTransfer).where(SettlementTransfer.batch_id == batch.id)
                                .order_by(SettlementTransfer.created_at, SettlementTransfer.id)).scalars())
    if not transfers:
        label = LABEL_NONE
    else:
        label = LABEL_EXACT if batch.is_exact_minimum else LABEL_REDUCED
    return {
        "id": str(batch.id),
        "version": batch.version,
        "state": batch.state,
        "ledger_version": batch.ledger_version,
        "is_stale": batch.ledger_version != group.ledger_version,
        "created_at": batch.created_at.isoformat(),
        "balances": batch.balances,
        "optimization": {
            "method": batch.method,
            "is_exact_minimum": batch.is_exact_minimum,
            "display_label": label,
            "fallback_reason": batch.fallback_reason,
            "naive_transfer_count": batch.naive_transfer_count,
            "optimized_transfer_count": len(transfers),
            "minimum_transfer_count": batch.minimum_transfer_count,
            "elapsed_ms": batch.elapsed_ms,
            "assumptions": batch.assumptions,
        },
        "transfers": [transfer_out(t) for t in transfers],
    }
