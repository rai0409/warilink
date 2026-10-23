from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.domain.money import MAX_ABS_AMOUNT

# JSON の 100.0 や "100" を受け付けない厳密な整数円
Minor = Annotated[int, Field(strict=True, ge=-MAX_ABS_AMOUNT, le=MAX_ABS_AMOUNT)]
PosInt = Annotated[int, Field(strict=True, ge=0, le=1_000_000)]
Id = Annotated[str, StringConstraints(min_length=1, max_length=64)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MemberValue(Strict):
    member_id: Id
    value: Minor  # mode により 重み/数量/basis point/金額


class Payer(Strict):
    member_id: Id
    amount_minor: Minor


class ItemAllocationIn(Strict):
    mode: Literal["equal", "weight", "units", "exact"]
    shares: list[MemberValue] = Field(max_length=100)


class ItemIn(Strict):
    id: Id
    name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)]
    line_minor: Minor
    quantity: Annotated[int, Field(strict=True, ge=1, le=10_000)] = 1
    tax_group: Literal["8", "10"] | None = None
    allocation: ItemAllocationIn | None = None


class AdjustmentRuleIn(Strict):
    type: Literal["proportional", "equal", "members", "exact"] = "proportional"
    scope_item_ids: list[Id] | None = None
    scope_tax_group: Literal["8", "10"] | None = None
    member_ids: list[Id] | None = None
    amounts: list[MemberValue] | None = None


class AdjustmentIn(Strict):
    id: Id
    kind: Literal["tax", "discount", "coupon", "service_charge", "rounding", "other"]
    label: Annotated[str, StringConstraints(strip_whitespace=True, max_length=60)] = ""
    amount_minor: Minor
    rule: AdjustmentRuleIn = Field(default_factory=AdjustmentRuleIn)


class ReceiptIn(Strict):
    receipt_total_minor: Minor
    items: list[ItemIn] = Field(min_length=1, max_length=300)
    adjustments: list[AdjustmentIn] = Field(default_factory=list, max_length=50)


class SplitIn(Strict):
    mode: Literal["equal", "weight", "percentage", "exact", "units", "itemized"]
    shares: list[MemberValue] = Field(default_factory=list, max_length=100)
    receipt: ReceiptIn | None = None


class ExpenseIn(Strict):
    title: Text
    amount_minor: Minor
    occurred_on: date | None = None
    payers: list[Payer] = Field(min_length=1, max_length=100)
    split: SplitIn


class ExpenseCreate(ExpenseIn):
    # クライアント生成の ID。プレビューと登録で同じ端数配分(タイブレーク)になる。
    client_expense_id: str | None = None
    # レシート読み取り結果から作った場合のレシート ID と、確認・訂正にかかった時間(評価用)
    source_receipt_id: str | None = None
    review_elapsed_ms: Annotated[int, Field(strict=True, ge=0, le=86_400_000)] | None = None


class ExpensePreviewIn(ExpenseIn):
    expense_id: str | None = None  # 既存支出の編集プレビュー/新規の client_expense_id


# ---------------------------------------------------------------- 出力


class IssueOut(BaseModel):
    code: str
    message: str
    target: str | None = None
    detail: dict = Field(default_factory=dict)


class AllocationOut(BaseModel):
    id: str  # item / adjustment id
    allocations: dict[str, int]  # member_id -> amount


class BreakdownOut(BaseModel):
    ok: bool
    issues: list[IssueOut]
    shares: dict[str, int]
    item_allocations: list[AllocationOut] = Field(default_factory=list)
    adjustment_allocations: list[AllocationOut] = Field(default_factory=list)
    breakdown_total_minor: int | None = None
    receipt_total_minor: int | None = None


class ExpenseOut(BaseModel):
    id: str
    title: str
    amount_minor: int
    currency: str
    split_mode: str
    occurred_on: date | None
    version: int
    state: str
    payers: dict[str, int]
    shares: dict[str, int]
    created_at: str
    updated_at: str


class ExpenseDetailOut(ExpenseOut):
    spec: dict
    breakdown: dict


class RevisionOut(BaseModel):
    version: int
    change_kind: str
    spec: dict
    result: dict
    changed_by_me: bool
    created_at: str
