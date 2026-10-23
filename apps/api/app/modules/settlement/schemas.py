from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from app.modules.expenses.schemas import BreakdownOut


class BalanceRowOut(BaseModel):
    member_id: str
    paid_minor: int
    share_minor: int
    settled_out_minor: int
    settled_in_minor: int
    net_minor: int


class BalancesOut(BaseModel):
    ledger_version: int
    currency: str
    balances: list[BalanceRowOut]
    balance_sum_minor: int


class AssumptionsOut(BaseModel):
    currency: str
    integer_minor_units: bool
    any_member_can_pay_any_member: bool
    transfer_fees_considered: bool
    per_transfer_limit_minor: int | None
    locked_transfers_preserved: bool


class OptimizationOut(BaseModel):
    method: Literal["exact_subset_dp", "greedy_fallback", "none"]
    is_exact_minimum: bool
    display_label: str
    fallback_reason: Literal["participant_limit", "time_limit"] | None
    naive_transfer_count: int
    optimized_transfer_count: int
    minimum_transfer_count: int | None
    participant_count: int | None = None
    elapsed_ms: float
    assumptions: AssumptionsOut


class PlannedTransferOut(BaseModel):
    from_member_id: str
    to_member_id: str
    amount_minor: int


class TransferOut(BaseModel):
    id: str
    batch_id: str
    from_member_id: str
    to_member_id: str
    amount_minor: int
    currency: str
    status: Literal["unpaid", "reported_paid", "recipient_confirmed", "superseded"]
    version: int


class TransferDetailOut(TransferOut):
    group_public_id: str
    from_name: str
    to_name: str
    my_role: Literal["payer", "payee"]
    bank_evidence_state: str


class SettlementPlanOut(BaseModel):
    ledger_version: int
    currency: str
    balances: list[BalanceRowOut]
    balance_sum_minor: int
    transfers: list[PlannedTransferOut]
    optimization: OptimizationOut
    locked_transfers: list[TransferOut]


class BatchOut(BaseModel):
    id: str
    version: int
    state: Literal["active", "superseded"]
    ledger_version: int
    is_stale: bool
    created_at: str
    balances: dict[str, int]
    optimization: OptimizationOut
    transfers: list[TransferOut]


class ExpensePreviewOut(BaseModel):
    breakdown: BreakdownOut
    projected_settlement: SettlementPlanOut | None
