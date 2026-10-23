"""レシートのアップロード・OCR ジョブ・利用枠(実 PostgreSQL)。OCR エンジンはテスト用の固定アダプターを使う。"""

import io
import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image
from sqlalchemy import select, text, update
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.core.db import get_engine
from app.models.tables import OcrCorrection, OcrQuotaEntry, OcrRun, Receipt, ReceiptJob
from app.ocr import jobs
from app.ocr.tokens import ModelInfo, OcrToken
from app.ocr.worker import process_one

from .conftest import User, make_group

RECEIPT_TOKENS = [
    ("居酒屋 WARI", None), ("生ビール", "2¥1,300"), ("唐揚げ", "¥800"), ("合計", "¥2,100"),
]


class FixedAdapter:
    """毎回同じトークンを返す(エンジンの代わり)。fail=True で内部エラーを起こす。"""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def recognize(self, image):
        self.calls += 1
        if self.fail:
            raise RuntimeError("engine crashed")
        out = []
        for i, (left, right) in enumerate(RECEIPT_TOKENS):
            y = 20 + i * 40
            out.append(OcrToken(left, 0.99, (20, y, 200, y + 30)))
            if right:
                out.append(OcrToken(right, 0.99, (300, y, 420, y + 30)))
        return out

    def model_info(self):
        return ModelInfo("fixed-test", "fixed", "0")


def png(color=255, size=(400, 600), exif_gps=False) -> bytes:
    img = Image.new("RGB", size, (color, color, color))
    buf = io.BytesIO()
    if exif_gps:
        exif = Image.Exif()
        exif[0x010F] = "TestCamera"  # Make
        exif[0x8825] = {2: (35.0, 41.0, 0.0)}  # GPSInfo
        img.save(buf, format="JPEG", exif=exif)
    else:
        img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    monkeypatch.setenv("WARILINK_RECEIPT_STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("WARILINK_OCR_JOB_RETRY_BACKOFF_SECONDS", "0")
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture
def sm():
    return sessionmaker(get_engine(), expire_on_commit=False)


@pytest.fixture
def worker_online(sm):
    with sm() as db:
        jobs.heartbeat(db, "test-worker", "fixed", "0", datetime.now(UTC))


def upload(u: User, public_id: str, data: bytes, name="r.png"):
    return u.post(f"/groups/{public_id}/receipts", files={"file": (name, data, "image/png")})


def test_upload_requires_running_worker(user_factory):
    a = User()
    g, _ = make_group(a)
    r = upload(a, g["public_id"], png())
    assert r.status_code == 503 and r.json()["code"] == "OCR_UNAVAILABLE"
    assert a.get("/plans").json()["ocr_available"] is False


def test_full_flow_draft_then_confirm_records_corrections(user_factory, worker_online, sm):
    a = User()
    g, ids = make_group(a, names=("Bさん",))
    pid = g["public_id"]
    r = upload(a, pid, png())
    assert r.status_code == 201, r.text
    rec = r.json()
    assert rec["state"] == "uploaded" and rec["quota"]["remaining"] == 2

    assert process_one(sm, FixedAdapter(), "w1") is True
    rec = a.get(f"/groups/{pid}/receipts/{rec['id']}").json()
    assert rec["state"] == "draft_ready"
    draft = rec["draft"]
    assert [(i["name"], i["quantity"], i["line_minor"]) for i in draft["items"]] == [("生ビール", 2, 1300),
                                                                                     ("唐揚げ", 1, 800)]
    assert draft["total_minor"] == 2100 and rec["run"]["model_name"] == "fixed"

    # 利用者が金額を訂正して登録(読み取り結果は下書き。確定はエンジンで検算)
    items = [{"id": "ocr-i1", "name": "生ビール", "line_minor": 1300, "quantity": 2,
              "allocation": {"mode": "equal", "shares": [{"member_id": ids["Aさん"], "value": 1}]}},
             {"id": "ocr-i2", "name": "唐揚げ", "line_minor": 900, "quantity": 1,
              "allocation": {"mode": "equal", "shares": [{"member_id": ids["Bさん"], "value": 1}]}}]
    body = {"title": "居酒屋", "amount_minor": 2200, "payers": [{"member_id": ids["Aさん"], "amount_minor": 2200}],
            "split": {"mode": "itemized", "receipt": {"receipt_total_minor": 2200, "items": items}},
            "source_receipt_id": rec["id"], "review_elapsed_ms": 42000}
    e = a.post(f"/groups/{pid}/expenses", json=body)
    assert e.status_code == 201, e.text
    with sm() as db:
        r_row = db.get(Receipt, uuid.UUID(rec["id"]))
        assert r_row.state == "confirmed" and str(r_row.expense_id) == e.json()["id"]
        corr = {(c.field_path, c.error_class, c.label_status) for c in db.execute(select(OcrCorrection)).scalars()}
    assert corr == {("receipt_total_minor", "recognition", "proposed"),
                    ("items[ocr-i2].line_minor", "recognition", "proposed")}
    # 同じレシートからの二重登録はできない
    again = a.post(f"/groups/{pid}/expenses", json=body)
    assert again.status_code == 409 and again.json()["code"] == "RECEIPT_ALREADY_USED"


def test_quota_three_per_month_and_duplicate_is_free(user_factory, worker_online):
    a = User()
    g, _ = make_group(a)
    pid = g["public_id"]
    first = upload(a, pid, png(250))
    assert first.status_code == 201
    dup = upload(a, pid, png(250))  # 同じ画像
    assert dup.status_code == 201 and dup.json()["duplicate"] is True and dup.json()["id"] == first.json()["id"]
    assert dup.json()["quota"]["remaining"] == 2
    assert upload(a, pid, png(240)).status_code == 201
    assert upload(a, pid, png(230)).status_code == 201
    over = upload(a, pid, png(220))
    assert over.status_code == 429 and over.json()["code"] == "OCR_QUOTA_EXCEEDED"
    assert "手入力" in over.json()["detail"]
    assert a.get("/ocr/quota").json() == {"period": a.get("/ocr/quota").json()["period"], "free": 3, "cap": 10,
                                          "used": 3, "remaining": 0}


def test_concurrent_uploads_cannot_exceed_quota(user_factory, worker_online):
    a = User()
    g, _ = make_group(a)
    results = []

    def go(i):
        results.append(upload(a, g["public_id"], png(100 + i)).status_code)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [201, 201, 201, 429, 429, 429]


def test_engine_error_retries_then_dead_and_refunds_once(user_factory, worker_online, sm):
    a = User()
    g, _ = make_group(a)
    rid = upload(a, g["public_id"], png()).json()["id"]
    bad = FixedAdapter(fail=True)
    for _ in range(3):
        assert process_one(sm, bad, "w1")
    assert process_one(sm, bad, "w1") is False  # もう取得するジョブがない
    rec = a.get(f"/groups/{g['public_id']}/receipts/{rid}").json()
    assert rec["state"] == "failed" and rec["job_state"] == "dead" and rec["attempts"] == 3
    assert "手入力" in rec["failure_message"]
    assert rec["quota"]["remaining"] == 3  # 枠は返還済み
    with sm() as db:
        runs = db.execute(select(OcrRun).where(OcrRun.receipt_id == uuid.UUID(rid))).scalars().all()
        assert [r.error_code for r in runs] == ["OCR_ENGINE_ERROR"] * 3
        assert jobs.quota.refund(db, uuid.UUID(rid), "again") is False  # 二重返還しない
        db.commit()
        kinds = [k for (k,) in db.execute(select(OcrQuotaEntry.kind).where(
            OcrQuotaEntry.receipt_id == uuid.UUID(rid)))]
    assert sorted(kinds) == ["consume", "refund"]


def test_unreadable_quality_image_fails_without_retry_and_refunds(user_factory, worker_online, sm):
    a = User()
    g, _ = make_group(a)
    rid = upload(a, g["public_id"], png(size=(200, 250))).json()["id"]  # 小さすぎる
    adapter = FixedAdapter()
    assert process_one(sm, adapter, "w1")
    rec = a.get(f"/groups/{g['public_id']}/receipts/{rid}").json()
    assert rec["state"] == "failed" and rec["job_state"] == "failed" and adapter.calls == 0
    assert "小さすぎます" in rec["failure_message"] and rec["quota"]["remaining"] == 3


def test_crashed_worker_lease_expires_and_late_result_is_discarded(user_factory, worker_online, sm):
    a = User()
    g, _ = make_group(a)
    rid = upload(a, g["public_id"], png()).json()["id"]
    with sm() as db:
        c_old = jobs.claim(db, "crashed")  # 取得したまま落ちた想定
    assert c_old is not None
    with sm() as db:  # lease 期限切れにする
        db.execute(update(ReceiptJob).values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)))
        db.commit()
    assert process_one(sm, FixedAdapter(), "w2")
    with sm() as db:
        job = db.execute(select(ReceiptJob)).scalar_one()
        assert job.state == "succeeded" and job.attempts == 2
        # 古いワーカーの遅れた書き込みは無視される
        assert jobs.fail(db, c_old, "crashed", "OCR_ENGINE_ERROR", retryable=True) is False
        db.rollback()
    assert a.get(f"/groups/{g['public_id']}/receipts/{rid}").json()["state"] == "draft_ready"


def test_skip_locked_gives_each_worker_a_different_job(user_factory, worker_online, sm):
    a = User()
    g, _ = make_group(a)
    for i in range(2):
        assert upload(a, g["public_id"], png(10 + i)).status_code == 201
    with sm() as db1, sm() as db2:
        db1.execute(text("SELECT 1"))
        c1 = jobs.claim(db1, "w1")
        c2 = jobs.claim(db2, "w2")
    assert c1 and c2 and c1.job_id != c2.job_id


def test_upload_validation_and_exif_is_stripped(user_factory, worker_online, storage):
    a = User()
    g, _ = make_group(a)
    pid = g["public_id"]
    r = a.post(f"/groups/{pid}/receipts", files={"file": ("x.txt", b"not an image", "text/plain")})
    assert r.status_code == 415 and r.json()["code"] == "IMAGE_UNREADABLE"
    gif = io.BytesIO()
    Image.new("RGB", (400, 400)).save(gif, format="GIF")
    assert upload(a, pid, gif.getvalue()).json()["code"] == "IMAGE_TYPE_UNSUPPORTED"

    r = upload(a, pid, png(exif_gps=True), name="photo.jpg")
    assert r.status_code == 201
    saved = next(storage.glob("*.jpg"))
    with Image.open(saved) as im:
        assert len(im.getexif()) == 0  # 位置情報などのメタデータは保存しない
    img = a.get(f"/groups/{pid}/receipts/{r.json()['id']}/image")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    assert "no-store" in img.headers["cache-control"]


def test_too_large_upload_rejected(user_factory, worker_online, monkeypatch):
    monkeypatch.setenv("WARILINK_RECEIPT_MAX_BYTES", "1000")
    get_settings.cache_clear()
    a = User()
    g, _ = make_group(a)
    r = upload(a, g["public_id"], png())
    assert r.status_code == 413 and r.json()["code"] == "IMAGE_TOO_LARGE"


def test_non_member_cannot_see_receipt_or_image(user_factory, worker_online):
    a = User()
    g, _ = make_group(a)
    rid = upload(a, g["public_id"], png()).json()["id"]
    outsider = User()
    assert outsider.get(f"/groups/{g['public_id']}/receipts/{rid}").status_code == 404
    assert outsider.get(f"/groups/{g['public_id']}/receipts/{rid}/image").status_code == 404
    # 別グループの ID を指定しても見えない
    g2, _ = make_group(outsider, title="別")
    assert outsider.get(f"/groups/{g2['public_id']}/receipts/{rid}").status_code == 404


def test_receipt_from_another_group_cannot_be_linked(user_factory, worker_online, sm):
    a = User()
    g1, ids1 = make_group(a, names=())
    g2, ids2 = make_group(a, names=(), title="別")
    rid = upload(a, g1["public_id"], png()).json()["id"]
    process_one(sm, FixedAdapter(), "w1")
    m = ids2["Aさん"]
    body = {"title": "x", "amount_minor": 100, "payers": [{"member_id": m, "amount_minor": 100}],
            "split": {"mode": "equal", "shares": [{"member_id": m, "value": 1}]}, "source_receipt_id": rid}
    r = a.post(f"/groups/{g2['public_id']}/expenses", json=body)
    assert r.status_code == 422 and r.json()["code"] == "RECEIPT_NOT_FOUND"
