from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import require_feature
from app.core.db import get_db
from app.core.errors import ApiError
from app.core.idempotency import run_idempotent
from app.core.security import current_identity, load_group_access, load_transfer_party, parse_uuid
from app.domain.settlement import verify_transfers
from app.models.tables import (
    AuditEvent,
    Group,
    Identity,
    Membership,
    SettlementBatch,
    SettlementTransfer,
    TransferEvent,
)
from app.modules.groups.lifecycle import require_open

from . import service
from .schemas import BalancesOut, BatchOut, SettlementPlanOut, TransferDetailOut, TransferOut

router = APIRouter(tags=["settlement"])


class BatchCreate(BaseModel):
    # プレビュー時点の台帳版。確定までに支出が変わっていれば 409 で再確認を求める。
    expected_ledger_version: int = Field(strict=True, ge=0)


@router.get("/groups/{public_id}/balances", response_model=BalancesOut)
def balances(public_id: str, db: Session = Depends(get_db), ident: Identity = Depends(current_identity)):
    acc = load_group_access(db, public_id, ident)
    ledger = service.load_ledger(db, acc.group)
    _, rows = service.balance_rows(ledger)
    return {"ledger_version": acc.group.ledger_version, "currency": "JPY", "balances": rows,
            "balance_sum_minor": sum(r["net_minor"] for r in rows)}


@router.post("/groups/{public_id}/settlements/preview", response_model=SettlementPlanOut)
def preview(public_id: str, db: Session = Depends(get_db), ident: Identity = Depends(current_identity)):
    require_feature("settlement")
    acc = load_group_access(db, public_id, ident)
    ledger = service.load_ledger(db, acc.group)
    net, rows = service.balance_rows(ledger)
    return service.plan_out(acc.group.ledger_version, ledger, rows, service.optimize(net))


@router.post("/groups/{public_id}/settlement-batches", status_code=201, response_model=BatchOut)
def create_batch(public_id: str, body: BatchCreate, request: Request, db: Session = Depends(get_db),
                 ident: Identity = Depends(current_identity)):
    require_feature("settlement")
    def do():
        acc = load_group_access(db, public_id, ident, for_update=True)  # グループ行ロックで直列化
        require_open(acc.group)
        group = acc.group
        if body.expected_ledger_version != group.ledger_version:
            raise ApiError(409, "REVISION_CONFLICT", "確認後に支出が変更されました。最新の精算案を確認してください",
                           current_ledger_version=group.ledger_version)
        current = db.execute(select(SettlementBatch).where(
            SettlementBatch.group_id == group.id, SettlementBatch.state == "active")).scalar_one_or_none()
        if current is not None and current.ledger_version == group.ledger_version:
            raise ApiError(409, "SETTLEMENT_ALREADY_CONFIRMED", "この内容の精算は既に確定しています",
                           batch_id=str(current.id))

        ledger = service.load_ledger(db, group)
        net, rows = service.balance_rows(ledger)
        res = service.optimize(net)
        verify_transfers(net, res.transfers)  # 確定前に再検証。失敗時は例外でロールバック

        if current is not None:
            # 未払いの送金だけを置き換える。申告済み・確認済みは変更しない。
            db.execute(update(SettlementTransfer)
                       .where(SettlementTransfer.batch_id == current.id, SettlementTransfer.status == "unpaid")
                       .values(status="superseded", version=SettlementTransfer.version + 1))
            current.state = "superseded"
            db.flush()
        next_version = (db.execute(select(func.coalesce(func.max(SettlementBatch.version), 0))
                                   .where(SettlementBatch.group_id == group.id)).scalar_one()) + 1
        plan = service.plan_out(group.ledger_version, ledger, rows, res)
        opt = plan["optimization"]
        batch = SettlementBatch(
            group_id=group.id, version=next_version, ledger_version=group.ledger_version, state="active",
            method=res.method, is_exact_minimum=res.is_exact_minimum, fallback_reason=res.fallback_reason,
            naive_transfer_count=opt["naive_transfer_count"], minimum_transfer_count=res.minimum_transfer_count,
            balances={r["member_id"]: r["net_minor"] for r in rows}, assumptions=res.assumptions,
            elapsed_ms=res.elapsed_ms, created_by=ident.id,
        )
        db.add(batch)
        db.flush()
        for t in res.transfers:
            db.add(SettlementTransfer(batch_id=batch.id, group_id=group.id, from_member_id=parse_uuid(t.from_member),
                                      to_member_id=parse_uuid(t.to_member), amount_minor=t.amount_minor))
            db.flush()
        db.add(AuditEvent(actor_identity_id=ident.id, action="settlement.batch.create", target_type="group",
                          target_id=str(group.id), safe_metadata={"version": next_version}))
        db.flush()
        return service.batch_out(db, group, batch)

    return run_idempotent(db, request, ident, f"settlement.batch:{public_id}", body, do, status_code=201)


@router.get("/groups/{public_id}/settlement-batches/current", response_model=BatchOut | None)
def current_batch(public_id: str, db: Session = Depends(get_db), ident: Identity = Depends(current_identity)):
    acc = load_group_access(db, public_id, ident)
    b = db.execute(select(SettlementBatch).where(SettlementBatch.group_id == acc.group.id,
                                                 SettlementBatch.state == "active")).scalar_one_or_none()
    return None if b is None else service.batch_out(db, acc.group, b)


@router.get("/groups/{public_id}/settlement-batches", response_model=list[BatchOut])
def list_batches(public_id: str, db: Session = Depends(get_db), ident: Identity = Depends(current_identity)):
    acc = load_group_access(db, public_id, ident)
    rows = db.execute(select(SettlementBatch).where(SettlementBatch.group_id == acc.group.id)
                      .order_by(SettlementBatch.version.desc())).scalars()
    return [service.batch_out(db, acc.group, b) for b in rows]


# ---------------------------------------------------------------- 送金の状態


def _load_transfer_for_party(db: Session, transfer_id: str, ident: Identity, *, for_update: bool = False):
    # 精算の当事者(支払者・受取人)以外には存在を隠す(振込先の表示と同じ判定を共有する)
    return load_transfer_party(db, transfer_id, ident, for_update=for_update)


def _transition(db: Session, t: SettlementTransfer, ident: Identity, kind: str, to: str) -> None:
    db.add(TransferEvent(transfer_id=t.id, kind=kind, from_status=t.status, to_status=to,
                         actor_identity_id=ident.id))
    t.status = to
    t.version += 1


@router.get("/settlements/{transfer_id}", response_model=TransferDetailOut)
def get_transfer(transfer_id: str, db: Session = Depends(get_db), ident: Identity = Depends(current_identity)):
    t, me = _load_transfer_for_party(db, transfer_id, ident)
    group = db.get(Group, t.group_id)
    assert group is not None
    names = {str(m.id): m.display_name for m in db.execute(
        select(Membership).where(Membership.id.in_([t.from_member_id, t.to_member_id]))).scalars()}
    return {**service.transfer_out(t), "group_public_id": group.public_id,
            "from_name": names[str(t.from_member_id)], "to_name": names[str(t.to_member_id)],
            "my_role": "payer" if me.id == t.from_member_id else "payee",
            # 銀行照合は別の証跡状態として扱う(Phase 5)。現時点では未接続。
            "bank_evidence_state": "not_connected"}


@router.post("/settlements/{transfer_id}/report-paid", response_model=TransferOut)
def report_paid(transfer_id: str, db: Session = Depends(get_db), ident: Identity = Depends(current_identity)):
    """支払者による送金申告。アプリは送金を実行しない。申告は銀行での確認を意味しない。"""
    require_feature("settlement_confirmations")
    t, me = _load_transfer_for_party(db, transfer_id, ident, for_update=True)
    require_open(db.get(Group, t.group_id))  # type: ignore[arg-type]
    if me.id != t.from_member_id:
        raise ApiError(403, "ACCESS_DENIED", "送金申告は支払う本人だけが行えます")
    if t.status in ("reported_paid", "recipient_confirmed"):
        return service.transfer_out(t)  # 冪等
    if t.status != "unpaid":
        raise ApiError(409, "TRANSFER_SUPERSEDED", "この精算案は新しい精算案に置き換えられました")
    _transition(db, t, ident, "report_paid", "reported_paid")
    db.flush()
    out = service.transfer_out(t)
    db.commit()
    return out


@router.post("/settlements/{transfer_id}/confirm-received", response_model=TransferOut)
def confirm_received(transfer_id: str, db: Session = Depends(get_db), ident: Identity = Depends(current_identity)):
    """受取人による手動の受取確認。銀行による証明ではない。"""
    require_feature("settlement_confirmations")
    t, me = _load_transfer_for_party(db, transfer_id, ident, for_update=True)
    require_open(db.get(Group, t.group_id))  # type: ignore[arg-type]
    if me.id != t.to_member_id:
        raise ApiError(403, "ACCESS_DENIED", "受取確認は受け取る本人だけが行えます")
    if t.status == "recipient_confirmed":
        return service.transfer_out(t)
    if t.status not in ("unpaid", "reported_paid"):
        raise ApiError(409, "TRANSFER_SUPERSEDED", "この精算案は新しい精算案に置き換えられました")
    _transition(db, t, ident, "confirm_received", "recipient_confirmed")
    db.flush()
    out = service.transfer_out(t)
    db.commit()
    return out
