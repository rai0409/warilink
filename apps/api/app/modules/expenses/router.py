from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Request
from fastapi.encoders import jsonable_encoder
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import ApiError, not_found
from app.core.idempotency import run_idempotent
from app.core.security import current_identity, load_group_access, parse_uuid
from app.domain.settlement import ExpenseLedgerEntry
from app.models.tables import (
    AuditEvent,
    Expense,
    ExpensePayer,
    ExpenseRevision,
    ExpenseShare,
    Group,
    Identity,
    Membership,
)
from app.modules.receipts.service import link_receipt
from app.modules.settlement import service as settle
from app.modules.settlement.schemas import ExpensePreviewOut

from . import service
from .schemas import (
    ExpenseCreate,
    ExpenseDetailOut,
    ExpenseIn,
    ExpenseOut,
    ExpensePreviewIn,
    RevisionOut,
)

router = APIRouter(prefix="/groups/{public_id}/expenses", tags=["expenses"])


def _member_ids(db: Session, group: Group) -> set[str]:
    return {str(m) for m in db.execute(select(Membership.id).where(
        Membership.group_id == group.id, Membership.status == "active")).scalars()}


def _expense_out(db: Session, e: Expense) -> ExpenseOut:
    payers = {str(p.member_id): p.amount_minor for p in db.execute(
        select(ExpensePayer).where(ExpensePayer.expense_id == e.id)).scalars()}
    shares = {str(s.member_id): s.amount_minor for s in db.execute(
        select(ExpenseShare).where(ExpenseShare.expense_id == e.id)).scalars()}
    return ExpenseOut(
        id=str(e.id), title=e.title, amount_minor=e.amount_minor, currency=e.currency,
        split_mode=e.split_mode, occurred_on=e.occurred_on, version=e.version, state=e.state,
        payers=payers, shares=shares, created_at=e.created_at.isoformat(), updated_at=e.updated_at.isoformat(),
    )


def _parse_if_match(if_match: str | None) -> int:
    if if_match is None:
        raise ApiError(428, "PRECONDITION_REQUIRED", "更新には版番号(If-Match)が必要です")
    try:
        return int(if_match.strip().strip('"').removeprefix("W/").strip('"'))
    except ValueError as e:
        raise ApiError(400, "INVALID_IF_MATCH", "版番号の形式が正しくありません") from e


def _write_rows(db: Session, e: Expense, payers: dict[str, int], shares: dict[str, int]) -> None:
    db.execute(delete(ExpensePayer).where(ExpensePayer.expense_id == e.id))
    db.execute(delete(ExpenseShare).where(ExpenseShare.expense_id == e.id))
    for m, v in payers.items():
        if v != 0:
            db.add(ExpensePayer(expense_id=e.id, member_id=uuid.UUID(m), amount_minor=v))
    for m, v in shares.items():
        if v != 0:
            db.add(ExpenseShare(expense_id=e.id, member_id=uuid.UUID(m), amount_minor=v))


def _load_expense(db: Session, group: Group, expense_id: str, *, for_update: bool = False) -> Expense:
    q = select(Expense).where(Expense.id == parse_uuid(expense_id), Expense.group_id == group.id)
    if for_update:
        q = q.with_for_update()
    e = db.execute(q).scalar_one_or_none()
    if e is None:
        raise not_found()
    return e


def _spec_json(body: ExpenseIn) -> dict:
    return jsonable_encoder(body.model_dump(exclude={"client_expense_id", "expense_id", "source_receipt_id",
                                                     "review_elapsed_ms"}))


@router.get("", response_model=list[ExpenseOut])
def list_expenses(public_id: str, db: Session = Depends(get_db), ident: Identity = Depends(current_identity)):
    acc = load_group_access(db, public_id, ident)
    rows = db.execute(select(Expense).where(Expense.group_id == acc.group.id, Expense.state == "active")
                      .order_by(Expense.occurred_on.desc().nulls_last(), Expense.created_at.desc())).scalars()
    return [_expense_out(db, e) for e in rows]


@router.post("/preview", response_model=ExpensePreviewOut)
def preview_expense(public_id: str, body: ExpensePreviewIn, db: Session = Depends(get_db),
                    ident: Identity = Depends(current_identity)) -> dict:
    """保存せずに負担額と、反映後の精算候補を計算する(入力のたびに呼ぶ想定)。"""
    acc = load_group_access(db, public_id, ident)
    seed = body.expense_id or "preview"
    res = service.compute(body, seed, _member_ids(db, acc.group))
    out: dict = {"breakdown": service.breakdown(res).model_dump(), "projected_settlement": None}
    if out["breakdown"]["ok"]:
        ledger = settle.load_ledger(db, acc.group)
        ledger.entries = [e for e in ledger.entries if e.expense_id != body.expense_id]
        payers = {p.member_id: p.amount_minor for p in body.payers}
        ledger.entries.append(ExpenseLedgerEntry(seed, payers, res.shares))
        net, rows = settle.balance_rows(ledger)
        out["projected_settlement"] = settle.plan_out(acc.group.ledger_version, ledger, rows, settle.optimize(net))
    return out


@router.post("", status_code=201, response_model=ExpenseDetailOut)
def create_expense(public_id: str, body: ExpenseCreate, request: Request, db: Session = Depends(get_db),
                   ident: Identity = Depends(current_identity)):
    def do():
        acc = load_group_access(db, public_id, ident, for_update=True)
        eid = parse_uuid(body.client_expense_id) if body.client_expense_id else uuid.uuid4()
        if db.get(Expense, eid) is not None:
            raise ApiError(409, "EXPENSE_EXISTS", "この支出は既に登録されています")
        res = service.compute(body, str(eid), _member_ids(db, acc.group))
        res.require_valid()  # 不一致・未割当があれば確定しない
        bd = service.breakdown(res).model_dump()
        e = Expense(id=eid, group_id=acc.group.id, title=body.title, amount_minor=body.amount_minor,
                    split_mode=body.split.mode, occurred_on=body.occurred_on, created_by=ident.id, version=1)
        db.add(e)
        db.flush()
        _write_rows(db, e, {p.member_id: p.amount_minor for p in body.payers}, res.shares)
        db.add(ExpenseRevision(expense_id=e.id, version=1, change_kind="create", spec=_spec_json(body),
                               result=bd, changed_by=ident.id))
        if body.source_receipt_id:
            link_receipt(db, acc.group, body, e.id, ident)
        acc.group.ledger_version += 1
        db.add(AuditEvent(actor_identity_id=ident.id, action="expense.create", target_type="expense",
                          target_id=str(e.id)))
        db.flush()
        db.refresh(e)
        return ExpenseDetailOut(**_expense_out(db, e).model_dump(), spec=_spec_json(body), breakdown=bd)

    return run_idempotent(db, request, ident, f"expense.create:{public_id}", body, do, status_code=201)


@router.get("/{expense_id}", response_model=ExpenseDetailOut)
def get_expense(public_id: str, expense_id: str, db: Session = Depends(get_db),
                ident: Identity = Depends(current_identity)):
    acc = load_group_access(db, public_id, ident)
    e = _load_expense(db, acc.group, expense_id)
    rev = db.execute(select(ExpenseRevision).where(ExpenseRevision.expense_id == e.id)
                     .order_by(ExpenseRevision.version.desc()).limit(1)).scalar_one()
    return ExpenseDetailOut(**_expense_out(db, e).model_dump(), spec=rev.spec, breakdown=rev.result)


@router.get("/{expense_id}/revisions", response_model=list[RevisionOut])
def list_revisions(public_id: str, expense_id: str, db: Session = Depends(get_db),
                   ident: Identity = Depends(current_identity)):
    acc = load_group_access(db, public_id, ident)
    e = _load_expense(db, acc.group, expense_id)
    revs = db.execute(select(ExpenseRevision).where(ExpenseRevision.expense_id == e.id)
                      .order_by(ExpenseRevision.version)).scalars()
    return [RevisionOut(version=r.version, change_kind=r.change_kind, spec=r.spec, result=r.result,
                        changed_by_me=r.changed_by == ident.id, created_at=r.created_at.isoformat()) for r in revs]


@router.put("/{expense_id}", response_model=ExpenseDetailOut)
def update_expense(public_id: str, expense_id: str, body: ExpenseIn, db: Session = Depends(get_db),
                   ident: Identity = Depends(current_identity), if_match: str | None = Header(default=None)):
    expected = _parse_if_match(if_match)
    acc = load_group_access(db, public_id, ident, for_update=True)
    e = _load_expense(db, acc.group, expense_id, for_update=True)
    if e.state != "active":
        raise not_found()
    if e.version != expected:
        raise ApiError(409, "REVISION_CONFLICT", "他の人が先にこの支出を更新しました。最新の内容を確認してください",
                       current_version=e.version)
    res = service.compute(body, str(e.id), _member_ids(db, acc.group))
    res.require_valid()
    bd = service.breakdown(res).model_dump()
    e.title, e.amount_minor = body.title, body.amount_minor
    e.split_mode, e.occurred_on = body.split.mode, body.occurred_on
    e.version += 1
    _write_rows(db, e, {p.member_id: p.amount_minor for p in body.payers}, res.shares)
    db.add(ExpenseRevision(expense_id=e.id, version=e.version, change_kind="update", spec=_spec_json(body),
                           result=bd, changed_by=ident.id))
    acc.group.ledger_version += 1
    db.flush()
    db.refresh(e)
    out = ExpenseDetailOut(**_expense_out(db, e).model_dump(), spec=_spec_json(body), breakdown=bd)
    db.commit()
    return out


@router.delete("/{expense_id}", status_code=200, response_model=ExpenseOut)
def delete_expense(public_id: str, expense_id: str, db: Session = Depends(get_db),
                   ident: Identity = Depends(current_identity), if_match: str | None = Header(default=None)):
    expected = _parse_if_match(if_match)
    acc = load_group_access(db, public_id, ident, for_update=True)
    e = _load_expense(db, acc.group, expense_id, for_update=True)
    if e.state != "active":
        raise not_found()
    if e.version != expected:
        raise ApiError(409, "REVISION_CONFLICT", "他の人が先にこの支出を更新しました", current_version=e.version)
    prev = db.execute(select(ExpenseRevision).where(ExpenseRevision.expense_id == e.id)
                      .order_by(ExpenseRevision.version.desc()).limit(1)).scalar_one()
    e.state = "deleted"
    e.version += 1
    db.add(ExpenseRevision(expense_id=e.id, version=e.version, change_kind="delete", spec=prev.spec,
                           result=prev.result, changed_by=ident.id))
    acc.group.ledger_version += 1
    db.flush()
    db.refresh(e)
    out = _expense_out(db, e)
    db.commit()
    return out
