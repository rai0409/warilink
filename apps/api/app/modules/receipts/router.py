from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import ApiError, not_found
from app.core.security import current_identity, load_group_access, parse_uuid
from app.models.tables import AuditEvent, Group, Identity, OcrRun, Receipt, ReceiptJob
from app.ocr import images, jobs, quota

# multipart の本文全体と、画像以外のフォーム項目を合わせた余裕
MULTIPART_OVERHEAD = 64 * 1024


class LimitedUploadRoute(APIRoute):
    """本文を読む(multipart を解析して一時ファイルに書く)前に Content-Length で大きさを確かめる。

    FastAPI は File 引数の解析をハンドラーより先に行うため、ハンドラー内の確認では遅い。
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def guarded(request: Request) -> Response:
            if request.method == "POST":
                limit = get_settings().receipt_max_bytes + MULTIPART_OVERHEAD
                length = request.headers.get("content-length")
                if length is None or not length.isdigit():
                    # chunked 送信などで長さが分からない本文は受け付けない
                    raise ApiError(411, "LENGTH_REQUIRED", "画像を送信できませんでした。もう一度選び直してください")
                if int(length) > limit:
                    raise ApiError(413, "IMAGE_TOO_LARGE",
                                   f"画像は{get_settings().receipt_max_bytes // (1024 * 1024)}MB以下にしてください")
            return await handler(request)

        return guarded


router = APIRouter(tags=["receipts"])
upload_router = APIRouter(tags=["receipts"], route_class=LimitedUploadRoute)

FAILURE_MESSAGE = {
    "IMAGE_UNREADABLE": "画像を読み込めませんでした。別の画像で試すか、手入力してください",
    "TOO_SMALL": "画像が小さすぎます。レシート全体を大きく写して撮り直すか、手入力してください",
    "IMAGE_MISSING": "画像が見つかりませんでした。もう一度アップロードしてください",
    "OCR_ENGINE_ERROR": "読み取りに失敗しました。時間をおいて再度試すか、手入力してください",
    "WORKER_CRASHED": "読み取りに失敗しました。この画像は手入力で登録してください",
}


class QuotaOut(BaseModel):
    period: str
    free: int
    cap: int
    used: int
    remaining: int


class ReceiptOut(BaseModel):
    id: str
    state: str  # uploaded | processing | draft_ready | failed | confirmed
    job_state: str | None
    attempts: int
    failure_message: str | None
    duplicate: bool = False
    image_url: str
    width: int
    height: int
    expense_id: str | None
    draft: dict[str, Any] | None  # state=draft_ready のときの下書き(確定値ではない)
    run: dict[str, Any] | None  # 計測情報(モデル名・処理時間)
    quota: QuotaOut


def _quota_out(st: quota.QuotaStatus) -> QuotaOut:
    return QuotaOut(period=st.period, free=st.free, cap=st.cap, used=st.used, remaining=st.remaining)


def receipt_out(db: Session, group: Group, r: Receipt, ident: Identity, duplicate: bool = False) -> ReceiptOut:
    job = db.execute(select(ReceiptJob).where(ReceiptJob.receipt_id == r.id)).scalar_one_or_none()
    run = None
    if r.state in ("draft_ready", "confirmed"):
        run = db.execute(select(OcrRun).where(OcrRun.receipt_id == r.id, OcrRun.error_code.is_(None))
                         .order_by(OcrRun.created_at.desc()).limit(1)).scalar_one_or_none()
    return ReceiptOut(
        id=str(r.id), state=r.state, job_state=job.state if job else None, attempts=job.attempts if job else 0,
        failure_message=FAILURE_MESSAGE.get(r.failure_code or "", "読み取りに失敗しました。手入力してください")
        if r.state == "failed" else None,
        duplicate=duplicate, image_url=f"/api/v1/groups/{group.public_id}/receipts/{r.id}/image",
        width=r.width, height=r.height, expense_id=str(r.expense_id) if r.expense_id else None,
        draft=run.draft if run else None,
        run={"model_name": run.model_name, "model_version": run.model_version, "parser_version": run.parser_version,
             "elapsed_ms": round(run.elapsed_ms), "recognize_ms": round(run.recognize_ms or 0)} if run else None,
        quota=_quota_out(quota.status(db, ident.id)),
    )


def load_receipt(db: Session, group: Group, receipt_id: str) -> Receipt:
    r = db.get(Receipt, parse_uuid(receipt_id))
    if r is None or r.group_id != group.id:
        raise not_found()
    return r


@router.get("/ocr/quota", response_model=QuotaOut)
def get_quota(db: Session = Depends(get_db), ident: Identity = Depends(current_identity)) -> QuotaOut:
    return _quota_out(quota.status(db, ident.id))


@upload_router.post("/groups/{public_id}/receipts", response_model=ReceiptOut, status_code=201)
def upload_receipt(public_id: str, file: UploadFile = File(...),
                   db: Session = Depends(get_db), ident: Identity = Depends(current_identity)) -> ReceiptOut:
    """安い確認から順に行い、重い画像処理(デコード・再エンコード)は最後にする。

    1. メンバー確認 → 2. バイト数 → 3. 同じ画像(枠を消費しない)→ 4. ワーカー稼働 → 5. 利用枠(ロックなし)
    → 6. 流量制限 → 7. ヘッダーの形式・寸法 → 8. デコード・EXIF 除去 → 9. 枠の確保(ロック)と制限の再確認 → 保存
    """
    s = get_settings()
    acc = load_group_access(db, public_id, ident)
    raw = file.file.read(s.receipt_max_bytes + 1)
    images.check_size(len(raw))
    sha = hashlib.sha256(raw).hexdigest()

    def existing() -> Receipt | None:
        return db.execute(select(Receipt).where(Receipt.group_id == acc.group.id, Receipt.original_sha256 == sha,
                                                Receipt.image_deleted_at.is_(None))).scalar_one_or_none()

    # 同じグループに同じ画像が既にあれば、それを返す(枠は消費しない)
    dup = existing()
    if dup is not None:
        return receipt_out(db, acc.group, dup, ident, duplicate=True)
    if not jobs.worker_available(db):
        raise ApiError(503, "OCR_UNAVAILABLE",
                       "現在、画像の読み取りを利用できません。レシートの内容を手入力してください")
    quota.precheck(db, ident.id)
    quota.check_limits(db, ident.id, acc.group.id)
    images.probe(raw)
    img = images.normalize_upload(raw)
    db.rollback()  # 事前確認の読み取りトランザクションを閉じ、ロックは短く持つ

    rid = uuid.uuid4()
    quota.reserve(db, ident.id)
    quota.check_limits(db, ident.id, acc.group.id, lock=True)
    key = images.save_image(rid, img.data)
    try:
        r = Receipt(id=rid, group_id=acc.group.id, uploaded_by=ident.id, original_sha256=img.original_sha256,
                    content_type=img.content_type, byte_size=len(img.data), width=img.width, height=img.height,
                    storage_key=key, state="uploaded")
        db.add(r)
        db.flush()
        quota.consume(db, ident.id, rid)
        db.add(ReceiptJob(receipt_id=rid, max_attempts=s.ocr_job_max_attempts))
        db.add(AuditEvent(actor_identity_id=ident.id, action="receipt.upload", target_type="receipt",
                          target_id=str(rid), safe_metadata={"bytes": len(img.data)}))
        db.commit()
    except IntegrityError:
        # 同じ画像が同時にアップロードされた: 先に登録された方を返す(枠は消費しない)
        db.rollback()
        images.delete_image(key)
        dup = existing()
        assert dup is not None
        return receipt_out(db, acc.group, dup, ident, duplicate=True)
    except Exception:
        db.rollback()
        images.delete_image(key)  # DB に載らなかった画像は残さない
        raise
    return receipt_out(db, acc.group, r, ident)


@router.get("/groups/{public_id}/receipts/{receipt_id}", response_model=ReceiptOut)
def get_receipt(public_id: str, receipt_id: str, db: Session = Depends(get_db),
                ident: Identity = Depends(current_identity)) -> ReceiptOut:
    acc = load_group_access(db, public_id, ident)
    return receipt_out(db, acc.group, load_receipt(db, acc.group, receipt_id), ident)


@router.get("/groups/{public_id}/receipts/{receipt_id}/image", response_class=FileResponse)
def get_receipt_image(public_id: str, receipt_id: str, db: Session = Depends(get_db),
                      ident: Identity = Depends(current_identity)):
    acc = load_group_access(db, public_id, ident)
    r = load_receipt(db, acc.group, receipt_id)
    path = images.image_path(r.storage_key)
    if r.image_deleted_at is not None or not path.exists():
        raise ApiError(410, "IMAGE_EXPIRED", "保存期間が過ぎたため、画像は削除されました")
    return FileResponse(path, media_type=r.content_type, headers={"Cache-Control": "private, no-store"})


# アップロードだけ本文の大きさを先に確かめるルートクラスを使う(main.py は router だけを読み込む)
router.include_router(upload_router)
