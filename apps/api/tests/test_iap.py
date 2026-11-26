import threading
import time
from concurrent.futures import ThreadPoolExecutor

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from app.core import iap
from app.core.config import Settings, UnsafeProductionConfig, ensure_safe
from app.main import create_app

AUD = "/projects/123456/locations/asia-northeast1/services/example"


@pytest.fixture
def signing(monkeypatch):
    private = ec.generate_private_key(ec.SECP256R1())
    monkeypatch.setattr(iap, "key_cache", iap.KeyCache())
    monkeypatch.setattr(iap, "_fetch_keys", lambda: {"test-key": private.public_key()})
    return private


def signed(private, *, claims=None, headers=None):
    now = int(time.time())
    data = {"iss": iap.ISSUER, "aud": AUD, "sub": "test-subject", "iat": now, "exp": now + 600}
    data.update(claims or {})
    data = {key: value for key, value in data.items() if value is not None}
    return jwt.encode(data, private, algorithm="ES256", headers=headers or {"kid": "test-key"})


def test_valid_signed_assertion(signing):
    assert iap.verify_assertion(signed(signing), AUD) == "test-subject"


@pytest.mark.parametrize("claims", [
    {"iss": "https://bad.invalid"}, {"aud": "/other"}, {"aud": None}, {"aud": [AUD]},
    {"sub": None}, {"sub": ""}, {"sub": 12}, {"iat": None}, {"exp": None},
    {"iat": "bad"}, {"exp": "bad"}, {"iat": 1, "exp": 10000}, {"exp": 1},
    {"iat": int(time.time()) + 600}, {"exp": int(time.time()) + 5000},
])
def test_bad_claims_rejected(signing, claims):
    with pytest.raises(iap.InvalidAssertion):
        iap.verify_assertion(signed(signing, claims=claims), AUD)


def test_bad_signature_alg_and_kid(signing):
    other = ec.generate_private_key(ec.SECP256R1())
    h, p, sig = signed(signing).split(".")
    mutated = f"{h}.{p}.{('A' if sig[0] != 'A' else 'B')}{sig[1:]}"
    for bad in (mutated, signed(other), signed(signing, headers={"kid": "unknown"}),
                jwt.encode({"sub": "x"}, "test-secret", algorithm="HS256", headers={"kid": "test-key"})):
        with pytest.raises(iap.InvalidAssertion):
            iap.verify_assertion(bad, AUD)


def test_cache_refresh_throttled_and_expired_fails_closed(monkeypatch, signing):
    cache = iap.KeyCache()
    now = [1000.0]
    calls = []
    monkeypatch.setattr(iap.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(iap, "_fetch_keys", lambda: calls.append(1) or {"test-key": signing.public_key()})
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert len(list(pool.map(lambda _: cache.key("test-key"), range(20)))) == 20
    for _ in range(10):
        with pytest.raises(iap.InvalidAssertion):
            cache.key("unknown")
    assert len(calls) == 1
    now[0] += 31
    with pytest.raises(iap.InvalidAssertion):
        cache.key("unknown")
    assert len(calls) == 2
    now[0] += 301

    def fail():
        calls.append(1)
        raise OSError("private detail")
    monkeypatch.setattr(iap, "_fetch_keys", fail)
    for _ in range(5):
        with pytest.raises(iap.IapUnavailable):
            cache.key("test-key")
    assert len(calls) == 3


@pytest.mark.parametrize("over", [
    {"rate_limit_source": "invalid"}, {"rate_limit_source": "iap"},
    {"rate_limit_source": "ip", "iap_audience": AUD},
    {"rate_limit_source": "iap", "iap_audience": AUD, "rate_limits_enabled": False, "rate_limit_secret": "r" * 32},
    {"rate_limit_source": "iap", "iap_audience": AUD, "rate_limits_enabled": True, "rate_limit_secret": "short"},
])
def test_unsafe_iap_settings_rejected(over):
    with pytest.raises(UnsafeProductionConfig):
        ensure_safe(Settings(_env_file=None, environment="test", **over))


def test_middleware_rejects_missing_duplicate_and_unsigned_identity(signing, monkeypatch):
    settings = Settings(_env_file=None, environment="test", rate_limit_source="iap", iap_audience=AUD,
                        rate_limits_enabled=True, rate_limit_secret="r" * 32)
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    client = TestClient(create_app())
    assert client.get("/api/v1/health").status_code == 200
    for headers in ([], [("x-goog-authenticated-user-email", "test@example.invalid")],
                    [("x-goog-iap-jwt-assertion", signed(signing))] * 2):
        response = client.get("/api/v1/identity/me", headers=headers)
        assert response.status_code == 401 and response.json()["code"] == "UNAUTHENTICATED"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"


def test_clock_skew_and_lifetime(signing):
    now = int(time.time())
    assert iap.verify_assertion(signed(signing, claims={"iat": now - 600, "exp": now - 20}), AUD)
    assert iap.verify_assertion(signed(signing, claims={"iat": now + 20, "exp": now + 620}), AUD)
    for claims in ({"iat": now - 600, "exp": now - 31}, {"iat": now + 31, "exp": now + 631},
                   {"iat": now, "exp": now + 661}):
        with pytest.raises(iap.InvalidAssertion):
            iap.verify_assertion(signed(signing, claims=claims), AUD)


def test_unknown_key_refresh_then_rotation(monkeypatch, signing):
    cache = iap.KeyCache()
    now = [1000.0]
    newer = ec.generate_private_key(ec.SECP256R1())
    calls = []
    monkeypatch.setattr(iap.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(iap, "_fetch_keys", lambda: calls.append(1) or
                        ({"old": signing.public_key()} if len(calls) == 1 else {"new": newer.public_key()}))
    assert cache.key("old") == signing.public_key()
    with pytest.raises(iap.InvalidAssertion):
        cache.key("new")
    now[0] += 31
    assert cache.key("new") == newer.public_key()
    assert len(calls) == 2


def test_jwt_url_headers_do_not_select_key_source(signing):
    token = signed(signing, headers={"kid": "test-key", "jku": "https://attacker.invalid/keys",
                                     "x5u": "https://attacker.invalid/cert"})
    assert iap.verify_assertion(token, AUD) == "test-subject"


def test_jwk_fetch_uses_fixed_url_and_timeout(monkeypatch):
    import json

    signing = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(signing.public_key(), as_dict=True)
    jwk.update({"kid": "test-key", "alg": "ES256"})
    seen = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, limit):
            assert limit == 262145
            return json.dumps({"keys": [jwk]}).encode()

    class FakeOpener:
        def open(self, request, timeout):
            seen.append((request.full_url, timeout))
            return FakeResponse()

    monkeypatch.setattr(iap.urllib.request, "build_opener", lambda *_: FakeOpener())
    assert iap._fetch_keys()["test-key"] == signing.public_key()
    assert seen == [(iap.KEYS_URL, 3)]


def test_iap_mode_can_be_safely_enabled():
    settings = Settings(_env_file=None, environment="test", rate_limit_source="iap", iap_audience=AUD,
                        rate_limits_enabled=True, rate_limit_secret="r" * 32)
    assert ensure_safe(settings).rate_limit_source == "iap"


def test_unknown_key_outage_and_recovery(monkeypatch, signing):
    now = 1000.0
    monkeypatch.setattr(iap.time, "monotonic", lambda: now)
    known = signed(signing)
    rotated = signed(signing, headers={"kid": "rotated"})
    assert iap.verify_assertion(known, AUD)
    now += iap.REFRESH_SECONDS
    calls = 0

    def fetch():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError
        return {"rotated": signing.public_key()}

    monkeypatch.setattr(iap, "_fetch_keys", fetch)
    for _ in range(2):
        with pytest.raises(iap.IapUnavailable):
            iap.verify_assertion(rotated, AUD)
        assert iap.verify_assertion(known, AUD)
    assert calls == 1
    now += iap.REFRESH_SECONDS
    assert iap.verify_assertion(rotated, AUD)
    with pytest.raises(iap.InvalidAssertion):
        iap.verify_assertion(known, AUD)
    assert calls == 2


def test_single_refresh_serves_valid_key_and_rejects_expired_key(monkeypatch, signing):
    now = 1000.0
    monkeypatch.setattr(iap.time, "monotonic", lambda: now)
    known = signed(signing)
    unknown = signed(signing, headers={"kid": "unknown"})
    assert iap.verify_assertion(known, AUD)
    now += iap.REFRESH_SECONDS
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def fetch():
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(10)
        raise OSError

    monkeypatch.setattr(iap, "_fetch_keys", fetch)
    with ThreadPoolExecutor(max_workers=3) as pool:
        refresh = pool.submit(iap.verify_assertion, unknown, AUD)
        try:
            assert started.wait(2)
            assert pool.submit(iap.verify_assertion, known, AUD).result(timeout=2)
            now += iap.CACHE_SECONDS
            # Keep the fetch in flight until the expired-key caller reaches its wait limit.
            expired = pool.submit(iap.verify_assertion, known, AUD)
            with pytest.raises(iap.IapUnavailable):
                expired.result(timeout=6)
        finally:
            release.set()
        with pytest.raises(iap.IapUnavailable):
            refresh.result(timeout=2)
    assert calls == 1
