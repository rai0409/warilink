"""本番設定の fail-closed 検証(DB 不要)。"""

import pytest
from fastapi import Request

from app.core import config as cfg
from app.core.config import Settings, UnsafeProductionConfig
from app.core.errors import ApiError

SAFE = {
    "environment": "production",
    "cookie_secure": True,
    "allowed_origins": ["https://warilink.example"],
    "database_url": "postgresql+psycopg://app:s3cret-from-env@db.internal:5432/warilink",
}


def make(**over) -> Settings:
    return cfg.ensure_safe(Settings(_env_file=None, **{**SAFE, **over}))


def test_safe_production_is_accepted():
    s = make()
    assert s.cookie_secure and cfg.production_problems(s) == []
    assert not s.feature_passkeys and not s.feature_payment_destinations and not s.feature_ocr


@pytest.mark.parametrize("name", ["passkeys", "payment_destinations", "ocr"])
def test_optional_v0_features_are_fail_closed_by_default(name):
    with pytest.raises(ApiError) as e:
        cfg.require_feature(name)
    assert e.value.status == 503 and e.value.code == "FEATURE_DISABLED"


@pytest.mark.parametrize("name", [
    "guest_sessions", "groups", "manual_expenses", "itemized_allocation", "settlement", "settlement_confirmations",
])
def test_v0_features_are_available_by_default(name):
    cfg.require_feature(name)


@pytest.mark.parametrize("name", ["passkeys", "payment_destinations", "ocr"])
def test_optional_features_are_available_when_enabled(monkeypatch, name):
    monkeypatch.setenv(f"WARILINK_FEATURE_{name.upper()}", "true")
    cfg.get_settings.cache_clear()
    try:
        cfg.require_feature(name)
    finally:
        cfg.get_settings.cache_clear()


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"cookie_secure": False}, "COOKIE_SECURE"),
        ({"allowed_origins": []}, "ALLOWED_ORIGINS"),
        ({"allowed_origins": ["*"]}, "ワイルドカード"),
        ({"allowed_origins": ["http://warilink.example"]}, "https://"),
        ({"allowed_origins": ["https://localhost:3000"]}, "localhost"),
        ({"allowed_origins": ["https://127.0.0.1"]}, "localhost"),
        ({"allowed_origins": ["https://warilink.example/app"]}, "パス"),
        ({"allowed_origins": ["https://"]}, "https://"),
        ({"database_url": "postgresql+psycopg://warilink:warilink-dev-only@db:5432/warilink"}, "既定パスワード"),
        ({"database_url": "postgresql+psycopg://warilink:warilink@localhost:55432/warilink"}, "既定パスワード"),
        ({"feature_bank_provider": True}, "銀行連携"),
        ({"feature_ads": True}, "広告"),
        ({"feature_billing": True}, "課金"),
    ],
)
def test_unsafe_production_is_rejected(over, needle):
    with pytest.raises(UnsafeProductionConfig) as e:
        make(**over)
    assert needle in str(e.value)
    assert "s3cret-from-env" not in str(e.value)  # 秘密をエラーに出さない


def test_unknown_environment_is_rejected():
    with pytest.raises(UnsafeProductionConfig):
        cfg.ensure_safe(Settings(_env_file=None, environment="prod"))


@pytest.mark.parametrize("env", ["development", "test"])
def test_development_and_test_defaults_unchanged(env):
    s = Settings(_env_file=None, environment=env)
    assert s.cookie_secure is False and s.allowed_origins == ["http://localhost:3000"]
    assert s.session_cookie_name == "wl_session"


def test_env_vars_are_validated(monkeypatch):
    monkeypatch.setenv("WARILINK_ENVIRONMENT", "production")
    monkeypatch.setenv("WARILINK_COOKIE_SECURE", "false")
    monkeypatch.setenv("WARILINK_ALLOWED_ORIGINS", '["https://warilink.example"]')
    monkeypatch.setenv("WARILINK_DATABASE_URL", "postgresql+psycopg://app:s3cret-from-env@db/warilink")
    cfg.get_settings.cache_clear()
    try:
        with pytest.raises(UnsafeProductionConfig) as e:
            cfg.get_settings()  # アプリが使う経路で拒否される
        assert "COOKIE_SECURE" in str(e.value) and "s3cret-from-env" not in str(e.value)
    finally:
        cfg.get_settings.cache_clear()


def test_invalid_value_error_does_not_echo_secrets():
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as e:
        Settings(_env_file=None, database_url="postgresql://u:s3cret-from-env@h/x", session_ttl_days="abc")
    assert "s3cret-from-env" not in str(e.value)


def _req(method: str, headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": method, "headers": raw, "path": "/", "query_string": b""})


@pytest.fixture
def production(monkeypatch):
    monkeypatch.setattr("app.core.security.get_settings", lambda: make())


def test_production_csrf_requires_header_and_allowed_origin(production):
    from app.core.security import check_csrf

    ok = {"X-WariLink-CSRF": "1", "Origin": "https://warilink.example"}
    check_csrf(_req("POST", ok))
    check_csrf(_req("GET", {}))  # 安全なメソッドは対象外
    for headers in ({"Origin": "https://warilink.example"},  # 独自ヘッダーなし
                    {"X-WariLink-CSRF": "1"},  # Origin なし(本番のみ拒否)
                    {"X-WariLink-CSRF": "1", "Origin": "https://evil.example"},
                    {"X-WariLink-CSRF": "1", "Origin": "null"}):
        with pytest.raises(ApiError) as e:
            check_csrf(_req("POST", headers))
        assert e.value.status == 403 and e.value.code == "CSRF_REJECTED"


def test_development_csrf_allows_missing_origin(monkeypatch):
    from app.core.security import check_csrf

    monkeypatch.setattr("app.core.security.get_settings", lambda: Settings(_env_file=None, environment="development"))
    check_csrf(_req("POST", {"X-WariLink-CSRF": "1"}))
    with pytest.raises(ApiError):
        check_csrf(_req("POST", {"X-WariLink-CSRF": "1", "Origin": "https://evil.example"}))


def test_production_app_hides_docs(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main

    monkeypatch.setattr(main, "get_settings", lambda: make())
    c = TestClient(main.create_app())
    assert c.get("/api/v1/docs").status_code == 404
    assert c.get("/api/v1/openapi.json").status_code == 404
    assert c.get("/redoc").status_code == 404
