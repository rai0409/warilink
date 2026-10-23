from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.core.security import parse_uuid
from app.models.tables import Group, Identity, OcrCorrection, OcrRun, Receipt
from app.ocr.corrections import diff_draft

if TYPE_CHECKING:
    from app.modules.expenses.schemas import ExpenseCreate


def link_receipt(db: Session, group: Group, body: ExpenseCreate, expense_id: uuid.UUID, ident: Identity) -> None:
    """読み取り結果から登録した支出をレシートに紐付け、読み取り結果との差分を記録する。"""
    assert body.source_receipt_id
    r = db.execute(select(Receipt).where(Receipt.id == parse_uuid(body.source_receipt_id))
                   .with_for_update()).scalar_one_or_none()
    if r is None or r.group_id != group.id:
        raise ApiError(422, "RECEIPT_NOT_FOUND", "読み取ったレシートが見つかりません")
    if r.state != "draft_ready":
        raise ApiError(409, "RECEIPT_ALREADY_USED", "このレシートは既に登録済みか、読み取りが完了していません")
    r.state, r.expense_id, r.review_elapsed_ms = "confirmed", expense_id, body.review_elapsed_ms
    run = db.execute(select(OcrRun).where(OcrRun.receipt_id == r.id, OcrRun.error_code.is_(None))
                     .order_by(OcrRun.created_at.desc()).limit(1)).scalar_one_or_none()
    if run is None or run.draft is None or body.split.receipt is None:
        return
    spec = body.split.receipt.model_dump()
    for d in diff_draft(run.draft, spec):
        db.add(OcrCorrection(receipt_id=r.id, run_id=run.id, corrected_by=ident.id, label_status="proposed", **d))
