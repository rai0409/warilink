from app.core.config import get_settings

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
