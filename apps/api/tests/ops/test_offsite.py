"""PR C: 外部保管のインターフェース(ローカルディレクトリの実装)。クラウドには接続しない。"""

from pathlib import Path

import pytest

from app.ops.offsite import LocalDirStorage, OffsiteError, storage_from_env, upload


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
        storage_from_env({"WARILINK_BACKUP_OFFSITE": "s3://bucket"})  # 未対応(接続しない)
    with pytest.raises(OffsiteError):
        LocalDirStorage(tmp_path / "r").put("../escape", tmp_path / "x")
