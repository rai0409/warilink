"""テスト用のソフトウェア認証器(WebAuthn)。P-256 の鍵で実際に署名し、サーバーの検証を本物の手順で通す。

attestation は "none"。実在の認証器・個人のデータは使わない。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

ORIGIN = "http://localhost:3000"
RP_ID = "localhost"
UP, UV, AT = 0x01, 0x04, 0x40


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class SoftAuthenticator:
    def __init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(32)
        self.sign_count = 0
        self.user_handle: bytes | None = None

    def _cose_key(self) -> bytes:
        n = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})

    @staticmethod
    def _client_data(kind: str, challenge: str, origin: str) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin, "crossOrigin": False}).encode()

    def create(self, options: dict[str, Any], *, origin: str = ORIGIN, rp_id: str = RP_ID,
               flags: int = UP | UV | AT, challenge: str | None = None) -> dict[str, Any]:
        self.user_handle = unb64url(options["user"]["id"])
        cd = self._client_data("webauthn.create", challenge or options["challenge"], origin)
        auth = (hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + self.sign_count.to_bytes(4, "big")
                + bytes(16) + len(self.credential_id).to_bytes(2, "big") + self.credential_id + self._cose_key())
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth})
        cid = b64url(self.credential_id)
        return {"id": cid, "rawId": cid, "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": b64url(cd), "attestationObject": b64url(att),
                             "transports": ["internal"]}}

    def get(self, options: dict[str, Any], *, origin: str = ORIGIN, rp_id: str = RP_ID, flags: int = UP | UV,
            user_handle: bytes | None = None, bump: bool = True) -> dict[str, Any]:
        if bump:
            self.sign_count += 1
        cd = self._client_data("webauthn.get", options["challenge"], origin)
        auth = hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + self.sign_count.to_bytes(4, "big")
        sig = self.key.sign(auth + hashlib.sha256(cd).digest(), ec.ECDSA(hashes.SHA256()))
        cid = b64url(self.credential_id)
        handle = user_handle if user_handle is not None else self.user_handle
        assert handle is not None
        return {"id": cid, "rawId": cid, "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": b64url(cd), "authenticatorData": b64url(auth),
                             "signature": b64url(sig), "userHandle": b64url(handle)}}
