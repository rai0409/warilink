"""グループの終了・作成者の引き継ぎ・参加者不在の検出・完全な削除(STEP4、docs/DATA_MODEL.md §11)。

- 作成者が抜ける・退会すると、参加の早い(position の小さい)参加者へ自動で引き継ぐ。誰もいなければ「参加者不在」にする。
- 作成者はグループを「終了」できる(以後は閲覧のみ。精算の記録は他のメンバーのために残す)。
- 参加者不在から orphaned_group_retention_days、終了から closed_group_retention_days で、purge-images のジョブが
  グループのすべての行とレシート画像を完全に削除し、削除済み一覧(tombstone)に ID を残す。

削除ジャーナル(STEP 01、app/ops/deletion_journal.py)の手順も、削除の 3 つの経路(退会・振込先の消去・グループの
完全な削除)で共通に使うためここに置く:
  op = begin_deletion(db, 対象)   # 行をロックした後・変更の前。DB の外へ prepare(失敗したら何も変えずに 503)
  ... 削除と add_tombstone(..., op_id=op) ...
  commit_deletion(db, op)        # commit → finalize(書けなければ再試行の後 503。成功とは返さない)
確実にロールバックしたときだけ abort_deletion(op)。commit 自体の失敗は結果が曖昧なので abort しない(reconcile)。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ApiError
from app.core.security import now
from app.models.tables import (
    AuditEvent,
    DeletionJournalOp,
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
from app.ops.deletion_journal import DeletionJournal, JournalError, JournalUnavailable, journal_from_url


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


def hard_delete_group(db: Session, group: Group, *, op_id: uuid.UUID | None = None) -> list[str]:
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
    add_tombstone(db, "group", gid, op_id=op_id)
    db.add(AuditEvent(actor_identity_id=None, action="group.delete", target_type="group", target_id=str(gid),
                      safe_metadata={"images": len(keys)}))
    return keys


def add_tombstone(db: Session, kind: str, target_id: uuid.UUID, *, op_id: uuid.UUID | None = None) -> None:
    exists = db.execute(select(func.count()).select_from(DeletionTombstone).where(
        DeletionTombstone.kind == kind, DeletionTombstone.target_id == target_id)).scalar_one()
    if not exists:
        db.add(DeletionTombstone(kind=kind, target_id=target_id, op_id=op_id))


# ---------------------------------------------------------------- 削除ジャーナル(STEP 01)

FINALIZE_ATTEMPTS = 3


def current_journal() -> DeletionJournal | None:
    """設定(WARILINK_DELETION_JOURNAL)のジャーナル。未設定は None(本番は起動時の検査で拒否する)。"""
    return journal_from_url(get_settings().deletion_journal)


def journal_unavailable() -> ApiError:
    return ApiError(503, "DELETION_JOURNAL_UNAVAILABLE",
                    "削除の記録を保存できなかったため、削除していません。時間をおいてもう一度お試しください")


def deletion_unconfirmed() -> ApiError:
    return ApiError(503, "DELETION_PENDING_CONFIRMATION",
                    "削除は行いましたが、削除の記録の確定を確認できませんでした。記録は自動で確定を再試行します")


def begin_deletion(db: Session, targets: Iterable[tuple[str, uuid.UUID]]) -> uuid.UUID | None:
    """DB の外へ prepare を耐久化し、同じトランザクションで操作の行を作る。ジャーナル未設定なら None。

    失敗は JournalError(DB はまだ変えていない)。"""
    j = current_journal()
    if j is None:
        return None
    op_id = uuid.uuid4()
    j.prepare(op_id, list(targets))
    db.add(DeletionJournalOp(op_id=op_id))
    return op_id


def abort_deletion(op_id: uuid.UUID | None) -> None:
    """確実にロールバックした操作を取り消しとして残す。書けなくても未確定のまま残るだけ(reconcile で扱う)。"""
    j = current_journal() if op_id is not None else None
    if j is None or op_id is None:
        return
    try:
        j.abort(op_id)
    except JournalError:
        pass


def commit_deletion(db: Session, op_id: uuid.UUID | None, *,
                    sleep: Callable[[float], None] = time.sleep) -> None:
    """commit してから finalize する。finalize できなければ JournalUnavailable(DB の削除は commit 済み)。"""
    db.commit()
    if op_id is not None:
        finalize_op(db, op_id, sleep=sleep)


def finalize_op(db: Session, op_id: uuid.UUID, *, journal: DeletionJournal | None = None,
                sleep: Callable[[float], None] = time.sleep) -> None:
    j = journal or current_journal()
    if j is None:
        raise JournalUnavailable("削除ジャーナルが設定されていません")
    for i in range(FINALIZE_ATTEMPTS):
        try:
            j.finalize(op_id)
            break
        except JournalError:
            if i == FINALIZE_ATTEMPTS - 1:
                raise JournalUnavailable("削除の記録を確定できません") from None
            sleep(0.2 * (i + 1))
    try:  # DB 側の印は目安(正はジャーナル)。書けなくても次の reconcile で埋まる
        db.execute(update(DeletionJournalOp).where(DeletionJournalOp.op_id == op_id,
                                                   DeletionJournalOp.finalized_at.is_(None))
                   .values(finalized_at=now()))
        db.commit()
    except SQLAlchemyError:
        db.rollback()


def confirm_pending_deletions(db: Session, kind: str, target_ids: list[uuid.UUID]) -> bool:
    """同じ対象の削除が commit 済みで未確定なら finalize する(削除の再送で確定させる)。確定したら True。

    finalize できなければ JournalUnavailable。"""
    if not target_ids or current_journal() is None:
        return False
    ops = list(db.execute(select(DeletionJournalOp.op_id).join(
        DeletionTombstone, DeletionTombstone.op_id == DeletionJournalOp.op_id).where(
        DeletionTombstone.kind == kind, DeletionTombstone.target_id.in_(target_ids),
        DeletionJournalOp.finalized_at.is_(None))).scalars())
    for op in ops:
        finalize_op(db, op)
    return bool(ops)


def reconcile_journal(db: Session, journal: DeletionJournal | None = None) -> dict[str, int]:
    """未確定の操作を照合する。DB に操作の行があるもの(commit 済み)だけを finalize し、DB の印を埋める。

    DB に無い未確定の操作は、そのまま残す(自動で abort にしない)。戻り値は件数だけ。"""
    j = journal or current_journal()
    if j is None:
        return {}
    st = j.state()
    in_db = set(map(str, db.execute(select(DeletionJournalOp.op_id).where(
        DeletionJournalOp.op_id.in_([uuid.UUID(o) for o in st.pending]))).scalars())) if st.pending else set()
    finalized = 0
    for op in sorted(in_db):
        j.finalize(uuid.UUID(op))
        finalized += 1
    done = set(st.finalized) | in_db
    unmarked = list(db.execute(select(DeletionJournalOp).where(DeletionJournalOp.finalized_at.is_(None))).scalars())
    t, missing = now(), 0
    for row in unmarked:
        if str(row.op_id) in done:
            row.finalized_at = t
        elif str(row.op_id) not in st.pending and str(row.op_id) not in st.aborted:
            missing += 1  # DB にあってジャーナルに無い(別のジャーナル・欠落)。監視で知らせる
    db.commit()
    return {"finalized": finalized, "pending": len(st.pending) - finalized, "missing_in_journal": missing}


def groups_due_for_deletion(db: Session, *, orphaned_before: datetime, closed_before: datetime) -> list[Group]:
    return list(db.execute(select(Group).where(
        ((Group.orphaned_at.is_not(None)) & (Group.orphaned_at <= orphaned_before))
        | ((Group.closed_at.is_not(None)) & (Group.closed_at <= closed_before))
    ).with_for_update(skip_locked=True)).scalars())


def abort_absent_ops(db: Session, journal: DeletionJournal, *, older_than: timedelta) -> int:
    """未確定のまま older_than を過ぎ、この DB に操作の行が無いものを取り消しとして記録する(運用者が明示的に実行)。

    この DB が、その操作より後に古いバックアップから戻されていないこと(本番の現用 DB であること)を運用者が
    確かめてから使う。戻した DB で実行すると、commit 済みの削除を取り消しと記録してしまう。"""
    st = journal.state()
    cut = now() - older_than
    old = [op for op, (_, at) in st.pending.items() if datetime.fromisoformat(at) <= cut]
    if not old:
        return 0
    present = set(map(str, db.execute(select(DeletionJournalOp.op_id).where(
        DeletionJournalOp.op_id.in_([uuid.UUID(o) for o in old]))).scalars()))
    n = 0
    for op in old:
        if op not in present:
            journal.abort(uuid.UUID(op))
            n += 1
    return n
