import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import iap, security
from app.core.config import get_settings
from app.core.db import get_engine
from app.main import create_app
from app.models.tables import Identity, RateLimitEvent
from tests.test_iap import AUD, signed


@pytest.fixture
def iap_client(monkeypatch):
    private = ec.generate_private_key(ec.SECP256R1())
    monkeypatch.setattr(iap, "key_cache", iap.KeyCache())
    monkeypatch.setattr(iap, "_fetch_keys", lambda: {"test-key": private.public_key()})
    s = get_settings()
    monkeypatch.setattr(s, "rate_limit_source", "iap")
    monkeypatch.setattr(s, "iap_audience", AUD)
    monkeypatch.setattr(s, "rate_limits_enabled", True)
    monkeypatch.setattr(s, "rate_limit_secret", "r" * 32)
    monkeypatch.setattr(s, "rate_limit_overrides", {"guest_create:ip": (2, 3600)})
    return TestClient(create_app()), private, s


def request(client, token, **headers):
    return client.post("/api/v1/identity/guest", headers={"X-WariLink-CSRF": "1",
                       "X-Goog-IAP-JWT-Assertion": token, **headers})


def counts():
    with Session(get_engine()) as db:
        return (db.scalar(select(func.count()).select_from(Identity)),
                db.scalar(select(func.count()).select_from(RateLimitEvent)))


def test_separate_iap_subjects_and_no_unauthenticated_side_effects(iap_client, monkeypatch):
    c, private, _ = iap_client
    a = signed(private, claims={"sub": "subject-a", "email": "test-person@example.invalid"})
    b = signed(private, claims={"sub": "subject-b"})
    assert request(c, a).status_code == 201
    c.cookies.clear()
    assert request(c, a).status_code == 201
    c.cookies.clear()
    refreshed = signed(private, claims={"sub": "subject-a"})
    assert refreshed != a
    denied = request(c, refreshed, **{"X-Forwarded-For": "198.51.100.22"})
    assert denied.status_code == 429 and denied.json()["code"] == "RATE_LIMITED"
    assert denied.headers["retry-after"] == str(denied.json()["retry_after_seconds"])
    assert request(c, b).status_code == 201
    before = counts()
    missing = c.post("/api/v1/identity/guest", headers={"X-WariLink-CSRF": "1"})
    assert missing.status_code == 401 and missing.json()["code"] == "UNAUTHENTICATED"
    invalid = request(c, jwt.encode({"sub": "subject-a"}, "x" * 32, algorithm="HS256", headers={"kid": "test-key"}))
    assert invalid.status_code == 401
    header, payload, signature = b.split(".")
    altered = f"{header}.{payload}.{('A' if signature[0] != 'A' else 'B')}{signature[1:]}"
    assert request(c, altered).status_code == 401
    monkeypatch.setattr(iap, "key_cache", iap.KeyCache())
    monkeypatch.setattr(iap, "_fetch_keys", lambda: (_ for _ in ()).throw(OSError("private")))
    unavailable = request(c, b)
    assert unavailable.status_code == 503 and unavailable.json()["code"] == "IAP_UNAVAILABLE"
    assert counts() == before
    with Session(get_engine()) as db:
        keys = list(db.scalars(select(RateLimitEvent.key_hash)).all())
        assert security._key("iap", "subject-a") in keys
        assert security._key("iap", "subject-b") in keys
        assert all("subject" not in key and a not in key and b not in key for key in keys)
        from sqlalchemy import text

        rows = db.execute(text("SELECT row_to_json(x)::text FROM rate_limit_events x "
                               "UNION ALL SELECT row_to_json(y)::text FROM identities y")).scalars().all()
        assert all("subject-a" not in row and "test-person@example.invalid" not in row
                   and a not in row and b not in row for row in rows)


def test_global_and_csrf_still_apply(iap_client):
    c, private, settings = iap_client
    settings.rate_limit_overrides = {"guest_create:ip": (2, 3600), "guest_create:global": (2, 60)}
    a = signed(private, claims={"sub": "subject-a"})
    b = signed(private, claims={"sub": "subject-b"})
    assert c.post("/api/v1/identity/guest", headers={"X-Goog-IAP-JWT-Assertion": a}).status_code == 403
    assert c.get("/api/v1/groups", headers={"X-Goog-IAP-JWT-Assertion": a}).status_code == 401
    assert request(c, a).status_code == 201
    c.cookies.clear()
    assert request(c, b).status_code == 201
    c.cookies.clear()
    assert request(c, signed(private, claims={"sub": "subject-c"})).status_code == 429


def test_failure_event_uses_subject_and_preserves_other_scopes(iap_client):
    from types import SimpleNamespace

    from starlette.requests import Request

    _, _, _settings = iap_client
    request_obj = Request({"type": "http", "method": "GET", "path": "/", "headers": [],
                           "client": ("127.0.0.1", 1234), "state": {"iap_subject": "subject-a"}})
    identity = SimpleNamespace(id="identity-1")
    keys = security.rate_limit_keys(request_obj, identity=identity, token="invite-token")
    assert keys == {"ip": security._key("iap", "subject-a"), "global": "global",
                    "identity": security._key("identity", "identity-1"),
                    "token": security._key("token", "invite-token")}
    security.record_rate_event(request_obj, "passkey_fail", identity=identity)
    with Session(get_engine()) as db:
        rows = {(r.bucket, r.key_hash) for r in db.scalars(select(RateLimitEvent)).all()}
    assert ("passkey_fail:ip", keys["ip"]) in rows
    assert ("passkey_fail:identity", keys["identity"]) in rows


def test_identity_and_token_limits_remain_shared_across_iap_users(iap_client):
    from types import SimpleNamespace

    from starlette.requests import Request

    _, _, settings = iap_client
    settings.rate_limit_overrides = {"invite_access:identity": (1, 600), "invite_access:token": (2, 600)}

    def make_request(subject):
        return Request({"type": "http", "method": "GET", "path": "/", "headers": [],
                        "client": ("127.0.0.1", 1234), "state": {"iap_subject": subject}})

    one = SimpleNamespace(id="identity-1")
    two = SimpleNamespace(id="identity-2")
    three = SimpleNamespace(id="identity-3")
    security.rate_limit(make_request("subject-a"), "invite_access", identity=one, token="invite-token")
    with pytest.raises(security.RateLimited):
        security.rate_limit(make_request("subject-b"), "invite_access", identity=one, token="other-token")
    security.rate_limit(make_request("subject-b"), "invite_access", identity=two, token="invite-token")
    with pytest.raises(security.RateLimited):
        security.rate_limit(make_request("subject-c"), "invite_access", identity=three, token="invite-token")


def test_missing_verified_subject_never_falls_back_to_ip(iap_client):
    from starlette.requests import Request

    from app.core.errors import ApiError

    request_obj = Request({"type": "http", "method": "GET", "path": "/", "headers": [],
                           "client": ("127.0.0.1", 1234)})
    with pytest.raises(ApiError) as error:
        security.rate_limit_keys(request_obj)
    assert error.value.status == 401 and error.value.code == "UNAUTHENTICATED"


def invite_setup(iap_client):
    c, private, settings = iap_client
    settings.rate_limit_overrides = {}
    a = signed(private, claims={"sub": "invite-subject-a"})
    b = signed(private, claims={"sub": "invite-subject-b"})
    h = {"X-WariLink-CSRF": "1", "X-Goog-IAP-JWT-Assertion": a, "Idempotency-Key": str(uuid.uuid4())}
    assert request(c, a).status_code == 201
    owner_cookies = dict(c.cookies)
    g = c.post("/api/v1/groups", headers=h, json={"title": "invite", "my_name": "owner",
               "other_member_names": ["guest"]}).json()
    h["Idempotency-Key"] = str(uuid.uuid4())
    inv = c.post(f"/api/v1/groups/{g['public_id']}/invites", headers=h, json={}).json()["token"]
    c.cookies.clear()
    assert request(c, b).status_code == 201
    guest_cookies = dict(c.cookies)
    return c, settings, a, b, owner_cookies, guest_cookies, g, inv


def test_invite_http_subject_limits_and_auth(iap_client, caplog):
    from sqlalchemy import text

    c, settings, a, b, owner, guest, group, token = invite_setup(iap_client)
    settings.rate_limit_overrides = {"invite_access:ip": (1, 600)}
    def headers(jwt_value):
        return {"X-WariLink-CSRF": "1", "X-Goog-IAP-JWT-Assertion": jwt_value}
    c.cookies.clear()
    c.cookies.update(guest)
    preview = c.post("/api/v1/invites/preview", headers=headers(b), json={"token": token})
    assert preview.status_code == 200
    member = preview.json()["unclaimed_members"][0]["id"]
    denied = c.post("/api/v1/invites/join", headers=headers(b), json={"token": token, "claim_member_id": member})
    assert denied.status_code == 429 and denied.headers["retry-after"]
    c.cookies.clear()
    c.cookies.update(owner)
    assert c.post("/api/v1/invites/preview", headers=headers(a), json={"token": token}).status_code == 200
    settings.rate_limit_overrides = {}
    c.cookies.clear()
    c.cookies.update(guest)
    missing = c.post("/api/v1/invites/join", headers={"X-WariLink-CSRF": "1"}, json={"token": token})
    invalid = c.post("/api/v1/invites/join", headers=headers("invalid-jwt"), json={"token": token})
    csrf = c.post("/api/v1/invites/join", headers={"X-Goog-IAP-JWT-Assertion": b}, json={"token": token})
    assert (missing.status_code, invalid.status_code, csrf.status_code) == (401, 401, 403)
    with Session(get_engine()) as db:
        assert db.execute(text("SELECT uses FROM invitations")).scalar_one() == 0
        assert db.execute(text("SELECT count(*) FROM memberships WHERE identity_id IS NOT NULL")).scalar_one() == 1
    joined = c.post("/api/v1/invites/join", headers=headers(b), json={"token": token, "claim_member_id": member})
    assert joined.status_code == 200 and joined.json()["public_id"] == group["public_id"]
    for response in (preview, denied, missing, invalid, csrf, joined):
        assert token not in response.text and a not in response.text and b not in response.text
    with Session(get_engine()) as db:
        for table in ("identities", "invitations", "memberships", "rate_limit_events", "audit_events"):
            rows = str(db.execute(text(f"SELECT * FROM {table}")).all())
            assert all(secret not in rows for secret in (a, b, token, "invite-subject-a", "invite-subject-b"))
    assert all(secret not in caplog.text for secret in (a, b, token, "invite-subject-a", "invite-subject-b"))


def test_invite_http_identity_token_and_miss_limits(iap_client):
    c, settings, a, b, owner, guest, _, token = invite_setup(iap_client)
    def preview(jwt_value, value):
        return c.post("/api/v1/invites/preview", headers={"X-WariLink-CSRF": "1",
                      "X-Goog-IAP-JWT-Assertion": jwt_value}, json={"token": value})
    settings.rate_limit_overrides = {"invite_access:identity": (1, 600)}
    assert preview(b, token).status_code == 200
    assert preview(a, "different-token").status_code == 429  # same session, different subject
    settings.rate_limit_overrides = {"invite_access:token": (2, 600)}
    c.cookies.clear()
    c.cookies.update(owner)
    assert preview(a, token).status_code == 200
    assert preview(a, token).status_code == 429
    settings.rate_limit_overrides = {"invite_miss:ip": (1, 600)}
    assert preview(a, "invalid-token").status_code == 404
    assert preview(a, token).status_code == 429
    c.cookies.clear()
    c.cookies.update(guest)
    assert preview(b, token).status_code == 200
