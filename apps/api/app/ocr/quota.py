"""OCR 利用枠(台帳方式)。上限は設定値。Phase 6 で広告報酬の付与を追加する。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ApiError
from app.models.tables import Group, Identity, OcrQuotaEntry, Receipt, ReceiptJob

JST = ZoneInfo("Asia/Tokyo")


def current_period(now: datetime | None = None) -> str:
    return (now or datetime.now(JST)).astimezone(JST).strftime("%Y-%m")


@dataclass
class QuotaStatus:
    period: str
    free: int
    cap: int
    consumed: int
    refunded: int

    @property
    def used(self) -> int:
        return self.consumed - self.refunded

    @property
    def remaining(self) -> int:
        # Phase 3 では無料枠のみ(広告報酬は Phase 6)。上限は cap を超えない
        return max(0, min(self.free, self.cap) - self.used)


def status(db: Session, identity_id: uuid.UUID, period: str | None = None) -> QuotaStatus:
    s = get_settings()
    period = period or current_period()
    rows = dict(db.execute(
        select(OcrQuotaEntry.kind, func.coalesce(func.sum(OcrQuotaEntry.units), 0))
        .where(OcrQuotaEntry.identity_id == identity_id, OcrQuotaEntry.period == period)
        .group_by(OcrQuotaEntry.kind)
    ).all())
    return QuotaStatus(period, s.ocr_free_monthly_scans, s.ocr_monthly_scan_cap,
                       int(rows.get("consume", 0)), int(rows.get("refund", 0)))


def reserve(db: Session, identity_id: uuid.UUID) -> QuotaStatus:
    """同じ利用者の同時アップロードで枠を超えないよう、利用者の行をロックしてから残りを確認する。

    レシート行の挿入(外部キー確認で利用者の行に共有ロックがかかる)より先に呼ぶこと。後だとデッドロックする。
    """
    db.execute(select(Identity.id).where(Identity.id == identity_id).with_for_update())
    st = status(db, identity_id)
    if st.remaining <= 0:
        raise ApiError(429, "OCR_QUOTA_EXCEEDED",
                       f"今月の読み取り枠({st.free}枚)を使い切りました。レシートの内容は手入力で登録できます")
    return st


def consume(db: Session, identity_id: uuid.UUID, receipt_id: uuid.UUID) -> QuotaStatus:
    """1 枚消費する(reserve で確認済みの同じトランザクション内で呼ぶ)。"""
    st = reserve(db, identity_id)
    db.add(OcrQuotaEntry(identity_id=identity_id, period=st.period, kind="consume", units=1, receipt_id=receipt_id))
    db.flush()
    st.consumed += 1
    return st


def refund(db: Session, receipt_id: uuid.UUID, reason: str) -> bool:
    """消費した枠を返す。消費と同じ月に計上し、1 枚につき 1 回だけ(一意制約で二重返還を防ぐ)。"""
    c = db.execute(select(OcrQuotaEntry).where(OcrQuotaEntry.receipt_id == receipt_id,
                                               OcrQuotaEntry.kind == "consume")).scalar_one_or_none()
    if c is None:
        return False
    res = db.execute(insert(OcrQuotaEntry).values(
        id=uuid.uuid4(), identity_id=c.identity_id, period=c.period, kind="refund", units=1,
        receipt_id=receipt_id, reason=reason,
    ).on_conflict_do_nothing(constraint="uq_quota_receipt_kind").returning(OcrQuotaEntry.id))
    return res.scalar_one_or_none() is not None


def precheck(db: Session, identity_id: uuid.UUID) -> None:
    """重い画像処理の前の安い確認(ロックなし)。確定的な判定は reserve で行う。"""
    st = status(db, identity_id)
    if st.remaining <= 0:
        raise ApiError(429, "OCR_QUOTA_EXCEEDED",
                       f"今月の読み取り枠({st.free}枚)を使い切りました。レシートの内容は手入力で登録できます")


def check_limits(db: Session, identity_id: uuid.UUID, group_id: uuid.UUID, *, lock: bool = False) -> None:
    """利用者・グループ・全体の流量制限。lock=True ではグループ行をロックして同時アップロードでも超えない。

    reserve(利用者の行のロック)の後に呼ぶこと(ロック順: 利用者 → グループ)。
    """
    s = get_settings()
    now = datetime.now(JST)
    if lock:
        db.execute(select(Group.id).where(Group.id == group_id).with_for_update())
    limited = ApiError(429, "OCR_RATE_LIMITED",
                       "短時間に多くの画像が送られました。しばらく待ってから試すか、手入力してください")
    n = db.execute(select(func.count()).select_from(Receipt).where(
        Receipt.uploaded_by == identity_id, Receipt.created_at > now - timedelta(minutes=10))).scalar_one()
    if n >= s.ocr_rate_identity_per_10min:
        raise limited
    n = db.execute(select(func.count()).select_from(Receipt).where(
        Receipt.group_id == group_id, Receipt.created_at > now - timedelta(hours=1))).scalar_one()
    if n >= s.ocr_rate_group_per_hour:
        raise limited
    n = db.execute(select(func.count()).select_from(ReceiptJob).join(Receipt, Receipt.id == ReceiptJob.receipt_id)
                   .where(Receipt.uploaded_by == identity_id, ReceiptJob.state.in_(("queued", "running")))).scalar_one()
    if n >= s.ocr_max_pending_per_identity:
        raise ApiError(429, "OCR_RATE_LIMITED", "読み取り中の画像があります。終わってから次の画像を選んでください")
    n = db.execute(select(func.count()).select_from(ReceiptJob)
                   .where(ReceiptJob.state.in_(("queued", "running")))).scalar_one()
    if n >= s.ocr_max_queue_backlog:
        raise ApiError(503, "OCR_BUSY", "読み取りが混み合っています。時間をおいて試すか、手入力してください")
