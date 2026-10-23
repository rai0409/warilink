"""PostgreSQL をキューとして使う OCR ジョブ管理。

- 取得: FOR UPDATE SKIP LOCKED で 1 件ずつ。lease(期限)付きで running にする。
- ワーカーが途中で落ちた場合: lease 期限切れのジョブを別のワーカーが再取得する。
- 完了・失敗の書き込みは lease を持っているワーカーだけが行える(古いワーカーの遅れた書き込みを無視する)。
- 最大試行回数を超えたら dead にし、利用枠を返還して手入力へ案内する。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.tables import OcrWorkerHeartbeat, Receipt, ReceiptJob
from app.ocr import quota


def now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Claimed:
    job_id: uuid.UUID
    receipt_id: uuid.UUID
    attempt: int
    storage_key: str


def claim(db: Session, worker_id: str) -> Claimed | None:
    s = get_settings()
    row = db.execute(text("""
        UPDATE receipt_jobs SET state = 'running', attempts = attempts + 1, lease_owner = :worker,
               lease_expires_at = now() + make_interval(secs => :lease)
         WHERE id = (
               SELECT id FROM receipt_jobs
                WHERE (state = 'queued' AND available_at <= now())
                   OR (state = 'running' AND lease_expires_at < now())
                ORDER BY available_at
                LIMIT 1
                FOR UPDATE SKIP LOCKED)
        RETURNING id, receipt_id, attempts, max_attempts
    """), {"worker": worker_id, "lease": s.ocr_job_lease_seconds}).one_or_none()
    if row is None:
        db.commit()
        return None
    job_id, receipt_id, attempts, max_attempts = row
    if attempts > max_attempts:
        # 処理中に毎回落ちる画像(ワーカーごと異常終了する等)は打ち切る
        _give_up(db, job_id, receipt_id, "WORKER_CRASHED")
        db.commit()
        return None
    rec = db.get(Receipt, receipt_id)
    assert rec is not None
    rec.state = "processing"
    db.commit()
    return Claimed(job_id, receipt_id, attempts, rec.storage_key)


def _owned(db: Session, job_id: uuid.UUID, worker_id: str) -> ReceiptJob | None:
    job = db.execute(select(ReceiptJob).where(ReceiptJob.id == job_id).with_for_update()).scalar_one()
    if job.state != "running" or job.lease_owner != worker_id:
        return None  # lease を失った(期限切れで別ワーカーが取得した)
    return job


def succeed(db: Session, c: Claimed, worker_id: str) -> bool:
    job = _owned(db, c.job_id, worker_id)
    if job is None:
        return False
    job.state, job.finished_at, job.lease_owner, job.lease_expires_at = "succeeded", now(), None, None
    rec = db.get(Receipt, c.receipt_id)
    assert rec is not None
    rec.state, rec.failure_code = "draft_ready", None
    return True


def fail(db: Session, c: Claimed, worker_id: str, code: str, *, retryable: bool) -> bool:
    s = get_settings()
    job = _owned(db, c.job_id, worker_id)
    if job is None:
        return False
    job.last_error_code = code
    if retryable and job.attempts < job.max_attempts:
        job.state, job.lease_owner, job.lease_expires_at = "queued", None, None
        job.available_at = now() + timedelta(seconds=s.ocr_job_retry_backoff_seconds * job.attempts)
        rec = db.get(Receipt, c.receipt_id)
        assert rec is not None
        rec.state = "uploaded"
        return True
    _give_up(db, c.job_id, c.receipt_id, code, terminal_state="dead" if retryable else "failed")
    return True


def _give_up(db: Session, job_id: uuid.UUID, receipt_id: uuid.UUID, code: str, terminal_state: str = "dead") -> None:
    db.execute(update(ReceiptJob).where(ReceiptJob.id == job_id).values(
        state=terminal_state, finished_at=now(), lease_owner=None, lease_expires_at=None, last_error_code=code))
    db.execute(update(Receipt).where(Receipt.id == receipt_id).values(state="failed", failure_code=code))
    # 読み取り結果を返せなかった場合は枠を返す(画像不良・内部エラーとも)
    quota.refund(db, receipt_id, code)


def heartbeat(db: Session, worker_id: str, model_name: str, model_version: str, started_at: datetime) -> None:
    t = now()
    db.execute(insert(OcrWorkerHeartbeat).values(
        worker_id=worker_id, model_name=model_name, model_version=model_version, started_at=started_at, last_seen_at=t,
    ).on_conflict_do_update(index_elements=["worker_id"], set_={"last_seen_at": t, "model_name": model_name,
                                                                "model_version": model_version}))
    db.commit()


def worker_available(db: Session) -> bool:
    s = get_settings()
    if not s.feature_ocr:
        return False
    cutoff = now() - timedelta(seconds=s.ocr_worker_heartbeat_ttl_seconds)
    return db.execute(select(OcrWorkerHeartbeat.worker_id).where(OcrWorkerHeartbeat.last_seen_at >= cutoff)
                      .limit(1)).first() is not None
