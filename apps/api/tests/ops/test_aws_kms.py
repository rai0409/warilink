"""STEP 02: AWS KMS の鍵の提供元(app/ops/aws_kms.py)。

AWS には接続しない。boto3 と同じ呼び方をするメモリ上の偽 KMS(FakeKms: エイリアス・ARN・自動ローテーション・
暗号化コンテキストの照合・鍵の状態・権限)を使い、ソケットの接続をテストの間は禁止する。
"""

import json
import os
import socket
import sys
import uuid

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.ops.aws_kms import AwsKmsProvider, encryption_context
from app.ops.backup_crypto import BackupCryptoError, KeyConfigError, dek_aad, new_dek

ACCOUNT, REGION = "123456789012", "ap-northeast-1"


class ClientError(Exception):
    def __init__(self, code: str):
        super().__init__(f"{code}: key policy for alias/warilink-secret denied, token=AKIASECRET")
        self.response = {"Error": {"Code": code, "Message": "secret details"}}


class FakeKms:
    """KMS の必要な部分だけ。暗号文に鍵と鍵の素材の番号を入れ、暗号化コンテキストを AAD として結び付ける。"""

    def __init__(self):
        self.keys: dict[str, dict] = {}
        self.aliases: dict[str, str] = {}
        self.order: list[str] = []
        self.denied: set[str] = set()
        self.calls: list[tuple[str, str]] = []

    def create_key(self, alias: str | None = None) -> str:
        arn = f"arn:aws:kms:{REGION}:{ACCOUNT}:key/{uuid.uuid4()}"
        self.keys[arn] = {"state": "Enabled", "usage": "ENCRYPT_DECRYPT", "spec": "SYMMETRIC_DEFAULT",
                          "materials": [os.urandom(32)]}
        self.order.append(arn)
        if alias:
            self.aliases[alias] = arn
        return arn

    def rotate(self, arn: str) -> None:  # 自動ローテーション: ARN は同じで素材が増える
        self.keys[arn]["materials"].append(os.urandom(32))

    def _resolve(self, key_id: str) -> str:
        if key_id.startswith("alias/"):
            if key_id not in self.aliases:
                raise ClientError("NotFoundException")
            return self.aliases[key_id]
        for arn in self.keys:
            if key_id in (arn, arn.rsplit("/", 1)[1]):
                return arn
        raise ClientError("NotFoundException")

    def _check(self, op: str, arn: str) -> dict:
        self.calls.append((op, arn))
        if op in self.denied:
            raise ClientError("AccessDeniedException")
        k = self.keys[arn]
        if k["state"] != "Enabled" and op != "describe_key":
            raise ClientError("DisabledException")
        return k

    def describe_key(self, *, KeyId):
        arn = self._resolve(KeyId)
        k = self._check("describe_key", arn)
        return {"KeyMetadata": {"Arn": arn, "KeyId": arn.rsplit("/", 1)[1], "KeyState": k["state"],
                                "KeyUsage": k["usage"], "KeySpec": k["spec"]}}

    def encrypt(self, *, KeyId, Plaintext, EncryptionContext):
        arn = self._resolve(KeyId)
        k = self._check("encrypt", arn)
        m = len(k["materials"]) - 1
        nonce = os.urandom(12)
        ct = AESGCM(k["materials"][m]).encrypt(nonce, Plaintext, json.dumps(EncryptionContext, sort_keys=True)
                                               .encode())
        return {"KeyId": arn, "CiphertextBlob": bytes([self.order.index(arn), m]) + nonce + ct}

    def decrypt(self, *, CiphertextBlob, KeyId=None, EncryptionContext=None):
        arn = self.order[CiphertextBlob[0]]
        if KeyId is not None and self._resolve(KeyId) != arn:
            raise ClientError("IncorrectKeyException")
        k = self._check("decrypt", arn)
        try:
            pt = AESGCM(k["materials"][CiphertextBlob[1]]).decrypt(
                CiphertextBlob[2:14], CiphertextBlob[14:], json.dumps(EncryptionContext or {}, sort_keys=True)
                .encode())
        except InvalidTag:
            raise ClientError("InvalidCiphertextException") from None
        return {"KeyId": arn, "Plaintext": pt}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **kw):
        raise AssertionError("ネットワークに接続しようとしました")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield
    assert "boto3" not in sys.modules


def _no_secret(msg: str):
    assert "AKIASECRET" not in msg and "warilink-secret" not in msg and "secret details" not in msg


def _aad(bid="b1", created="2026-11-04T00:00:00+00:00", kid=""):
    return dek_aad(bid, created, kid)


def test_alias_is_normalized_to_the_key_arn_and_round_trips():
    kms = FakeKms()
    arn = kms.create_key("alias/warilink-backup")
    p = AwsKmsProvider(kms, "alias/warilink-backup")
    assert p.active_key_id() == arn  # manifest には ARN を書く(エイリアスを付け替えても過去の分を開ける)
    dek = new_dek()
    kid, wrapped = p.wrap(dek, _aad(kid=arn))
    assert kid == arn and dek not in wrapped
    assert p.unwrap(kid, wrapped, _aad(kid=arn)) == dek
    assert AwsKmsProvider(kms).unwrap(kid, wrapped, _aad(kid=arn)) == dek  # 復元は設定の鍵が無くても開ける
    assert encryption_context(_aad(kid=arn))["purpose"] == "warilink-backup-dek"


@pytest.mark.parametrize("change", ["backup_id", "created_at", "key_id_in_aad"])
def test_a_different_aad_is_rejected_by_kms(change):
    kms = FakeKms()
    arn = kms.create_key("alias/a")
    p = AwsKmsProvider(kms, "alias/a")
    kid, wrapped = p.wrap(new_dek(), _aad(kid=arn))
    other = {"backup_id": _aad(bid="b2", kid=arn), "created_at": _aad(created="2026-11-05T00:00:00+00:00", kid=arn),
             "key_id_in_aad": _aad(kid="arn:aws:kms:ap-northeast-1:123456789012:key/other")}[change]
    with pytest.raises(BackupCryptoError, match="鍵が違うか") as e:
        p.unwrap(kid, wrapped, other)
    _no_secret(str(e.value))


def test_wrong_key_and_non_arn_key_ids_are_rejected():
    kms = FakeKms()
    a, b = kms.create_key("alias/a"), kms.create_key("alias/b")
    p = AwsKmsProvider(kms, "alias/a")
    _, wrapped = p.wrap(new_dek(), _aad(kid=a))
    with pytest.raises(BackupCryptoError, match="IncorrectKeyException"):
        p.unwrap(b, wrapped, _aad(kid=b))  # 別の鍵の ARN を指定(manifest の書き換え)
    with pytest.raises(BackupCryptoError, match="ARN"):
        p.unwrap("alias/a", wrapped, _aad(kid="alias/a"))  # ARN 以外は受け付けない


def test_rotation_and_alias_repointing_keep_old_backups_readable():
    kms = FakeKms()
    old = kms.create_key("alias/warilink-backup")
    p = AwsKmsProvider(kms, "alias/warilink-backup")
    dek1 = new_dek()
    kid1, w1 = p.wrap(dek1, _aad(kid=old))
    kms.rotate(old)  # 自動ローテーション(ARN は同じ)
    dek2 = new_dek()
    kid2, w2 = AwsKmsProvider(kms, "alias/warilink-backup").wrap(dek2, _aad("b2", kid=old))
    assert kid1 == kid2 == old and w1[1] != w2[1]  # 新しい素材で暗号化
    new = kms.create_key()
    kms.aliases["alias/warilink-backup"] = new  # 鍵の入れ替え(エイリアスの付け替え)
    q = AwsKmsProvider(kms, "alias/warilink-backup")
    assert q.active_key_id() == new
    assert q.unwrap(kid1, w1, _aad(kid=old)) == dek1  # 古いバックアップは manifest の ARN で開ける
    assert q.unwrap(kid2, w2, _aad("b2", kid=old)) == dek2


def test_unusable_keys_and_missing_permissions_fail_closed():
    kms = FakeKms()
    arn = kms.create_key("alias/a")
    p = AwsKmsProvider(kms, "alias/a")
    kid, wrapped = p.wrap(new_dek(), _aad(kid=arn))
    kms.denied.add("decrypt")
    with pytest.raises(BackupCryptoError, match="権限") as e:
        AwsKmsProvider(kms).unwrap(kid, wrapped, _aad(kid=arn))
    _no_secret(str(e.value))
    kms.denied.clear()
    kms.keys[arn]["state"] = "Disabled"
    with pytest.raises(BackupCryptoError, match="使えない"):
        AwsKmsProvider(kms).unwrap(kid, wrapped, _aad(kid=arn))
    with pytest.raises(KeyConfigError, match="有効な対称暗号"):
        AwsKmsProvider(kms, "alias/a").active_key_id()
    with pytest.raises(KeyConfigError, match="NotFoundException"):
        AwsKmsProvider(kms, "alias/missing").active_key_id()
    with pytest.raises(KeyConfigError, match="WARILINK_BACKUP_KMS_KEY_ID"):
        AwsKmsProvider(kms).active_key_id()
    kms.keys[arn]["state"] = "Enabled"
    kms.denied.add("encrypt")
    with pytest.raises(KeyConfigError, match="権限"):
        AwsKmsProvider(kms, "alias/a").wrap(new_dek(), _aad(kid=arn))
    kms.keys[arn]["usage"] = "SIGN_VERIFY"
    with pytest.raises(KeyConfigError):
        AwsKmsProvider(kms, "alias/a").active_key_id()


def test_from_env_never_connects_without_the_live_flag(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIASECRET")
    with pytest.raises(KeyConfigError, match="WARILINK_AWS_LIVE=1"):
        AwsKmsProvider.from_env({"WARILINK_BACKUP_KMS_KEY_ID": "alias/a"})
    kms = FakeKms()
    p = AwsKmsProvider.from_env({"WARILINK_BACKUP_KMS_KEY_ID": "alias/a"}, client=kms)
    assert p.key_ref == "alias/a" and kms.calls == []  # 作るだけでは呼ばない
