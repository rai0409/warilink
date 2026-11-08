"""AWS KMS の鍵の提供元(STEP 02)。既存の KeyProvider(backup_crypto.py)を実装する。暗号の本体は変えない。

- DEK を KMS の Encrypt / Decrypt で包む・ほどく。KMS の暗号化コンテキストに、既存の AAD(dek_aad: バックアップ ID・
  作成日時・鍵 ID)の SHA-256 を結び付ける。
  AAD が違えば(別のバックアップへの付け替え・manifest の書き換え)KMS が拒否する。
- 鍵 ID の正規化: 設定(WARILINK_BACKUP_KMS_KEY_ID)はエイリアス・鍵 ID・ARN のどれでもよいが、DescribeKey で
  **鍵の ARN** に直してから使い、manifest にも ARN を書く。
  エイリアスを付け替えても、過去のバックアップは書いた ARN で開ける。
  KMS の自動ローテーションでは ARN は変わらない。鍵を入れ替えたら、古い鍵は保持期間(30 日)を過ぎるまで無効にしない。
- 復元は manifest の key_id(ARN)で Decrypt する(設定の鍵とは関係なく、その鍵の Decrypt 権限があればよい)。
- 接続は WARILINK_AWS_LIVE=1 のときだけ(s3_storage.aws_client)。エラーの表示は AWS のエラーコードだけ。
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Any

from app.ops.backup_crypto import BackupCryptoError, KeyConfigError
from app.ops.s3_storage import AwsUnavailable, aws_client, error_code

NAME = "aws-kms"
PURPOSE = "warilink-backup-dek"
KEY_ARN_RE = re.compile(r"^arn:aws[a-z-]*:kms:[a-z0-9-]+:\d{12}:key/[A-Za-z0-9-]{1,128}$")
WRONG_KEY = {"InvalidCiphertextException", "IncorrectKeyException", "InvalidKeyUsageException"}
DENIED = {"AccessDeniedException", "AccessDenied", "UnrecognizedClientException", "InvalidClientTokenId",
          "ExpiredTokenException", "ExpiredToken"}
UNUSABLE = {"NotFoundException", "DisabledException", "KMSInvalidStateException", "KeyUnavailableException"}


def encryption_context(aad: bytes) -> dict[str, str]:
    return {"purpose": PURPOSE, "aad_sha256": hashlib.sha256(aad).hexdigest()}


def _message(e: Exception, what: str) -> str:
    code = error_code(e)
    if code in WRONG_KEY:
        return f"{what}: 鍵が違うか、manifest が書き換えられています({code})"
    if code in DENIED:
        return f"{what}: KMS の権限・署名の確認に失敗しました({code})"
    if code in UNUSABLE:
        return f"{what}: KMS の鍵が見つからないか使えない状態です({code})"
    return f"{what}: KMS の操作に失敗しました({code})"


@dataclass
class AwsKmsProvider:
    client: Any
    key_ref: str | None = None  # バックアップを作るときだけ必要(エイリアス・鍵 ID・ARN)
    name: str = NAME
    _arn: str | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, *, client: Any = None) -> AwsKmsProvider:
        e = os.environ if env is None else env
        if client is None:
            try:
                client = aws_client("kms", dict(e))
            except AwsUnavailable as err:
                raise KeyConfigError(str(err)) from None
        return cls(client=client, key_ref=e.get("WARILINK_BACKUP_KMS_KEY_ID") or None)

    def active_key_id(self) -> str:
        """設定の鍵を ARN に直す(有効な対称暗号の鍵だけ)。"""
        if self._arn is not None:
            return self._arn
        if not self.key_ref:
            raise KeyConfigError("WARILINK_BACKUP_KMS_KEY_ID を設定してください")
        try:
            meta = self.client.describe_key(KeyId=self.key_ref)["KeyMetadata"]
        except Exception as e:  # noqa: BLE001 - SDK の例外は種類が多い。コードだけで分ける
            raise KeyConfigError(_message(e, "KMS の鍵の確認")) from None
        arn = meta.get("Arn", "")
        if not KEY_ARN_RE.fullmatch(arn):
            raise KeyConfigError("KMS の鍵の ARN を確認できません")
        if meta.get("KeyState") != "Enabled" or meta.get("KeyUsage") != "ENCRYPT_DECRYPT" \
                or meta.get("KeySpec", "SYMMETRIC_DEFAULT") != "SYMMETRIC_DEFAULT":
            raise KeyConfigError("KMS の鍵が有効な対称暗号の鍵ではありません")
        self._arn = arn
        return arn

    def wrap(self, dek: bytes, aad: bytes) -> tuple[str, bytes]:
        arn = self.active_key_id()
        try:
            r = self.client.encrypt(KeyId=arn, Plaintext=dek, EncryptionContext=encryption_context(aad))
        except Exception as e:  # noqa: BLE001
            raise KeyConfigError(_message(e, "KMS での暗号化")) from None
        if r.get("KeyId") != arn:
            raise KeyConfigError("KMS が別の鍵で暗号化しました")
        return arn, bytes(r["CiphertextBlob"])

    def unwrap(self, key_id: str, wrapped: bytes, aad: bytes) -> bytes:
        if not KEY_ARN_RE.fullmatch(key_id):
            raise BackupCryptoError("manifest の鍵 ID が KMS の鍵の ARN ではありません")
        try:
            r = self.client.decrypt(CiphertextBlob=wrapped, KeyId=key_id, EncryptionContext=encryption_context(aad))
        except Exception as e:  # noqa: BLE001
            raise BackupCryptoError(_message(e, "バックアップの鍵の復号")) from None
        dek = bytes(r.get("Plaintext") or b"")
        if r.get("KeyId") != key_id or len(dek) != 32:
            raise BackupCryptoError("バックアップの鍵を復号できません(鍵が違うか、manifest が書き換えられています)")
        return dek
