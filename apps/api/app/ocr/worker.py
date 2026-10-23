"""OCR ワーカー(API とは別プロセス・別コンテナ)。`python -m app.ocr.worker` で起動する。"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.db import get_engine
from app.models.tables import OcrRun
from app.ocr import images, jobs
from app.ocr.parser import PARSER_VERSION, parse_receipt
from app.ocr.tokens import OcrAdapter

PREPROCESS_VERSION = "cv-0.1.0"
log = logging.getLogger("warilink.ocr")


class ImageRejected(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def run_pipeline(adapter: OcrAdapter, data: bytes, *, low_confidence: float) -> dict[str, Any]:
    """画像 → 前処理 → 認識 → 解析。DB に依存しない(CLI・評価からも使う)。"""
    from app.ocr.preprocess import decode, prepare  # noqa: PLC0415  (OpenCV はワーカーにのみ入れる)

    t0 = time.perf_counter()
    img = decode(data)
    if img is None:
        raise ImageRejected("IMAGE_UNREADABLE")
    prep = prepare(img)
    t1 = time.perf_counter()
    if prep.rejected:
        raise ImageRejected(prep.warnings[0]["code"])
    tokens = adapter.recognize(prep.image)
    t2 = time.perf_counter()
    draft = parse_receipt(tokens, low_confidence=low_confidence).to_json()
    t3 = time.perf_counter()
    draft["warnings"] = prep.warnings + draft["warnings"]
    draft["image_scale"] = prep.scale  # bbox は前処理後の座標。元画像へは 1/scale 倍
    return {
        "tokens": [t.to_json() for t in tokens], "draft": draft, "quality": prep.quality,
        "preprocess_ms": (t1 - t0) * 1000, "recognize_ms": (t2 - t1) * 1000, "parse_ms": (t3 - t2) * 1000,
    }


def process_one(sm: sessionmaker[Session], adapter: OcrAdapter, worker_id: str) -> bool:
    """ジョブを 1 件処理する。処理したら True。"""
    s = get_settings()
    with sm() as db:
        c = jobs.claim(db, worker_id)
    if c is None:
        return False
    info = adapter.model_info()
    started = datetime.now(UTC)
    t0 = time.perf_counter()
    result: dict[str, Any] | None = None
    error: str | None = None
    retryable = False
    try:
        data = images.image_path(c.storage_key).read_bytes()
        result = run_pipeline(adapter, data, low_confidence=s.ocr_low_confidence)
    except ImageRejected as e:
        error = e.code
    except FileNotFoundError:
        error = "IMAGE_MISSING"
    except Exception as e:  # noqa: BLE001  エンジン内部の失敗は再試行する(画像や OCR 結果はログに出さない)
        error, retryable = "OCR_ENGINE_ERROR", True
        log.warning("ocr job %s attempt %s failed: %s", c.job_id, c.attempt, type(e).__name__)
    elapsed = (time.perf_counter() - t0) * 1000

    with sm() as db:
        run = OcrRun(
            id=uuid.uuid4(), receipt_id=c.receipt_id, job_id=c.job_id, attempt=c.attempt, worker_id=worker_id,
            adapter=info.adapter, model_name=info.model_name, model_version=info.model_version,
            preprocess_version=PREPROCESS_VERSION, parser_version=PARSER_VERSION, params=info.params,
            quality=(result or {}).get("quality", {}), tokens=(result or {}).get("tokens"),
            draft=(result or {}).get("draft"), preprocess_ms=(result or {}).get("preprocess_ms"),
            recognize_ms=(result or {}).get("recognize_ms"), parse_ms=(result or {}).get("parse_ms"),
            elapsed_ms=elapsed, error_code=error, started_at=started,
        )
        if error is None:
            ok = jobs.succeed(db, c, worker_id)
        else:
            ok = jobs.fail(db, c, worker_id, error, retryable=retryable)
        if ok:
            db.add(run)
            db.commit()
        else:
            db.rollback()  # lease を失った: 別ワーカーの結果を優先し、この結果は保存しない
            log.warning("ocr job %s lease lost; result discarded", c.job_id)
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    s = get_settings()
    from app.ocr.paddle_adapter import PaddleOcrAdapter  # noqa: PLC0415

    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    adapter = PaddleOcrAdapter(det_model=s.ocr_det_model, rec_model=s.ocr_rec_model, cpu_threads=s.ocr_cpu_threads)
    info = adapter.model_info()
    sm = sessionmaker(get_engine(), expire_on_commit=False)
    started = datetime.now(UTC)
    with sm() as db:  # 受付開始を API に知らせてから ready を出す
        jobs.heartbeat(db, worker_id, info.model_name, info.model_version, started)
    log.info("ocr worker %s ready: %s (%s), init %.0f ms", worker_id, info.model_name, info.model_version,
             adapter.init_ms)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    def beat() -> None:
        while not stop.is_set():
            try:
                with sm() as db:
                    jobs.heartbeat(db, worker_id, info.model_name, info.model_version, started)
            except Exception as e:  # noqa: BLE001
                log.warning("heartbeat failed: %s", type(e).__name__)
            stop.wait(min(30, s.ocr_worker_heartbeat_ttl_seconds / 3))

    threading.Thread(target=beat, daemon=True).start()
    while not stop.is_set():
        try:
            busy = process_one(sm, adapter, worker_id)
        except Exception as e:  # noqa: BLE001  DB 切断などでもワーカーを止めない
            log.warning("worker loop error: %s", type(e).__name__)
            busy = False
        if not busy:
            stop.wait(s.ocr_worker_poll_seconds)
    log.info("ocr worker %s stopped", worker_id)


if __name__ == "__main__":
    main()
