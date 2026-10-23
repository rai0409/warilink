"""振込先のエンベロープ暗号化(AES-256-GCM)。

- 版ごとに乱数の DEK(32 バイト)を作って中身を暗号化し、DEK を KEK(鍵の版 `key_version`)で包む。
- AAD に「振込先 ID・版・所有者」を入れ、暗号文を別の行・別の人へ付け替えても復号できないようにする。
- KEK は設定(環境変数)から読む。DB・バックアップ・ログには入れない。
- 鍵の入れ替え(rotation)は DEK を包み直すだけ(中身の再暗号化は不要)。`cli.py rotate` を参照。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import Settings, get_settings

DEV_KEY_VERSION = "dev1"
# 開発・テスト専用の固定鍵(本番では使えない。production_problems で拒否)
DEV_KEY = hashlib.sha256(b"warilink-dev-only-destination-key").digest()


class KeyUnavailable(Exception):
    """鍵が設定されていない・版が見つからない。振込先機能を停止する(fail-closed)。"""


@dataclass
class Sealed:
    key_version: str
    wrapped_dek: bytes
    nonce: bytes
    ciphertext: bytes


def _decode_key(v: str) -> bytes:
    k = base64.b64decode(v, validate=True)
    if len(k) != 32:
        raise ValueError("key must be 32 bytes")
    return k


def keyring(s: Settings | None = None) -> tuple[str, dict[str, bytes]]:
    s = s or get_settings()
    if s.destination_keys:
        keys = {ver: _decode_key(v) for ver, v in s.destination_keys.items()}
        active = s.destination_active_key or ""
        if active not in keys:
            raise KeyUnavailable("active key version is not configured")
        return active, keys
    if s.environment in ("development", "test"):
        return DEV_KEY_VERSION, {DEV_KEY_VERSION: DEV_KEY}
    raise KeyUnavailable("destination keys are not configured")


def key_problems(s: Settings) -> list[str]:
    """本番で拒否する鍵の設定(値はメッセージに含めない)。"""
    out: list[str] = []
    try:
        active, keys = keyring(s)
    except (KeyUnavailable, ValueError):
        return ["WARILINK_DESTINATION_KEYS / WARILINK_DESTINATION_ACTIVE_KEY が不正です"
                "(32 バイトの base64、使用中の版を含める)"]
    if active == DEV_KEY_VERSION or any(k == DEV_KEY for k in keys.values()):
        out.append("振込先の暗号化に開発用の鍵が使われています")
    return out


def _aad(destination_id: str, version: int, owner_id: str) -> bytes:
    return f"warilink:destination:{destination_id}:{version}:{owner_id}".encode()


def seal(payload: dict[str, Any], *, destination_id: str, version: int, owner_id: str) -> Sealed:
    active, keys = keyring()
    dek = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    aad = _aad(destination_id, version, owner_id)
    ct = AESGCM(dek).encrypt(nonce, json.dumps(payload, ensure_ascii=False).encode(), aad)
    wrap_nonce = os.urandom(12)
    wrapped = wrap_nonce + AESGCM(keys[active]).encrypt(wrap_nonce, dek, aad)
    return Sealed(active, wrapped, nonce, ct)


def open_sealed(sealed: Sealed, *, destination_id: str, version: int, owner_id: str) -> dict[str, Any]:
    _, keys = keyring()
    kek = keys.get(sealed.key_version)
    if kek is None:
        raise KeyUnavailable("key version not available")
    aad = _aad(destination_id, version, owner_id)
    dek = AESGCM(kek).decrypt(sealed.wrapped_dek[:12], sealed.wrapped_dek[12:], aad)
    return json.loads(AESGCM(dek).decrypt(sealed.nonce, sealed.ciphertext, aad))


def rewrap(sealed: Sealed, *, destination_id: str, version: int, owner_id: str) -> Sealed:
    """DEK を使用中の KEK で包み直す(中身は復号しない)。"""
    active, keys = keyring()
    old = keys.get(sealed.key_version)
    if old is None:
        raise KeyUnavailable("old key version not available")
    aad = _aad(destination_id, version, owner_id)
    dek = AESGCM(old).decrypt(sealed.wrapped_dek[:12], sealed.wrapped_dek[12:], aad)
    n = os.urandom(12)
    return Sealed(active, n + AESGCM(keys[active]).encrypt(n, dek, aad), sealed.nonce, sealed.ciphertext)
