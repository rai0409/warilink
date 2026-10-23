"""Passkey(WebAuthn)の登録・ゲスト昇格・別端末ログイン・失効(実 PostgreSQL、ソフトウェア認証器で実署名)。"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.core.db import get_engine
from app.main import app
from app.models.tables import WebauthnChallenge

from .conftest import API, User, equal_expense, make_group
from .webauthn_soft import UP, SoftAuthenticator

H = {"X-WariLink-CSRF": "1"}


def register(u: User, auth: SoftAuthenticator, **create_kw):
    o = u.post("/auth/passkeys/register/options", json={"nickname": "テスト端末"})
    assert o.status_code == 200, o.text
    body = o.json()
    return u.post("/auth/passkeys/register/verify",
                  json={"flow_id": body["flow_id"], "credential": auth.create(body["options"], **create_kw)})


def device() -> TestClient:
    """Cookie の無い別の端末(ゲストも作っていない)。"""
    return TestClient(app, headers=H)


def login(c: TestClient, auth: SoftAuthenticator, merge: bool = False, **get_kw):
    o = c.post(f"{API}/auth/passkeys/login/options")
    assert o.status_code == 200, o.text
    body = o.json()
    return c.post(f"{API}/auth/passkeys/login/verify",
                  json={"flow_id": body["flow_id"], "credential": auth.get(body["options"], **get_kw),
                        "merge_current_guest": merge})


def test_guest_upgrade_keeps_groups_and_rotates_session(user_factory):
    a = user_factory()
    g, ids = make_group(a)
    pid = g["public_id"]
    a.post(f"/groups/{pid}/expenses", json=equal_expense(ids["Aさん"], 1200, list(ids.values())))
    before = a.get("/identity/me").json()
    assert before["kind"] == "guest" and before["passkey_count"] == 0
    old_token = a.c.cookies.get("wl_session")
    auth = SoftAuthenticator()

    r = register(a, auth)
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["kind"] == "registered" and st["assurance_level"] == "passkey" and st["passkey_count"] == 1
    assert st["identity_id"] == before["identity_id"]  # 同じ identity のまま昇格
    assert a.c.cookies.get("wl_session") != old_token  # セッションを作り直した
    # 古いトークンは失効(セッション固定の防止)
    stale = TestClient(app, headers=H, cookies={"wl_session": old_token})
    assert stale.get(f"{API}/identity/me").json() is None
    assert stale.get(f"{API}/groups/{pid}").status_code == 401
    # グループ・支出はそのまま
    assert a.get(f"/groups/{pid}").status_code == 200
    assert len(a.get(f"/groups/{pid}/expenses").json()) == 1

    # 別の端末で Passkey ログイン → 同じグループが見える
    other = device()
    r = login(other, auth)
    assert r.status_code == 200, r.text
    assert r.json()["identity_id"] == before["identity_id"]
    assert other.get(f"{API}/groups/{pid}").status_code == 200


def test_challenge_is_single_use_and_expires(user_factory):
    a = user_factory()
    auth = SoftAuthenticator()
    o = a.post("/auth/passkeys/register/options", json={}).json()
    cred = auth.create(o["options"])
    r = a.post("/auth/passkeys/register/verify", json={"flow_id": o["flow_id"], "credential": cred})
    assert r.status_code == 200
    # 同じ flow・同じ応答の再送(リプレイ)は拒否
    r = a.post("/auth/passkeys/register/verify", json={"flow_id": o["flow_id"], "credential": cred})
    assert r.status_code == 400 and r.json()["code"] == "PASSKEY_CHALLENGE_INVALID"

    # 期限切れの challenge
    c = device()
    o = c.post(f"{API}/auth/passkeys/login/options").json()
    with get_engine().begin() as conn:
        conn.execute(update(WebauthnChallenge).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    r = c.post(f"{API}/auth/passkeys/login/verify",
               json={"flow_id": o["flow_id"], "credential": auth.get(o["options"])})
    assert r.status_code == 400 and r.json()["code"] == "PASSKEY_CHALLENGE_INVALID"

    # 以前のログイン応答を新しい flow に流用(challenge が違う)
    o1 = c.post(f"{API}/auth/passkeys/login/options").json()
    old = auth.get(o1["options"])
    r = c.post(f"{API}/auth/passkeys/login/verify", json={"flow_id": o1["flow_id"], "credential": old})
    assert r.status_code == 200
    o2 = c.post(f"{API}/auth/passkeys/login/options").json()
    r = c.post(f"{API}/auth/passkeys/login/verify", json={"flow_id": o2["flow_id"], "credential": old})
    assert r.status_code == 400


@pytest.mark.parametrize("kw", [
    {"origin": "https://evil.example"},  # 偽の Origin
    {"rp_id": "evil.example"},  # 別の RP
    {"flags": UP | 0x40},  # ユーザー検証(生体認証・PIN)なし
    {"challenge": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"},  # 別の challenge
])
def test_registration_rejects_wrong_origin_rp_uv_challenge(user_factory, kw):
    a = user_factory()
    r = register(a, SoftAuthenticator(), **kw)
    assert r.status_code == 400 and r.json()["code"] == "PASSKEY_VERIFICATION_FAILED"
    assert a.get("/identity/me").json()["kind"] == "guest"


@pytest.mark.parametrize("kw", [{"origin": "https://evil.example"}, {"rp_id": "evil.example"}, {"flags": UP}])
def test_login_rejects_wrong_origin_rp_uv(user_factory, kw):
    a = user_factory()
    auth = SoftAuthenticator()
    assert register(a, auth).status_code == 200
    r = login(device(), auth, **kw)
    assert r.status_code == 400 and r.json()["code"] == "PASSKEY_VERIFICATION_FAILED"


def test_registration_challenge_cannot_be_used_by_another_user(user_factory):
    a, b = user_factory(), user_factory()
    o = a.post("/auth/passkeys/register/options", json={}).json()
    r = b.post("/auth/passkeys/register/verify",
               json={"flow_id": o["flow_id"], "credential": SoftAuthenticator().create(o["options"])})
    assert r.status_code == 400 and b.get("/identity/me").json()["kind"] == "guest"


def test_sign_count_regression_and_user_handle_mismatch_rejected(user_factory):
    a = user_factory()
    auth = SoftAuthenticator()
    assert register(a, auth).status_code == 200
    assert login(device(), auth).status_code == 200
    assert login(device(), auth).status_code == 200
    # 署名回数が戻った(認証器の複製の疑い)
    auth.sign_count -= 1
    assert login(device(), auth, bump=False).status_code == 400
    auth.sign_count += 5
    # user handle が本人のものと違う
    assert login(device(), auth, user_handle=b"x" * 32).status_code == 400
    # 登録されていない(紛失・別サービスの)Passkey は同じ応答
    r = login(device(), SoftAuthenticator(), user_handle=b"y" * 32)
    assert r.status_code == 400 and r.json()["code"] == "PASSKEY_VERIFICATION_FAILED"


def test_multiple_passkeys_revoke_and_idor(user_factory):
    a, other = user_factory(), user_factory()
    k1, k2 = SoftAuthenticator(), SoftAuthenticator()
    assert register(a, k1).status_code == 200
    o = a.post("/auth/passkeys/register/options", json={}).json()
    assert [c["id"] for c in o["options"]["excludeCredentials"]]  # 登録済みは二重登録させない
    r = a.post("/auth/passkeys/register/verify", json={"flow_id": o["flow_id"], "credential": k2.create(o["options"])})
    assert r.json()["passkey_count"] == 2
    keys = a.get("/auth/passkeys").json()
    assert len(keys) == 2 and all("public_key" not in k and "credential_id" not in k for k in keys)

    # 他人の Passkey は削除できない(存在も見せない)
    assert other.delete(f"/auth/passkeys/{keys[0]['id']}").status_code == 404
    assert a.delete(f"/auth/passkeys/{keys[0]['id']}").json()["passkey_count"] == 1
    assert login(device(), k1).status_code == 400  # 失効した Passkey ではログインできない
    assert login(device(), k2).status_code == 200
    r = a.delete(f"/auth/passkeys/{keys[1]['id']}")
    assert r.status_code == 409 and r.json()["code"] == "LAST_PASSKEY"


def test_logout_and_revoke_other_sessions(user_factory):
    a = user_factory()
    r = a.post("/auth/logout")
    assert r.status_code == 409 and r.json()["code"] == "GUEST_CANNOT_LOGOUT"
    auth = SoftAuthenticator()
    assert register(a, auth).status_code == 200
    phone, laptop = device(), device()
    assert login(phone, auth).status_code == 200
    assert login(laptop, auth).status_code == 200
    r = laptop.post(f"{API}/auth/sessions/revoke-others")
    assert r.status_code == 200
    assert phone.get(f"{API}/identity/me").json() is None
    assert a.get("/identity/me").json() is None
    assert laptop.get(f"{API}/identity/me").json()["kind"] == "registered"
    r = laptop.post(f"{API}/auth/logout")
    assert r.status_code == 204 and laptop.get(f"{API}/identity/me").json() is None


def test_csrf_required_on_passkey_endpoints(user_factory):
    a = user_factory()
    no_header = TestClient(app, cookies=dict(a.c.cookies))
    for path in ("/auth/passkeys/register/options", "/auth/passkeys/login/options", "/auth/logout"):
        r = no_header.post(f"{API}{path}", json={})
        assert r.status_code == 403 and r.json()["code"] == "CSRF_REJECTED", path


def test_login_can_merge_guest_groups_when_chosen(user_factory):
    a = user_factory()
    auth = SoftAuthenticator()
    assert register(a, auth).status_code == 200
    ga, _ = make_group(a, title="登録済みの旅行")
    b = user_factory()  # 別の端末でゲストとして作ったグループ
    gb, _ = make_group(b, title="ゲストで作った飲み会")
    o = b.post("/auth/passkeys/login/options").json()
    r = b.post("/auth/passkeys/login/verify",
               json={"flow_id": o["flow_id"], "credential": auth.get(o["options"]), "merge_current_guest": True})
    assert r.status_code == 200, r.text
    assert r.json()["merged_groups"] == 1 and r.json()["unmerged_groups"] == 0
    assert b.get(f"/groups/{gb['public_id']}").status_code == 200
    assert b.get(f"/groups/{ga['public_id']}").status_code == 200
    assert a.get(f"/groups/{gb['public_id']}").status_code == 200  # 登録済みアカウントの別セッションからも見える
