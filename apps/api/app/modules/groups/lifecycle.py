"""グループの終了・作成者の引き継ぎ・参加者不在の検出・完全な削除(STEP4、docs/DATA_MODEL.md §11)。

- 作成者が抜ける・退会すると、参加の早い(position の小さい)参加者へ自動で引き継ぐ。誰もいなければ「参加者不在」にする。
- 作成者はグループを「終了」できる(以後は閲覧のみ。精算の記録は他のメンバーのために残す)。
- 参加者不在から orphaned_group_retention_days、終了から closed_group_retention_days で、purge-images のジョブが
  グループのすべての行とレシート画像を完全に削除し、削除済み一覧(tombstone)に ID を残す。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.core.security import now
from app.models.tables import (
    AuditEvent,
    DeletionTombstone,
    Expense,
    ExpenseRevision,
    Group,
    Invitation,
    Membership,
    OcrCorrection,
    OcrQuotaEntry,
    OcrRun,
    Receipt,
    ReceiptJob,
    SettlementBatch,
    SettlementTransfer,
    TransferDestinationPin,
    TransferEvent,
)


def require_open(group: Group) -> None:
    """終了したグループは閲覧のみ(支出・精算・メンバー・招待の変更を受け付けない)。"""
    if group.closed_at is not None:
        raise ApiError(409, "GROUP_CLOSED", "このグループは終了しています。閲覧だけできます")


def claimed_members(db: Session, group: Group) -> list[Membership]:
    return list(db.execute(select(Membership).where(
        Membership.group_id == group.id, Membership.identity_id.is_not(None), Membership.status == "active")
        .order_by(Membership.position)).scalars())


def transfer_owner(db: Session, group: Group, old: Membership, new: Membership, *, reason: str) -> None:
    assert new.identity_id is not None
    old.role = "member"
    new.role = "owner"
    group.owner_identity_id = new.identity_id
    db.add(AuditEvent(actor_identity_id=old.identity_id, action="group.owner.transfer", target_type="group",
                      target_id=str(group.id), safe_metadata={"reason": reason}))


def release_owner(db: Session, group: Group, owner: Membership) -> bool:
    """作成者がいなくなる前に呼ぶ。参加の早い参加者へ引き継げたら True(誰もいなければ False)。"""
    successor = next((m for m in claimed_members(db, group) if m.id != owner.id), None)
    if successor is None:
        return False
    transfer_owner(db, group, owner, successor, reason="auto")
    return True


def mark_if_orphaned(db: Session, group: Group) -> None:
    """結び付きを外した後に呼ぶ。参加者が誰もいなくなったら、削除までの期間を数え始める。"""
    db.flush()
    if group.orphaned_at is None and not claimed_members(db, group):
        group.orphaned_at = now()


def hard_delete_group(db: Session, group: Group) -> list[str]:
    """グループのすべての行を消し、消すべき画像の保存キーを返す(画像ファイルの削除と commit は呼び出し側)。"""
    gid = group.id
    keys = list(db.execute(select(Receipt.storage_key).where(Receipt.group_id == gid,
                                                             Receipt.image_deleted_at.is_(None))).scalars())
    transfers = select(SettlementTransfer.id).where(SettlementTransfer.group_id == gid)
    receipts = select(Receipt.id).where(Receipt.group_id == gid)
    expenses = select(Expense.id).where(Expense.group_id == gid)
    for stmt in (
        delete(TransferDestinationPin).where(TransferDestinationPin.transfer_id.in_(transfers)),
        delete(TransferEvent).where(TransferEvent.transfer_id.in_(transfers)),
        delete(SettlementTransfer).where(SettlementTransfer.group_id == gid),
        delete(SettlementBatch).where(SettlementBatch.group_id == gid),
        delete(OcrCorrection).where(OcrCorrection.receipt_id.in_(receipts)),
        delete(OcrQuotaEntry).where(OcrQuotaEntry.receipt_id.in_(receipts)),
        delete(OcrRun).where(OcrRun.receipt_id.in_(receipts)),
        delete(ReceiptJob).where(ReceiptJob.receipt_id.in_(receipts)),
        delete(Receipt).where(Receipt.group_id == gid),
        delete(ExpenseRevision).where(ExpenseRevision.expense_id.in_(expenses)),
        delete(Expense).where(Expense.group_id == gid),  # 負担・立替の行は ON DELETE CASCADE
        delete(Invitation).where(Invitation.group_id == gid),
        delete(Membership).where(Membership.group_id == gid),
        delete(Group).where(Group.id == gid),
    ):
        db.execute(stmt)
    add_tombstone(db, "group", gid)
    db.add(AuditEvent(actor_identity_id=None, action="group.delete", target_type="group", target_id=str(gid),
                      safe_metadata={"images": len(keys)}))
    return keys


def add_tombstone(db: Session, kind: str, target_id: uuid.UUID) -> None:
    exists = db.execute(select(func.count()).select_from(DeletionTombstone).where(
        DeletionTombstone.kind == kind, DeletionTombstone.target_id == target_id)).scalar_one()
    if not exists:
        db.add(DeletionTombstone(kind=kind, target_id=target_id))


def groups_due_for_deletion(db: Session, *, orphaned_before: datetime, closed_before: datetime) -> list[Group]:
    return list(db.execute(select(Group).where(
        ((Group.orphaned_at.is_not(None)) & (Group.orphaned_at <= orphaned_before))
        | ((Group.closed_at.is_not(None)) & (Group.closed_at <= closed_before))
    ).with_for_update(skip_locked=True)).scalars())
