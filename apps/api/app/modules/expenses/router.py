from __future__ import annotations

import base64
import binascii
import json
import uuid
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.encoders import jsonable_encoder
from sqlalchemy import Date, cast, delete, func, select
from sqlalchemy.orm import Session

from app.core.config import require_feature
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
    SettlementTransfer,
)
from app.modules.groups.lifecycle import require_open
from app.modules.receipts.service import link_receipt
from app.modules.settlement import service as settle
from app.modules.settlement.schemas import ExpensePreviewOut

from . import service
from .schemas import (
    Category,
    CategoryStat,
    ExpenseCreate,
    ExpenseDetailOut,
    ExpenseIn,
    ExpenseOut,
    ExpensePreviewIn,
    ExpenseSearchOut,
    GroupStatsOut,
    MemberStat,
    MonthStat,
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
        split_mode=e.split_mode, occurred_on=e.occurred_on, category=e.category, version=e.version, state=e.state,
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


# 支出日(未入力なら登録日、日本時間)。検索・集計の期間はこの日付で判定する
_DAY = func.coalesce(Expense.occurred_on, cast(func.timezone("Asia/Tokyo", Expense.created_at), Date))
_SORTS = {
    "date_desc": (_DAY.desc(), Expense.created_at.desc(), Expense.id),
    "date_asc": (_DAY.asc(), Expense.created_at.asc(), Expense.id),
    "amount_desc": (Expense.amount_minor.desc(), Expense.created_at.desc(), Expense.id),
    "amount_asc": (Expense.amount_minor.asc(), Expense.created_at.desc(), Expense.id),
}
MAX_OFFSET = 10_000


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        off = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))["o"]
    except (ValueError, KeyError, TypeError, binascii.Error):
        raise ApiError(422, "INVALID_CURSOR", "ページの指定が正しくありません。最初から表示し直してください") from None
    if not isinstance(off, int) or not 0 <= off <= MAX_OFFSET:
        raise ApiError(422, "INVALID_CURSOR", "ページの指定が正しくありません。最初から表示し直してください")
    return off


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode()).rstrip(b"=").decode()


@router.get("/search", response_model=ExpenseSearchOut)
def search_expenses(
    public_id: str,
    q: str | None = Query(default=None, max_length=100),
    category: Category | None = None,
    uncategorized: bool = False,
    date_from: date | None = None,
    date_to: date | None = None,
    payer_member_id: str | None = None,
    created_by_me: bool = False,
    sort: Literal["date_desc", "date_asc", "amount_desc", "amount_asc"] = "date_desc",
    limit: int = Query(default=30, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=200),
    db: Session = Depends(get_db),
    ident: Identity = Depends(current_identity),
) -> ExpenseSearchOut:
    """グループ内の有効な支出だけを検索する(他のグループの支出は対象にならない)。"""
    acc = load_group_access(db, public_id, ident)
    offset = _decode_cursor(cursor)
    conds = [Expense.group_id == acc.group.id, Expense.state == "active"]
    if q and q.strip():
        # LIKE の特殊文字はそのままの文字として扱う
        esc = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conds.append(Expense.title.ilike(f"%{esc}%", escape="\\"))
    if uncategorized:
        conds.append(Expense.category.is_(None))
    elif category:
        conds.append(Expense.category == category)
    if date_from:
        conds.append(_DAY >= date_from)
    if date_to:
        conds.append(_DAY <= date_to)
    if payer_member_id:
        conds.append(Expense.id.in_(select(ExpensePayer.expense_id).where(
            ExpensePayer.member_id == parse_uuid(payer_member_id))))
    if created_by_me:
        conds.append(Expense.created_by == ident.id)
    count, total = db.execute(select(func.count(), func.coalesce(func.sum(Expense.amount_minor), 0))
                              .where(*conds)).one()
    rows = list(db.execute(select(Expense).where(*conds).order_by(*_SORTS[sort])
                           .offset(offset).limit(limit + 1)).scalars())
    more = len(rows) > limit and offset + limit <= MAX_OFFSET
    return ExpenseSearchOut(items=[_expense_out(db, e) for e in rows[:limit]], total_count=count,
                            total_amount_minor=int(total), next_cursor=_encode_cursor(offset + limit) if more else None)


@router.get("/stats", response_model=GroupStatsOut)
def group_stats(public_id: str, date_from: date | None = None, date_to: date | None = None,
                db: Session = Depends(get_db), ident: Identity = Depends(current_identity)) -> GroupStatsOut:
    """グループ内の集計(このグループのメンバーへの表示のためだけに計算する。広告・外部提供には使わない)。"""
    acc = load_group_access(db, public_id, ident)
    conds = [Expense.group_id == acc.group.id, Expense.state == "active"]
    if date_from:
        conds.append(_DAY >= date_from)
    if date_to:
        conds.append(_DAY <= date_to)
    ids = select(Expense.id).where(*conds)
    cat_rows = db.execute(select(Expense.category, func.count(), func.sum(Expense.amount_minor)).where(*conds)
                          .group_by(Expense.category).order_by(func.sum(Expense.amount_minor).desc())).all()
    month = func.to_char(_DAY, "YYYY-MM")
    month_rows = db.execute(select(month, func.count(), func.sum(Expense.amount_minor)).where(*conds)
                            .group_by(month).order_by(month)).all()
    paid = dict(db.execute(select(ExpensePayer.member_id, func.sum(ExpensePayer.amount_minor))
                           .where(ExpensePayer.expense_id.in_(ids)).group_by(ExpensePayer.member_id)).all())
    share = dict(db.execute(select(ExpenseShare.member_id, func.sum(ExpenseShare.amount_minor))
                            .where(ExpenseShare.expense_id.in_(ids)).group_by(ExpenseShare.member_id)).all())
    members = db.execute(select(Membership.id).where(Membership.group_id == acc.group.id)
                         .order_by(Membership.position)).scalars().all()
    unconf = db.execute(select(func.count(), func.coalesce(func.sum(SettlementTransfer.amount_minor), 0))
                        .where(SettlementTransfer.group_id == acc.group.id,
                               SettlementTransfer.status == "reported_paid")).one()
    count = sum(c for _, c, _ in cat_rows)
    total = sum(int(t) for _, _, t in cat_rows)
    return GroupStatsOut(
        expense_count=count, total_minor=total,
        by_category=[CategoryStat(category=c, count=n, total_minor=int(t)) for c, n, t in cat_rows],
        by_month=[MonthStat(month=m, count=n, total_minor=int(t)) for m, n, t in month_rows],
        by_member=[MemberStat(member_id=str(m), paid_minor=int(paid.get(m, 0)), share_minor=int(share.get(m, 0)))
                   for m in members],
        unconfirmed_transfer_count=unconf[0], unconfirmed_transfer_minor=int(unconf[1]),
        notes=["「立て替えた額」は支払った金額、「負担する額」は各自が使った分です。差が精算の対象になります",
               "「受取確認待ち」は送金の申告だけで、受取人はまだ確認していません",
               "期間で絞り込んだ集計は、その期間の支出だけを数えます(精算額とは一致しないことがあります)"],
    )


@router.post("/preview", response_model=ExpensePreviewOut)
def preview_expense(public_id: str, body: ExpensePreviewIn, db: Session = Depends(get_db),
                    ident: Identity = Depends(current_identity)) -> dict:
    """保存せずに負担額と、反映後の精算候補を計算する(入力のたびに呼ぶ想定)。"""
    require_feature("manual_expenses")
    if body.split.mode == "itemized":
        require_feature("itemized_allocation")
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
    require_feature("manual_expenses")
    if body.split.mode == "itemized":
        require_feature("itemized_allocation")
    def do():
        acc = load_group_access(db, public_id, ident, for_update=True)
        require_open(acc.group)
        eid = parse_uuid(body.client_expense_id) if body.client_expense_id else uuid.uuid4()
        if db.get(Expense, eid) is not None:
            raise ApiError(409, "EXPENSE_EXISTS", "この支出は既に登録されています")
        res = service.compute(body, str(eid), _member_ids(db, acc.group))
        res.require_valid()  # 不一致・未割当があれば確定しない
        bd = service.breakdown(res).model_dump()
        e = Expense(id=eid, group_id=acc.group.id, title=body.title, amount_minor=body.amount_minor,
                    split_mode=body.split.mode, occurred_on=body.occurred_on, category=body.category,
                    created_by=ident.id, version=1)
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
    require_feature("manual_expenses")
    if body.split.mode == "itemized":
        require_feature("itemized_allocation")
    expected = _parse_if_match(if_match)
    acc = load_group_access(db, public_id, ident, for_update=True)
    require_open(acc.group)
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
    e.split_mode, e.occurred_on, e.category = body.split.mode, body.occurred_on, body.category
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
    require_open(acc.group)
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
