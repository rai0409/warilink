"""IAP signed assertions: fixed key source, bounded cache and fail-closed verification."""

from __future__ import annotations

import json
import math
import threading
import time
import urllib.request
from typing import Any

import jwt
from jwt import PyJWK

KEYS_URL = "https://www.gstatic.com/iap/verify/public_key-jwk"
ISSUER = "https://cloud.google.com/iap"
CACHE_SECONDS = 300
REFRESH_SECONDS = 30
MAX_LIFETIME = 660  # 10 minutes plus 2 * 30 seconds of skew


class InvalidAssertion(Exception):
    pass


class IapUnavailable(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch_keys() -> dict[str, Any]:
    request = urllib.request.Request(KEYS_URL, headers={"Accept": "application/json"})
    with urllib.request.build_opener(_NoRedirect).open(request, timeout=3) as response:
        data = response.read(262145)
    if len(data) > 262144:
        raise ValueError("key response too large")
    payload = json.loads(data)
    if not isinstance(payload, dict) or not isinstance(payload.get("keys"), list):
        raise ValueError("invalid key response")
    keys = {}
    for item in payload["keys"]:
        if not isinstance(item, dict) or item.get("kty") != "EC" or item.get("crv") != "P-256":
            continue
        kid = item.get("kid")
        if not isinstance(kid, str) or not kid or kid in keys or item.get("alg", "ES256") != "ES256":
            raise ValueError("invalid key identifier")
        keys[kid] = PyJWK.from_dict(item).key
    if not keys:
        raise ValueError("no suitable keys")
    return keys


class KeyCache:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._keys: dict[str, Any] = {}
        self._expires = 0.0
        self._next_refresh = 0.0
        self._refreshing = False
        self._refresh_failed = False

    def _cached_key(self, kid: str) -> Any:
        if time.monotonic() < self._expires:
            if kid in self._keys:
                return self._keys[kid]
            if not self._refresh_failed:
                raise InvalidAssertion
        raise IapUnavailable

    def key(self, kid: str) -> Any:
        with self._condition:
            now = time.monotonic()
            if now < self._expires and kid in self._keys:
                return self._keys[kid]
            if self._refreshing:
                if not self._condition.wait_for(lambda: not self._refreshing, timeout=3):
                    raise IapUnavailable
                return self._cached_key(kid)
            if now < self._next_refresh:
                return self._cached_key(kid)
            self._refreshing = True

        keys = None
        try:
            keys = _fetch_keys()
        except Exception:  # noqa: BLE001 - never expose transport or key parsing details
            pass
        finally:
            with self._condition:
                now = time.monotonic()
                self._refresh_failed = keys is None
                if keys is not None:
                    self._keys = keys
                    self._expires = now + CACHE_SECONDS
                self._next_refresh = now + REFRESH_SECONDS
                self._refreshing = False
                self._condition.notify_all()

        with self._condition:
            return self._cached_key(kid)


key_cache = KeyCache()


def verify_assertion(token: str, audience: str) -> str:
    if not token or len(token) > 16384:
        raise InvalidAssertion
    try:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if header.get("alg") != "ES256" or not isinstance(kid, str) or not kid or len(kid) > 256:
            raise InvalidAssertion
        claims = jwt.decode(token, key_cache.key(kid), algorithms=["ES256"], audience=audience,
                            issuer=ISSUER, leeway=30,
                            options={"require": ["iss", "aud", "exp", "iat", "sub"], "strict_aud": True})
        issued, expiry, sub = claims["iat"], claims["exp"], claims["sub"]
        if (isinstance(issued, bool) or isinstance(expiry, bool)
                or not isinstance(issued, (int, float)) or not isinstance(expiry, (int, float))
                or not math.isfinite(issued) or not math.isfinite(expiry)
                or not 0 < expiry - issued <= MAX_LIFETIME
                or not isinstance(sub, str) or not sub.strip()):
            raise InvalidAssertion
        return sub
    except (jwt.PyJWTError, ValueError, TypeError, OverflowError, KeyError):
        raise InvalidAssertion from None
