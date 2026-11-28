"""固定招待 API の秘密保持と廃止経路の回帰。"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import get_engine
from app.main import app

from .conftest import API, make_group


@pytest.fixture
def limits(monkeypatch):
    def set_(**overrides):
        monkeypatch.setenv("WARILINK_RATE_LIMITS_ENABLED", "true")
        monkeypatch.setenv("WARILINK_RATE_LIMIT_OVERRIDES",
                           json.dumps({k.replace("__", ":"): list(v) for k, v in overrides.items()}))
        get_settings.cache_clear()

    yield set_
    get_settings.cache_clear()


def test_invite_body_and_legacy_paths(user_factory, caplog):
    owner, guest = user_factory(), user_factory()
    group, ids = make_group(owner)
    token = owner.post(f"/groups/{group['public_id']}/invites", json={"max_uses": 1}).json()["token"]
    preview = guest.post("/invites/preview", json={"token": token})
    assert preview.status_code == 200
    assert token not in preview.text and token not in caplog.text
    assert preview.headers["cache-control"] == "no-store"
    assert preview.headers["referrer-policy"] == "no-referrer"
    assert guest.get(f"/invites/{token}").status_code == 404
    legacy = guest.post(f"/invites/{token}/join", json={"claim_member_id": ids["Bさん"]})
    assert legacy.status_code in (404, 405) and token not in legacy.text
    assert not next(m for m in owner.get(f"/groups/{group['public_id']}").json()["members"]
                    if m["id"] == ids["Bさん"])["claimed"]
    assert guest.post("/invites/join", json={"token": token, "claim_member_id": ids["Bさん"]}).status_code == 200
    assert token not in caplog.text
    with get_engine().connect() as conn:
        for table in ("invitations", "audit_events", "rate_limit_events"):
            rows = conn.execute(text(f"SELECT * FROM {table}")).all()
            assert token not in str(rows)


def test_invalid_inputs_and_auth_do_not_reflect_token(user_factory, caplog):
    guest = user_factory()
    canary = "canary-secret-invite-token"
    for body in ({}, {"token": 123}, {"token": canary * 30}):
        r = guest.post("/invites/preview", json=body)
        assert r.status_code == 422 and canary not in r.text
    invalid = guest.post("/invites/preview", json={"token": canary})
    assert invalid.status_code == 404 and invalid.json()["code"] == "INVITE_INVALID"
    assert canary not in invalid.text
    with TestClient(app) as anonymous:
        assert anonymous.post(f"{API}/invites/preview", json={"token": canary}).status_code == 403
        r = anonymous.post(f"{API}/invites/preview", json={"token": canary}, headers={"X-WariLink-CSRF": "1"})
        assert r.status_code == 401 and canary not in r.text
    denied = guest.post("/invites/preview", json={"token": canary}, headers={"Origin": "https://invalid.example"})
    assert denied.status_code == 403 and denied.json()["code"] == "CSRF_REJECTED"
    assert canary not in denied.text and canary not in caplog.text


def test_rate_limit_and_internal_error_hide_canary(user_factory, limits, monkeypatch, caplog):
    from app.modules.groups import router as groups

    guest = user_factory()
    canary = "synthetic-canary-token-for-errors"
    limits(invite_access__ip=(1, 600))
    assert guest.post("/invites/preview", json={"token": canary}).status_code == 404
    limited = guest.post("/invites/preview", json={"token": canary})
    assert limited.status_code == 429 and "retry-after" in limited.headers
    assert canary not in limited.text and canary not in caplog.text
    limits(invite_access__ip=(100, 600))

    def fail(*args, **kwargs):
        raise RuntimeError(canary)

    monkeypatch.setattr(groups, "_valid_invite", fail)
    client = TestClient(app, raise_server_exceptions=False, headers={"X-WariLink-CSRF": "1"})
    client.cookies = guest.c.cookies
    failed = client.post(f"{API}/invites/preview", json={"token": canary})
    assert failed.status_code == 500
    assert failed.headers["cache-control"] == "no-store"
    assert failed.headers["referrer-policy"] == "no-referrer"
    assert canary not in failed.text and canary not in caplog.text


def test_legacy_problem_instance_redacts_token():
    from starlette.requests import Request

    from app.core.errors import _problem

    canary = "legacy-canary-token"
    request = Request({"type": "http", "method": "GET", "path": f"/api/v1/invites/{canary}",
                       "headers": [], "query_string": b"", "scheme": "http", "server": ("test", 80)})
    response = _problem(request, 401, "UNAUTHENTICATED", "認証を確認できませんでした")
    assert canary.encode() not in response.body
    assert b"[redacted]" in response.body


@pytest.mark.parametrize("path", ["preview", "join"])
@pytest.mark.parametrize("kind", ["runtime", "invariant", "money"])
def test_internal_failure_logs_fixed_event(user_factory, monkeypatch, caplog, path, kind):
    from app.core import errors
    from app.domain.money import InvariantViolation, MoneyError
    from app.modules.groups import router as groups

    # Alembic の fileConfig が既存 logger を無効化するため、このテストでは捕捉を再有効化する。
    monkeypatch.setattr(errors.log, "disabled", False)

    guest = user_factory()
    canary = "sensitive-invite-canary"
    def fail(*args, **kwargs):
        if kind == "invariant":
            raise InvariantViolation(canary)
        if kind == "money":
            raise MoneyError("INVALID", canary, input=canary)
        raise RuntimeError(canary)

    monkeypatch.setattr(groups, "_valid_invite", fail)
    body = {"token": canary, "new_member_name": "test"}
    response = guest.post(f"/invites/{path}", json=body, headers={"X-Request-ID": "untrusted-header"})
    assert response.status_code == 500
    assert response.headers["cache-control"] == "no-store"
    assert canary not in response.text and canary not in caplog.text
    records = [r for r in caplog.records if r.name == "warilink" and "invite_internal_failure" in r.message]
    assert len(records) == 1 and records[0].message == f"invite_internal_failure path=/api/v1/invites/{path}"
    assert records[0].exc_info is None and "untrusted-header" not in records[0].message
