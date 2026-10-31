"""バックアップの暗号化(PR C)。暗号は自作せず、cryptography の AES-256-GCM だけを使う。

方式(エンベロープ暗号化):
- バックアップごとに乱数の DEK(32 バイト)を作り、各ファイルを 1 MiB の塊ごとに AES-256-GCM で暗号化する。
- 塊の nonce = ファイルごとの乱数 8 バイト + 塊の番号 4 バイト(同じ DEK で nonce が重ならない)。
- 塊の AAD = バックアップ ID・ファイル名・塊の番号・最後の塊か。
  切り詰め・順番の入れ替え・ファイルの差し替えは復号で失敗する。
- DEK は鍵の提供元(KeyProvider)の KEK で包み、包んだ DEK だけを manifest に書く。KEK はバックアップに入れない。
- 包むときの AAD = バックアップ ID・作成日時・鍵 ID(包んだ DEK を別のバックアップへ付け替えても使えない)。

ファイルの形: 塊ごとに [4 バイトの長さ(ビッグエンディアン)][暗号文+認証タグ]。最後の塊(空でもよい)の印は AAD に含む。
"""

from __future__ import annotations

import base64
import hashlib
import os
import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import BinaryIO, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

FORMAT = "warilink-backup-2"
ALGORITHM = "AES-256-GCM"
CHUNK_BYTES = 1 << 20
_LEN = struct.Struct(">I")
_MAX_CIPHERTEXT = CHUNK_BYTES + 16


class BackupCryptoError(Exception):
    """改ざん・破損・鍵違い・欠落。復元を中止する(fail-closed)。メッセージに鍵や中身を含めない。"""


class KeyConfigError(Exception):
    """鍵の設定が無い・不正。バックアップを作らない。"""


# ---------------------------------------------------------------- 鍵の提供元


class KeyProvider(Protocol):
    """KEK で DEK を包む・ほどく。本番では管理された KMS を使う(接続先は未定: KMS_LIVE=BLOCKED)。"""

    name: str

    def active_key_id(self) -> str: ...

    def wrap(self, dek: bytes, aad: bytes) -> tuple[str, bytes]: ...

    def unwrap(self, key_id: str, wrapped: bytes, aad: bytes) -> bytes: ...


def _aes_wrap(kek: bytes, dek: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(kek).encrypt(nonce, dek, aad)


def _aes_unwrap(kek: bytes, wrapped: bytes, aad: bytes) -> bytes:
    try:
        return AESGCM(kek).decrypt(wrapped[:12], wrapped[12:], aad)
    except InvalidTag as e:
        raise BackupCryptoError("バックアップの鍵を復号できません(鍵が違うか、manifest が書き換えられています)") from e


# 開発用に公開されている鍵(振込先の開発用の固定鍵など)はバックアップに使わない
_FORBIDDEN_KEYS = {hashlib.sha256(b"warilink-dev-only-destination-key").digest(), bytes(32)}


@dataclass
class EnvKeyProvider:
    """環境変数の KEK。`WARILINK_BACKUP_KEYS="id1:base64,id2:base64"`、`WARILINK_BACKUP_ACTIVE_KEY=id1`。

    KMS が決まるまでの手段。鍵はバックアップと別の場所(シークレットストア)で管理し、バックアップの保管先に置かない。
    古い鍵 ID は、その鍵で作ったバックアップが保持期間を過ぎるまで残す(ほどく時に使う)。
    """

    keys: dict[str, bytes]
    active: str
    name: str = "env"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> EnvKeyProvider:
        e = os.environ if env is None else env
        raw, active = e.get("WARILINK_BACKUP_KEYS", ""), e.get("WARILINK_BACKUP_ACTIVE_KEY", "")
        if not raw or not active:
            raise KeyConfigError("WARILINK_BACKUP_KEYS と WARILINK_BACKUP_ACTIVE_KEY を設定してください")
        keys: dict[str, bytes] = {}
        for part in raw.split(","):
            kid, sep, b64 = part.strip().partition(":")
            if not sep or not kid:
                raise KeyConfigError("WARILINK_BACKUP_KEYS は「鍵 ID:base64」をカンマで区切って指定してください")
            try:
                key = base64.b64decode(b64, validate=True)
            except ValueError as err:
                raise KeyConfigError(f"鍵 {kid} が base64 ではありません") from err
            if len(key) != 32:
                raise KeyConfigError(f"鍵 {kid} は 32 バイトにしてください")
            if key in _FORBIDDEN_KEYS:
                raise KeyConfigError(f"鍵 {kid} は公開されている開発用の値です")
            keys[kid] = key
        if active not in keys:
            raise KeyConfigError("WARILINK_BACKUP_ACTIVE_KEY が WARILINK_BACKUP_KEYS にありません")
        return cls(keys=keys, active=active)

    def active_key_id(self) -> str:
        return self.active

    def wrap(self, dek: bytes, aad: bytes) -> tuple[str, bytes]:
        return self.active, _aes_wrap(self.keys[self.active], dek, aad)

    def unwrap(self, key_id: str, wrapped: bytes, aad: bytes) -> bytes:
        if key_id not in self.keys:
            raise BackupCryptoError(f"鍵 {key_id} がありません(入れ替え後に古い鍵を消していないか確認してください)")
        return _aes_unwrap(self.keys[key_id], wrapped, aad)


@dataclass
class FakeKmsProvider:
    """テスト・訓練用の KMS の代わり(メモリ上の乱数の鍵)。本番では使わない。"""

    keys: dict[str, bytes] = field(default_factory=lambda: {"fake-1": os.urandom(32)})
    active: str = "fake-1"
    name: str = "fake-kms"
    calls: list[str] = field(default_factory=list)

    def active_key_id(self) -> str:
        return self.active

    def wrap(self, dek: bytes, aad: bytes) -> tuple[str, bytes]:
        self.calls.append("wrap")
        return self.active, _aes_wrap(self.keys[self.active], dek, aad)

    def unwrap(self, key_id: str, wrapped: bytes, aad: bytes) -> bytes:
        self.calls.append("unwrap")
        if key_id not in self.keys:
            raise BackupCryptoError(f"鍵 {key_id} がありません")
        return _aes_unwrap(self.keys[key_id], wrapped, aad)


def dek_aad(backup_id: str, created_at: str, key_id: str) -> bytes:
    return f"warilink-backup-dek\x1f{backup_id}\x1f{created_at}\x1f{key_id}".encode()


# ---------------------------------------------------------------- ファイルの暗号化・復号


def _chunk_aad(backup_id: str, name: str, index: int, final: bool) -> bytes:
    return f"{FORMAT}\x1f{backup_id}\x1f{name}\x1f{index}\x1f{int(final)}".encode()


@dataclass
class EncryptedFile:
    nonce_prefix: str  # base64(8 バイト)
    ciphertext_sha256: str
    bytes: int  # 暗号文の大きさ
    plaintext_sha256: str  # 中身の manifest(暗号化側)にだけ書く


class EncryptWriter:
    """平文を受け取り、塊ごとに暗号化して out に書く。平文をディスクに置かない(pg_dump の出力をそのまま流し込める)。"""

    def __init__(self, out: BinaryIO, dek: bytes, backup_id: str, name: str) -> None:
        self._out, self._aes, self._backup_id, self._name = out, AESGCM(dek), backup_id, name
        self._prefix = os.urandom(8)
        self._buf = bytearray()
        self._index = 0
        self._ct_hash, self._pt_hash = hashlib.sha256(), hashlib.sha256()
        self._size = 0
        self._closed = False

    def _emit(self, data: bytes, final: bool) -> None:
        nonce = self._prefix + struct.pack(">I", self._index)
        ct = self._aes.encrypt(nonce, data, _chunk_aad(self._backup_id, self._name, self._index, final))
        frame = _LEN.pack(len(ct)) + ct
        self._out.write(frame)
        self._ct_hash.update(frame)
        self._size += len(frame)
        self._index += 1

    def write(self, data: bytes) -> int:
        if self._closed:
            raise ValueError("閉じたあとに書き込めません")
        self._pt_hash.update(data)
        self._buf += data
        while len(self._buf) > CHUNK_BYTES:  # 最後の塊を必ず close() で出すため「より大きい」とき
            self._emit(bytes(self._buf[:CHUNK_BYTES]), final=False)
            del self._buf[:CHUNK_BYTES]
        return len(data)

    def close(self) -> EncryptedFile:
        if not self._closed:
            self._emit(bytes(self._buf), final=True)
            self._buf.clear()
            self._closed = True
        return EncryptedFile(base64.b64encode(self._prefix).decode(), self._ct_hash.hexdigest(), self._size,
                             self._pt_hash.hexdigest())


def decrypt_chunks(src: BinaryIO, dek: bytes, backup_id: str, name: str, nonce_prefix: str) -> Iterator[bytes]:
    """塊ごとに復号して返す。認証に失敗した塊・最後の印の無い終わり・最後の印の後のデータで BackupCryptoError。"""
    aes = AESGCM(dek)
    prefix = base64.b64decode(nonce_prefix)
    index = 0
    while True:
        head = src.read(_LEN.size)
        if not head:
            raise BackupCryptoError(f"{name} が途中で切れています")
        if len(head) != _LEN.size:
            raise BackupCryptoError(f"{name} が壊れています")
        (length,) = _LEN.unpack(head)
        if length > _MAX_CIPHERTEXT or length < 16:
            raise BackupCryptoError(f"{name} が壊れています")
        ct = src.read(length)
        if len(ct) != length:
            raise BackupCryptoError(f"{name} が途中で切れています")
        nonce = prefix + struct.pack(">I", index)
        for final in (False, True):
            try:
                pt = aes.decrypt(nonce, ct, _chunk_aad(backup_id, name, index, final))
            except InvalidTag:
                continue
            break
        else:
            raise BackupCryptoError(f"{name} を復号できません(改ざん・差し替え・鍵違いのいずれか)")
        yield pt
        index += 1
        if final:
            if src.read(1):
                raise BackupCryptoError(f"{name} の最後のあとにデータがあります")
            return


def new_dek() -> bytes:
    return AESGCM.generate_key(bit_length=256)


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()
