from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_engine
from app.ocr import jobs

from .conftest import User


def test_plans_come_from_settings_and_are_marked_unavailable(user_factory, monkeypatch):
    monkeypatch.setenv("WARILINK_OCR_MONTHLY_SCAN_CAP", "12")
    get_settings.cache_clear()
    try:
        r = User().get("/plans")
    finally:
        get_settings.cache_clear()
    assert r.status_code == 200
    body = r.json()
    assert body["ocr_free_monthly_scans"] == 3
    assert body["ocr_monthly_scan_cap"] == 12  # 定数ではなく設定値
    # Phase 2 時点では OCR・広告・課金はいずれも未提供(画面で「準備中」と表示する)
    assert body["ocr_available"] is False and body["ads_available"] is False and body["billing_available"] is False
    pro = next(p for p in body["plans"] if p["id"] == "pro")
    assert pro["available"] is False


def _plans_with(monkeypatch, **flags: bool) -> dict:
    for name, value in flags.items():
        monkeypatch.setenv(f"WARILINK_FEATURE_{name.upper()}", str(value).lower())
    get_settings.cache_clear()
    try:
        r = User().get("/plans")
    finally:
        get_settings.cache_clear()
    assert r.status_code == 200
    return r.json()


FLAG_COMBINATIONS = [(True, True), (True, False), (False, True), (False, False)]


@pytest.mark.parametrize(("passkeys", "payment_destinations"), FLAG_COMBINATIONS)
def test_payment_destinations_available_requires_passkeys_too(monkeypatch, passkeys, payment_destinations):
    body = _plans_with(monkeypatch, passkeys=passkeys, payment_destinations=payment_destinations)
    assert body["passkeys_available"] is passkeys
    assert body["payment_destinations_available"] is (passkeys and payment_destinations)


def test_ocr_available_requires_feature_flag_even_with_live_worker(monkeypatch):
    with Session(get_engine()) as db:
        jobs.heartbeat(db, "test-worker", "fixed", "0", datetime.now(UTC))
    assert _plans_with(monkeypatch, ocr=True)["ocr_available"] is True
    assert _plans_with(monkeypatch, ocr=False)["ocr_available"] is False


def test_default_v0_plans_offer_no_optional_features(monkeypatch):
    for name in ("PASSKEYS", "PAYMENT_DESTINATIONS", "OCR"):
        monkeypatch.delenv(f"WARILINK_FEATURE_{name}", raising=False)
    get_settings.cache_clear()
    try:
        body = User().get("/plans").json()
    finally:
        get_settings.cache_clear()
    assert body["passkeys_available"] is False and body["payment_destinations_available"] is False
    assert body["ocr_available"] is False and body["ads_available"] is False and body["billing_available"] is False
    assert {p["id"] for p in body["plans"]} >= {"free"}
