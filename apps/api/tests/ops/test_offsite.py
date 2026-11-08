"""PR C: 外部保管のインターフェース(ローカルディレクトリの実装)。クラウドには接続しない。"""

from pathlib import Path

import pytest

from app.ops.offsite import (
    LocalDirStorage,
    OffsiteError,
    fetch,
    put_receipt,
    read_receipt,
    storage_from_env,
    upload,
)


def _backup(tmp: Path) -> Path:
    d = tmp / "b"
    (d / "receipts").mkdir(parents=True)
    (d / "db.dump.enc").write_bytes(b"d" * 10)
    (d / "receipts" / "x.enc").write_bytes(b"x")
    (d / "manifest.json").write_text("{}")
    return d


def test_upload_is_idempotent_and_verified(tmp_path):
    store = LocalDirStorage(tmp_path / "remote")
    d = _backup(tmp_path)
    assert upload(store, d, "2026-10-31-abc") == {"uploaded": 3, "skipped": 0}
    assert upload(store, d, "2026-10-31-abc") == {"uploaded": 0, "skipped": 3}  # 置き直さない
    assert (tmp_path / "remote" / "2026-10-31-abc" / "receipts" / "x.enc").read_bytes() == b"x"
    assert not list((tmp_path / "remote").rglob("*.part"))


def test_upload_retries_then_fails_without_partial_manifest(tmp_path):
    d = _backup(tmp_path)
    calls = []

    class Flaky(LocalDirStorage):
        def put(self, key, src):
            calls.append(key)
            if key.endswith("db.dump.enc"):
                raise OSError("network")
            super().put(key, src)

    store = Flaky(tmp_path / "remote")
    with pytest.raises(OffsiteError):
        upload(store, d, "p", attempts=3, sleep=lambda s: None)
    assert calls.count("p/db.dump.enc") == 3
    assert not (tmp_path / "remote" / "p" / "manifest.json").exists()  # manifest は最後(そろっていなければ置かない)


def test_storage_from_env_and_path_escape(tmp_path):
    assert storage_from_env({}) is None
    assert isinstance(storage_from_env({"WARILINK_BACKUP_OFFSITE": f"local:{tmp_path}"}), LocalDirStorage)
    with pytest.raises(OffsiteError):
        storage_from_env({"WARILINK_BACKUP_OFFSITE": "s3://bucket"})  # STEP 02: 接頭辞の無い S3 は拒否
    with pytest.raises(OffsiteError, match="WARILINK_AWS_LIVE=1"):
        storage_from_env({"WARILINK_BACKUP_OFFSITE": "s3://warilink-backups/prod"})  # 接続の許可が無ければ接続しない
    with pytest.raises(OffsiteError):
        storage_from_env({"WARILINK_BACKUP_OFFSITE": "gs://bucket/x"})  # 未対応の保管先
    with pytest.raises(OffsiteError):
        LocalDirStorage(tmp_path / "r").put("../escape", tmp_path / "x")


def test_receipt_and_fetch_detect_replaced_local_copies(tmp_path):
    """STEP 02: 受領記録の SHA-256 と読み戻した実バイトを照合する(保管先のファイルを後から差し替えた場合)。"""
    store = LocalDirStorage(tmp_path / "remote")
    d = _backup(tmp_path)
    versions: dict = {}
    assert upload(store, d, "p", versions=versions) == {"uploaded": 3, "skipped": 0}
    assert all(v["version_id"] is None and len(v["sha256"]) == 64 for v in versions.values())
    receipt = put_receipt(store, "p", versions, tmp_path)
    body, digest = read_receipt(store, "p", None, tmp_path / "r")
    assert digest == receipt["sha256"] and body["objects"] == versions
    assert fetch(store, "p", body, tmp_path / "ok") == []
    (tmp_path / "remote" / "p" / "db.dump.enc").write_bytes(b"replaced")
    problems = fetch(store, "p", body, tmp_path / "bad")
    assert problems == ["db.dump.enc: 読み戻した内容の SHA-256 が受領記録と一致しません"]
