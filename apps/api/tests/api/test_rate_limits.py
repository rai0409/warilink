"""STEP3: 濫用対策の回数制限(app/core/security.py の rate_limit)。

接続元 IP は TestClient の client=(ip, port) で疑似的に変え、時刻は security._rate_clock を固定して
再現できるようにする。
"""

import importlib.util
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core import security
from app.core.config import get_settings
from app.core.db import get_engine
from app.main import app, create_app
from app.models.tables import Identity, RateLimitEvent

from .conftest import API, make_group
from .test_passkeys import register
from .webauthn_soft import SoftAuthenticator

H = {"X-WariLink-CSRF": "1"}
T0 = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)


@pytest.fixture
def limits(monkeypatch):
    """回数制限を有効にし、指定した上限で上書きする(テストの間だけ)。"""

    def set_(**overrides: tuple[int, int]):
        monkeypatch.setenv("WARILINK_RATE_LIMITS_ENABLED", "true")
        monkeypatch.setenv("WARILINK_RATE_LIMIT_OVERRIDES",
                           json.dumps({k.replace("__", ":"): list(v) for k, v in overrides.items()}))
        get_settings.cache_clear()

    yield set_
    get_settings.cache_clear()


@pytest.fixture
def clock(monkeypatch):
    now = {"t": T0}
    monkeypatch.setattr(security, "_rate_clock", lambda: now["t"])
    return now


def browser(ip: str = "203.0.113.5", application=app, **headers: str) -> TestClient:
    return TestClient(application, client=(ip, 50000), headers={**H, **headers})


def guest(c: TestClient):
    return c.post(f"{API}/identity/guest")


def identities() -> int:
    with Session(get_engine()) as db:
        return db.execute(select(func.count()).select_from(Identity)).scalar_one()


def assert_limited(r, max_retry: int):
    assert r.status_code == 429, r.text
    body = r.json()
    assert body["code"] == "RATE_LIMITED" and "しばらく待って" in body["detail"]
    retry = int(r.headers["Retry-After"])
    assert 1 <= retry <= max_retry and body["retry_after_seconds"] == retry


# ---------------------------------------------------------------- ゲスト(Cookie を消した作り直しを含む)

def test_guest_creation_is_limited_per_ip_and_rejection_changes_nothing(limits, clock):
    limits(guest_create__ip=(3, 3600))
    for _ in range(3):
        assert guest(browser()).status_code == 201  # 毎回 Cookie なし = Cookie を消して作り直したのと同じ
    before = identities()
    clock["t"] = T0 + timedelta(minutes=10)
    r = guest(browser())
    assert_limited(r, 3600)
    assert int(r.headers["Retry-After"]) == 3000  # 最初の記録から 1 時間後まで(固定時刻で正確に)
    assert identities() == before  # 拒否したときは何も作らない
    assert guest(browser("198.51.100.7")).status_code == 201  # 別の IP は影響を受けない
    clock["t"] = T0 + timedelta(hours=1, seconds=1)
    assert guest(browser()).status_code == 201  # 期間が過ぎれば元に戻る


def test_reusing_an_existing_session_is_not_counted(limits, clock):
    limits(guest_create__ip=(1, 3600))
    c = browser()
    assert guest(c).status_code == 201
    assert guest(c).status_code == 201  # 同じ Cookie の再利用は新規作成ではない


def test_global_cap_stops_distributed_guest_creation(limits, clock):
    limits(guest_create__ip=(100, 3600), guest_create__global=(5, 60))
    for i in range(5):
        assert guest(browser(f"192.0.2.{i + 1}")).status_code == 201
    assert_limited(guest(browser("192.0.2.200")), 60)  # IP を毎回変えても全体の上限で止まる


def test_limits_survive_restart(limits, clock):
    limits(guest_create__ip=(2, 3600))
    for _ in range(2):
        assert guest(browser()).status_code == 201
    get_settings.cache_clear()  # 別プロセスで起動し直したのと同じ(記録は DB にある)
    restarted = create_app()
    assert_limited(guest(browser(application=restarted)), 3600)


def test_parallel_requests_never_exceed_the_limit(limits, clock):
    limits(guest_create__ip=(5, 3600))
    with ThreadPoolExecutor(max_workers=12) as pool:
        codes = list(pool.map(lambda _: guest(browser()).status_code, range(12)))
    assert codes.count(201) == 5 and codes.count(429) == 7  # 同時に来ても 5 回だけ通る(advisory lock)
    with Session(get_engine()) as db:
        n = db.execute(select(func.count()).select_from(RateLimitEvent)
                       .where(RateLimitEvent.bucket == "guest_create:ip")).scalar_one()
    assert n == 5


# ---------------------------------------------------------------- 接続元の判定

def test_spoofed_forwarded_for_is_ignored_without_trusted_proxy(limits, clock):
    limits(guest_create__ip=(2, 3600))
    for i in range(2):
        assert guest(browser(**{"X-Forwarded-For": f"192.0.2.{i}"})).status_code == 201
    assert guest(browser(**{"X-Forwarded-For": "192.0.2.99"})).status_code == 429


def test_trusted_proxy_uses_rightmost_untrusted_hop(limits, clock, monkeypatch):
    limits(guest_create__ip=(1, 3600))
    monkeypatch.setattr(get_settings(), "trusted_proxies", ["10.0.0.0/8"])
    proxy = "10.0.0.1"
    assert guest(browser(proxy, **{"X-Forwarded-For": "192.0.2.1, 10.0.0.2"})).status_code == 201
    # 左端(利用者が自由に書ける部分)を変えても同じ利用者として数える
    assert guest(browser(proxy, **{"X-Forwarded-For": "9.9.9.9, 192.0.2.1, 10.0.0.2"})).status_code == 429
    assert guest(browser(proxy, **{"X-Forwarded-For": "192.0.2.2"})).status_code == 201


def test_ipv6_is_counted_per_64_and_no_raw_value_is_stored(limits, clock):
    limits(guest_create__ip=(1, 3600))
    assert security._ip_unit("2001:db8::1") == security._ip_unit("2001:db8::ffff:1")
    assert security._ip_unit("2001:db8::1") != security._ip_unit("2001:db8:0:1::1")
    assert security._ip_unit("::ffff:203.0.113.5") == "203.0.113.5"
    assert guest(browser("2001:db8::1")).status_code == 201
    assert guest(browser("2001:db8::abcd")).status_code == 429  # 末尾だけを変えても同じ /64
    with get_engine().connect() as c:
        dump = "\n".join(str(tuple(r)) for r in c.execute(text("SELECT * FROM rate_limit_events")))
    assert "2001:db8" not in dump and "203.0.113" not in dump


# ---------------------------------------------------------------- 招待

def test_invite_guessing_is_limited_without_revealing_valid_tokens(limits, clock, user_factory):
    owner = user_factory()
    g, _ = make_group(owner)
    token = owner.post(f"/groups/{g['public_id']}/invites", json={}).json()["token"]
    limits(invite_miss__ip=(3, 600))
    c = browser()
    assert guest(c).status_code == 201
    for i in range(3):
        assert c.get(f"{API}/invites/not-a-token-{i}").json()["code"] == "INVITE_INVALID"
    # 上限に達した後は、正しい token でも存在しない token でも同じ 429(token の有無が分からない)
    valid, invalid = c.get(f"{API}/invites/{token}"), c.get(f"{API}/invites/still-not-a-token")
    assert valid.status_code == invalid.status_code == 429
    assert valid.json()["code"] == invalid.json()["code"] == "RATE_LIMITED"
    other = browser("198.51.100.7")
    assert guest(other).status_code == 201
    assert other.get(f"{API}/invites/{token}").status_code == 200


def test_invite_access_is_limited_per_identity_and_per_token(limits, clock, user_factory):
    owner = user_factory()
    g, _ = make_group(owner)
    token = owner.post(f"/groups/{g['public_id']}/invites", json={}).json()["token"]
    limits(invite_access__identity=(2, 600), invite_access__token=(4, 600))
    c = browser("192.0.2.1")
    assert guest(c).status_code == 201
    assert c.get(f"{API}/invites/{token}").status_code == 200
    assert c.get(f"{API}/invites/{token}").status_code == 200
    # 同じ本人は IP を変えても止まる(Cookie を移す)
    moved = browser("192.0.2.2")
    moved.cookies = c.cookies
    assert moved.get(f"{API}/invites/{token}").status_code == 429
    # 1 つの token には、別の人・別の IP からでも全体で 4 回まで
    for ip in ("192.0.2.3", "192.0.2.4"):
        x = browser(ip)
        assert guest(x).status_code == 201
        assert x.get(f"{API}/invites/{token}").status_code == 200
    y = browser("192.0.2.5")
    assert guest(y).status_code == 201
    assert y.get(f"{API}/invites/{token}").status_code == 429


def test_join_rejection_leaves_membership_unchanged(limits, clock, user_factory):
    owner = user_factory()
    g, ids = make_group(owner)
    token = owner.post(f"/groups/{g['public_id']}/invites", json={}).json()["token"]
    limits(invite_access__ip=(1, 600))
    c = browser()
    assert guest(c).status_code == 201
    assert c.get(f"{API}/invites/{token}").status_code == 200
    r = c.post(f"{API}/invites/{token}/join", json={"claim_member_id": ids["Bさん"]},
               headers={"Idempotency-Key": "7d0a5d0e-0000-4000-8000-000000000001"})
    assert r.status_code == 429
    members = owner.get(f"/groups/{g['public_id']}").json()["members"]
    assert not next(m for m in members if m["id"] == ids["Bさん"])["claimed"]


def test_invite_creation_is_limited_per_identity(limits, clock, user_factory):
    owner = user_factory()
    g, _ = make_group(owner)
    limits(invite_create__identity=(2, 3600))
    for _ in range(2):
        assert owner.post(f"/groups/{g['public_id']}/invites", json={}).status_code == 201
    assert owner.post(f"/groups/{g['public_id']}/invites", json={}).status_code == 429


# ---------------------------------------------------------------- Passkey

def test_failed_passkey_logins_block_further_attempts_from_the_ip(limits, clock):
    limits(passkey_fail__ip=(2, 600))
    c = browser()
    for _ in range(2):
        o = c.post(f"{API}/auth/passkeys/login/options").json()
        cred = SoftAuthenticator().get(o["options"], user_handle=b"x" * 32)
        assert c.post(f"{API}/auth/passkeys/login/verify",
                      json={"flow_id": o["flow_id"], "credential": cred}).status_code == 400
    assert c.post(f"{API}/auth/passkeys/login/options").status_code == 429
    assert browser("198.51.100.7").post(f"{API}/auth/passkeys/login/options").status_code == 200


def test_passkey_challenge_requests_are_limited(limits, clock, user_factory):
    limits(passkey_login__ip=(2, 600), passkey_register__identity=(1, 600))
    c = browser()
    assert [c.post(f"{API}/auth/passkeys/login/options").status_code for _ in range(3)] == [200, 200, 429]
    u = user_factory()
    assert u.post("/auth/passkeys/register/options", json={}).status_code == 200
    assert u.post("/auth/passkeys/register/options", json={}).status_code == 429


def test_normal_passkey_registration_still_works_with_limits_on(limits, clock, user_factory):
    limits()  # 既定の上限で、通常の登録・ログインが止まらないこと
    u = user_factory()
    assert register(u, SoftAuthenticator()).status_code == 200


# ---------------------------------------------------------------- 保存量・設定・バックアップ

def test_expired_rows_are_pruned_and_storage_stays_bounded(limits, clock):
    limits(guest_create__ip=(1000, 60))
    for i in range(30):
        clock["t"] = T0 + timedelta(seconds=i)
        assert guest(browser()).status_code == 201
    clock["t"] = T0 + timedelta(days=2)
    assert guest(browser()).status_code == 201
    with Session(get_engine()) as db:
        rows = db.execute(select(RateLimitEvent.bucket, RateLimitEvent.created_at)).all()
    assert all(t >= T0 + timedelta(days=2) - timedelta(hours=1) for _b, t in rows)  # 古い行は残らない


def test_limits_are_off_in_development_and_on_in_production():
    s = get_settings()
    assert os.environ.get("WARILINK_RATE_LIMITS_ENABLED") is None
    assert s.environment == "test" and not s.rate_limits_active
    assert type(s)(_env_file=None, environment="production", rate_limit_secret="r" * 32).rate_limits_active


def test_backup_keeps_rate_limit_rows_out_of_dumps_and_counts():
    path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "scripts", "warilink_backup.py")
    spec = importlib.util.spec_from_file_location("warilink_backup", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "rate_limit_events" in mod.EXCLUDED_TABLE_DATA and "rate_limit_events" not in mod.TABLES
    src = open(path, encoding="utf-8").read()
    assert '--exclude-table-data={t}" for t in EXCLUDED_TABLE_DATA' in src
