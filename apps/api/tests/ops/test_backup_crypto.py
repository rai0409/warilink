"""PR C: バックアップの暗号化(AES-256-GCM のエンベロープ方式)と鍵の提供元。"""

import base64
import io
import os

import pytest

from app.ops.backup_crypto import (
    CHUNK_BYTES,
    BackupCryptoError,
    EncryptWriter,
    EnvKeyProvider,
    FakeKmsProvider,
    KeyConfigError,
    decrypt_chunks,
    dek_aad,
    new_dek,
)


def _enc(data: bytes, dek: bytes, name: str = "db.dump", backup_id: str = "b1"):
    buf = io.BytesIO()
    w = EncryptWriter(buf, dek, backup_id, name)
    w.write(data)
    meta = w.close()  # 最後の塊は close で書く
    return buf.getvalue(), meta


def _dec(blob: bytes, dek: bytes, meta, name: str = "db.dump", backup_id: str = "b1") -> bytes:
    return b"".join(decrypt_chunks(io.BytesIO(blob), dek, backup_id, name, meta.nonce_prefix))


@pytest.mark.parametrize("size", [0, 1, CHUNK_BYTES - 1, CHUNK_BYTES, CHUNK_BYTES + 1, 3 * CHUNK_BYTES + 7])
def test_roundtrip_any_size(size):
    dek, data = new_dek(), os.urandom(size)
    blob, meta = _enc(data, dek)
    assert _dec(blob, dek, meta) == data
    assert data[:64] not in blob or size == 0  # 平文がそのまま入っていない


def test_tampering_truncation_reordering_and_swapping_are_rejected():
    dek, data = new_dek(), os.urandom(2 * CHUNK_BYTES + 10)
    blob, meta = _enc(data, dek)
    flipped = bytearray(blob)
    flipped[100] ^= 1
    frame = 4 + CHUNK_BYTES + 16
    cases = {
        "1 ビットの改ざん": bytes(flipped),
        "最後の塊を落とす(切り詰め)": blob[: 2 * frame],
        "途中で切れる": blob[:-5],
        "塊の入れ替え": blob[frame: 2 * frame] + blob[:frame] + blob[2 * frame:],
        "後ろにデータを足す": blob + blob[:frame],
    }
    for label, bad in cases.items():
        with pytest.raises(BackupCryptoError):
            _dec(bad, dek, meta)
        assert label
    # 別のファイル・別のバックアップとして復号できない(差し替えの検出)
    with pytest.raises(BackupCryptoError):
        _dec(blob, dek, meta, name="receipts/x.jpg")
    with pytest.raises(BackupCryptoError):
        _dec(blob, dek, meta, backup_id="other")
    with pytest.raises(BackupCryptoError):
        _dec(blob, new_dek(), meta)  # 鍵違い


def test_wrapped_dek_is_bound_to_backup_and_key():
    kms = FakeKmsProvider()
    dek = new_dek()
    kid, wrapped = kms.wrap(dek, dek_aad("b1", "t1", kms.active_key_id()))
    assert kms.unwrap(kid, wrapped, dek_aad("b1", "t1", kid)) == dek
    for aad in (dek_aad("b2", "t1", kid), dek_aad("b1", "t2", kid)):
        with pytest.raises(BackupCryptoError):
            kms.unwrap(kid, wrapped, aad)  # 別のバックアップへ付け替えられない
    with pytest.raises(BackupCryptoError):
        FakeKmsProvider().unwrap(kid, wrapped, dek_aad("b1", "t1", kid))  # 別の鍵
    with pytest.raises(BackupCryptoError):
        kms.unwrap("missing", wrapped, dek_aad("b1", "t1", "missing"))


def test_env_key_provider_validates_keys():
    good = base64.b64encode(os.urandom(32)).decode()
    p = EnvKeyProvider.from_env({"WARILINK_BACKUP_KEYS": f"k2:{good}, k1:{good}", "WARILINK_BACKUP_ACTIVE_KEY": "k2"})
    assert p.active_key_id() == "k2" and set(p.keys) == {"k1", "k2"}
    bad = [
        {},
        {"WARILINK_BACKUP_KEYS": f"k1:{good}"},  # 使う鍵の指定が無い
        {"WARILINK_BACKUP_KEYS": "k1:short", "WARILINK_BACKUP_ACTIVE_KEY": "k1"},
        {"WARILINK_BACKUP_KEYS": f"k1:{base64.b64encode(os.urandom(16)).decode()}", "WARILINK_BACKUP_ACTIVE_KEY": "k1"},
        {"WARILINK_BACKUP_KEYS": f"k1:{base64.b64encode(bytes(32)).decode()}", "WARILINK_BACKUP_ACTIVE_KEY": "k1"},
        {"WARILINK_BACKUP_KEYS": f"k1:{good}", "WARILINK_BACKUP_ACTIVE_KEY": "k9"},
        {"WARILINK_BACKUP_KEYS": good, "WARILINK_BACKUP_ACTIVE_KEY": "k1"},
    ]
    for env in bad:
        with pytest.raises(KeyConfigError) as e:
            EnvKeyProvider.from_env(env)
        assert good not in str(e.value)  # エラーに鍵を含めない
