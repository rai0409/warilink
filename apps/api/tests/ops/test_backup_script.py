"""PR C: scripts/warilink_backup.py の暗号化バックアップと、復元の安全装置(Docker・DB を使わない部分)。

pg_dump・psql を置き換え、暗号化の形式・改ざんの検出・復元を拒否する条件を確かめる。
実際の DB への backup → restore → 削除の再適用は `drill`(隔離した一時コンテナ)で確かめる。
"""

import argparse
import base64
import importlib.util
import io
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from app.ops.backup_crypto import BackupCryptoError, FakeKmsProvider
from app.ops.deletion_journal import LocalDirJournal
from app.ops.offsite import LocalDirStorage
from app.ops.s3_storage import S3Location, S3Storage

from .test_aws_kms import FakeKms
from .test_s3_storage import BUCKET as FAKE_BUCKET
from .test_s3_storage import (
    FakeS3,
    no_network,  # noqa: F401 - このモジュールでも接続を禁止する(autouse)
)

S3_LOC = S3Location(FAKE_BUCKET, "backups/prod")

SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "warilink_backup.py"


@pytest.fixture
def mod(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("warilink_backup_under_test", SCRIPT)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    snap = {"alembic_version": "v1", "row_counts": {"groups": 1}, "live_image_keys": ["a.jpg", "b.jpg"]}
    monkeypatch.setattr(m, "snapshot", lambda args, db: snap)
    monkeypatch.setattr(m, "tombstones_latest", lambda args, db: "2026-10-30T00:00:00+00:00")
    monkeypatch.setattr(m, "record_run", lambda args, db, job, *, ok: None)
    code = "import sys; sys.stdout.buffer.write(b'PGDMP' + bytes(range(256)) * 9000)"
    monkeypatch.setattr(m, "dump_cmd", lambda args, db: [sys.executable, "-c", code])
    created = []

    def psql(args, db, sql):  # 復元を拒否するべき場面で DB を作っていないかを記録する
        created.append(sql)
        raise AssertionError("このテストでは DB に触れない")

    monkeypatch.setattr(m, "psql", psql)
    monkeypatch.setattr(m, "db_exists", lambda args, name: False)
    m.created = created
    monkeypatch.delenv("WARILINK_ENVIRONMENT", raising=False)
    monkeypatch.delenv("WARILINK_BACKUP_OFFSITE", raising=False)
    # 削除ジャーナル(STEP 01)。backup はチェックポイントを manifest に入れ、restore は検査してから使う
    monkeypatch.setenv("WARILINK_DELETION_JOURNAL", f"local:{tmp_path / 'journal'}")
    return m


def _args(**kw):
    base = {"container": "x", "user": "warilink", "port": "55432", "db": "warilink_src", "insecure_plaintext": False}
    return argparse.Namespace(**{**base, **kw})


def _make(mod, tmp_path, provider, *, images=("a.jpg", "b.jpg"), storage=None, name="backup"):
    src = tmp_path / f"src_{name}"
    src.mkdir()
    for n in images:
        (src / n).write_bytes(f"jpeg-{n}".encode() * 100)
    out = tmp_path / name
    rc = mod.cmd_backup(_args(receipts_dir=str(src), out=str(out), provider=provider, storage=storage))
    return out, rc


def _restore(mod, tmp_path, out, provider, tomb, **kw):
    return mod.cmd_restore(_args(**{"from": str(out), "target_db": "warilink_restore_t", "receipts_target":
                                    str(tmp_path / "dst"), "provider": provider, "tombstones": tomb, **kw}))


def _tomb(tmp_path, latest="2026-10-31T00:00:00+00:00"):
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps({"kind": "identity", "target_id": "00000000-0000-0000-0000-000000000001",
                             "created_at": latest}) + "\n")
    return str(p)


def test_backup_is_encrypted_and_keys_are_not_stored(mod, tmp_path, capsys):
    kms = FakeKmsProvider()
    out, rc = _make(mod, tmp_path, kms, storage=LocalDirStorage(tmp_path / "remote"))
    assert rc == 0
    names = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    assert names == ["db.dump.enc", "manifest.enc", "manifest.json", "receipts/a.jpg.enc", "receipts/b.jpg.enc"]
    outer = json.loads((out / "manifest.json").read_text())
    assert outer["format"] == "warilink-backup-2" and outer["encrypted"] is True
    assert outer["key"]["provider"] == "fake-kms" and "row_counts" not in outer  # 件数・画像のキーは暗号化側
    blob = b"".join(p.read_bytes() for p in out.rglob("*") if p.is_file())
    assert b"PGDMP" not in blob and b"jpeg-a.jpg" not in blob  # 平文が残っていない
    assert all(k not in blob for k in kms.keys.values())  # KEK を含めない
    report = capsys.readouterr().out
    assert all(base64_key not in report for base64_key in (outer["key"]["wrapped_dek"],))
    assert len(list((tmp_path / "remote").rglob("*.enc"))) == 4  # 外部保管(ローカルで模す)にも置いた


def test_restore_refuses_before_touching_the_db(mod, tmp_path, capsys):
    kms = FakeKmsProvider()
    out, _ = _make(mod, tmp_path, kms)
    # 削除済み一覧が無い・バックアップより古い
    with pytest.raises(mod.Refused, match="--tombstones"):
        _restore(mod, tmp_path, out, kms, None)
    with pytest.raises(mod.Refused, match="古い"):
        _restore(mod, tmp_path, out, kms, _tomb(tmp_path, latest="2026-10-29T00:00:00+00:00"))
    # 鍵違い
    assert _restore(mod, tmp_path, out, FakeKmsProvider(), _tomb(tmp_path)) == 1
    assert "復号できません" in capsys.readouterr().out
    assert mod.created == []


@pytest.mark.parametrize("damage", ["flip", "truncate", "missing", "swap", "manifest_edit", "extra_file_entry"])
def test_tampered_backups_are_rejected(mod, tmp_path, capsys, damage):
    kms = FakeKmsProvider()
    out, _ = _make(mod, tmp_path, kms)
    a, b = out / "receipts" / "a.jpg.enc", out / "receipts" / "b.jpg.enc"
    if damage == "flip":
        data = bytearray((out / "db.dump.enc").read_bytes())
        data[50] ^= 1
        (out / "db.dump.enc").write_bytes(bytes(data))
    elif damage == "truncate":
        (out / "db.dump.enc").write_bytes((out / "db.dump.enc").read_bytes()[:-30])
    elif damage == "missing":
        a.unlink()
    elif damage == "swap":
        da, db_ = a.read_bytes(), b.read_bytes()
        a.write_bytes(db_)
        b.write_bytes(da)
    else:
        outer = json.loads((out / "manifest.json").read_text())
        if damage == "manifest_edit":
            outer["files"]["receipts/a.jpg"]["ciphertext_sha256"] = "0" * 64
        else:
            outer["files"]["receipts/c.jpg"] = dict(outer["files"]["receipts/a.jpg"])
        (out / "manifest.json").write_text(json.dumps(outer))
    assert _restore(mod, tmp_path, out, kms, _tomb(tmp_path)) == 1
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["restored"] is False
    assert mod.created == []  # 復元先の DB を作る前に止まる


def test_wrapped_key_cannot_be_moved_between_backups(mod, tmp_path, capsys):
    kms = FakeKmsProvider()
    one, _ = _make(mod, tmp_path, kms, name="one")
    two, _ = _make(mod, tmp_path, kms, name="two")
    o1, o2 = (json.loads((d / "manifest.json").read_text()) for d in (one, two))
    o2["key"] = o1["key"]
    (two / "manifest.json").write_text(json.dumps(o2))
    assert _restore(mod, tmp_path, two, kms, _tomb(tmp_path)) == 1


def test_missing_images_at_backup_need_explicit_acceptance(mod, tmp_path, capsys):
    kms = FakeKmsProvider()
    out, rc = _make(mod, tmp_path, kms, images=("a.jpg",))  # b.jpg が撮影時点で欠けている
    assert rc == 1
    with pytest.raises(mod.Refused, match="欠けています"):
        _restore(mod, tmp_path, out, kms, _tomb(tmp_path))


def test_production_requires_keys_offsite_and_no_plaintext(mod, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WARILINK_ENVIRONMENT", "production")
    with pytest.raises(mod.Refused):
        mod.cmd_backup(_args(receipts_dir=str(tmp_path), out=str(tmp_path / "p"), insecure_plaintext=True))
    monkeypatch.delenv("WARILINK_BACKUP_KEYS", raising=False)
    with pytest.raises(mod.Refused, match="WARILINK_BACKUP_KEYS"):
        mod.cmd_backup(_args(receipts_dir=str(tmp_path), out=str(tmp_path / "q")))
    # 鍵はあっても外部保管が無ければ不完全(終了コード 4)
    out, rc = _make(mod, tmp_path, FakeKmsProvider())
    assert rc == 4
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["offsite"] == "not_configured"


def test_prune_handles_both_formats_and_leaves_unknown(mod, tmp_path, capsys):
    root = tmp_path / "root"
    root.mkdir()
    for name, fmt, created in (("v1-old", "warilink-backup-1", "2026-01-01T00:00:00+00:00"),
                               ("v2-old", "warilink-backup-2", "2026-01-01T00:00:00+00:00"),
                               ("other", "something-else", "2026-01-01T00:00:00+00:00")):
        (root / name).mkdir()
        (root / name / "manifest.json").write_text(json.dumps({"format": fmt, "created_at": created}))
    assert mod.main(["prune", "--root", str(root)]) == 0
    assert sorted(p.name for p in root.iterdir()) == ["other"]


@pytest.mark.parametrize("case, expected", [("no_offsite", 4), ("upload_failure", 4), ("missing", 1),
                                           ("complete", 0), ("local", 0), ("local_storage", 0)])
def test_backup_status_requires_complete_verified_offsite(mod, tmp_path, monkeypatch, case, expected):
    monkeypatch.setenv("WARILINK_ENVIRONMENT", "development" if case in ("local", "local_storage") else "production")
    runs = []
    monkeypatch.setattr(mod, "record_run", lambda args, db, job, *, ok: runs.append(ok))
    storage = None if case in ("no_offsite", "local") else LocalDirStorage(tmp_path / "remote")
    if case == "upload_failure":
        def fail(*args, **kwargs):
            raise mod.OffsiteError("failed")

        monkeypatch.setattr(mod, "upload", fail)
    _, rc = _make(mod, tmp_path, FakeKmsProvider(), storage=storage,
                  images=("a.jpg",) if case == "missing" else ("a.jpg", "b.jpg"))
    assert rc == expected
    assert runs == ([] if case in ("local", "local_storage") else [case == "complete"])
    if case == "missing":
        # 不完全な画像セットを、manifest付きの復元可能バックアップとして保管しない。
        assert not list((tmp_path / "remote").rglob("manifest.json"))


def test_backup_status_write_failure_is_not_success(mod, tmp_path, monkeypatch):
    monkeypatch.setenv("WARILINK_ENVIRONMENT", "production")

    def fail(*args, **kwargs):
        raise mod.Refused("status write failed")

    monkeypatch.setattr(mod, "record_run", fail)
    with pytest.raises(mod.Refused):
        _make(mod, tmp_path, FakeKmsProvider(), storage=LocalDirStorage(tmp_path / "remote"))


def test_record_run_fails_closed_on_database_error(mod, monkeypatch):
    spec = importlib.util.spec_from_file_location("backup_record_test", SCRIPT)
    assert spec and spec.loader
    actual = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(actual)

    def fail(*args):
        raise subprocess.CalledProcessError(1, "psql")

    monkeypatch.setattr(actual, "psql", fail)
    with pytest.raises(actual.Refused):
        actual.record_run(_args(), "warilink_src", "backup", ok=True)


# ---------------------------------------------------------------- 削除ジャーナル(STEP 01)

def _journal(tmp_path) -> LocalDirJournal:
    return LocalDirJournal(tmp_path / "journal")  # mod のフィクスチャが WARILINK_DELETION_JOURNAL に設定した場所


def _deleted(j: LocalDirJournal, n: int, *, finalize=True) -> uuid.UUID:
    op = uuid.uuid4()
    j.prepare(op, [("identity", uuid.UUID(int=n))])
    if finalize:
        j.finalize(op)
    return op


def _inner(mod, out, kms) -> dict:
    inner, _ = mod.open_encrypted(_args(provider=kms), out, json.loads((out / "manifest.json").read_text()))
    return inner


def test_backup_records_the_journal_checkpoint_and_production_requires_a_journal(mod, tmp_path, monkeypatch):
    j = _journal(tmp_path)
    _deleted(j, 1)
    kms = FakeKmsProvider()
    out, rc = _make(mod, tmp_path, kms)
    assert rc == 0
    assert _inner(mod, out, kms)["journal_checkpoint"] == j.state().checkpoint.to_json()
    assert "journal" not in (out / "manifest.json").read_text()  # 外側(平文)の manifest には出さない
    monkeypatch.delenv("WARILINK_DELETION_JOURNAL")
    monkeypatch.setenv("WARILINK_ENVIRONMENT", "production")
    with pytest.raises(mod.Refused, match="削除ジャーナル"):
        _make(mod, tmp_path, kms, name="prod", storage=LocalDirStorage(tmp_path / "remote"))


@pytest.mark.parametrize("case", ["no_journal", "pending", "other_journal", "tampered", "truncated", "corrupt_head"])
def test_restore_refuses_an_unprovable_journal_before_touching_the_db(mod, tmp_path, monkeypatch, case):
    j = _journal(tmp_path)
    _deleted(j, 1)
    _deleted(j, 2)
    kms = FakeKmsProvider()
    out, _ = _make(mod, tmp_path, kms)
    _deleted(j, 3, finalize=case != "pending")  # バックアップの後の退会
    root = tmp_path / "journal"
    lines = (root / "journal.jsonl").read_text().splitlines()
    if case == "no_journal":
        monkeypatch.delenv("WARILINK_DELETION_JOURNAL")
    elif case == "other_journal":
        other = LocalDirJournal(tmp_path / "other")
        for n in range(1, 6):
            _deleted(other, n)
        monkeypatch.setenv("WARILINK_DELETION_JOURNAL", f"local:{tmp_path / 'other'}")
    elif case == "tampered":
        r = json.loads(lines[0])
        r["targets"][0]["target_id"] = str(uuid.UUID(int=99))
        (root / "journal.jsonl").write_text("\n".join([json.dumps(r), *lines[1:]]) + "\n")
    elif case == "truncated":  # バックアップの時点より短い(連番と HEAD は整合させる)
        (root / "journal.jsonl").write_text(lines[0] + "\n")
        (root / "HEAD").write_text(json.dumps({"format": "warilink-deletion-journal-1", "seq": 1,
                                               "head": json.loads(lines[0])["hash"]}))
    elif case == "corrupt_head":
        (root / "HEAD").write_text("{")
    with pytest.raises(mod.Refused, match="ジャーナル|確定していない"):
        _restore(mod, tmp_path, out, kms, _tomb(tmp_path))
    assert mod.created == []  # 復元先の DB を作る前に止まる


def test_restore_reapplies_every_finalized_deletion_including_after_the_backup(mod, tmp_path):
    j = _journal(tmp_path)
    before = _deleted(j, 1)
    kms = FakeKmsProvider()
    out, _ = _make(mod, tmp_path, kms)
    after = _deleted(j, 2)
    _deleted(j, 3, finalize=False)
    j.abort(uuid.UUID(next(iter(j.state().pending))))  # 確実にロールバックした操作は再適用しない
    summary, rows = mod.journal_rows(_args(), _inner(mod, out, kms))
    assert summary == {"status": "verified", "checkpoint_seq": 2, "journal_seq": 6, "finalized_ops": 2}
    assert sorted((r["op_id"], r["target_id"]) for r in rows) == sorted(
        [(str(before), str(uuid.UUID(int=1))), (str(after), str(uuid.UUID(int=2)))])


def test_legacy_backups_without_a_checkpoint_are_not_silently_trusted(mod, tmp_path, monkeypatch):
    legacy = {"tombstones_latest": None}  # STEP 01 より前の manifest.enc(チェックポイントが無い)
    assert mod.journal_rows(_args(), legacy)[0]["status"] == "verified_without_checkpoint"
    monkeypatch.delenv("WARILINK_DELETION_JOURNAL")
    assert mod.journal_rows(_args(), legacy) == ({"status": "unverified_legacy"}, [])  # 開発では従来どおり
    monkeypatch.setenv("WARILINK_ENVIRONMENT", "production")
    with pytest.raises(mod.Refused, match="--accept-legacy-backup"):
        mod.journal_rows(_args(), legacy)
    monkeypatch.setenv("WARILINK_DELETION_JOURNAL", f"local:{tmp_path / 'journal'}")
    with pytest.raises(mod.Refused, match="--accept-legacy-backup"):
        mod.journal_rows(_args(), legacy)
    assert mod.journal_rows(_args(accept_legacy_backup=True), legacy)[0]["status"] == "verified_without_checkpoint"


# ---------------------------------------------------------------- S3 の外部保管・KMS(STEP 02。偽の S3・KMS、接続なし)

def _last_json(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_backup_to_s3_is_success_only_after_reading_back_the_recorded_versions(mod, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WARILINK_ENVIRONMENT", "production")
    runs = []
    monkeypatch.setattr(mod, "record_run", lambda args, db, job, *, ok: runs.append(ok))
    s3, kms = FakeS3(), FakeKmsProvider()
    out, rc = _make(mod, tmp_path, kms, storage=S3Storage(s3, S3_LOC))
    assert rc == 0 and runs == [True]
    off = _last_json(capsys)["offsite"]
    assert off["readback"] == "verified" and off["uploaded"] == 5
    receipt = off["receipt"]
    assert receipt["version_id"] and receipt["key"].endswith("/upload-receipt.json")
    puts = [k for op, k in s3.calls if op == "put"]
    assert puts[-2].endswith("/manifest.json") and puts[-1].endswith("/upload-receipt.json")  # manifest → 受領記録
    blob = b"".join(v["data"] for vs in s3.store.values() for v in vs)
    assert b"PGDMP" not in blob and b"jpeg-a.jpg" not in blob  # 外部には暗号文だけ
    # 隔離した取得: 記録した世代を空のディレクトリへ取り、検証する(DB には触れない)
    dest = tmp_path / "fetched"
    problems, summary = mod.verify_offsite_copy(_args(provider=kms), S3Storage(s3, S3_LOC), off["prefix"],
                                                receipt["version_id"], dest, expected_receipt_sha256=receipt["sha256"])
    assert problems == [] and summary["files"] == 5 and summary["journal_checkpoint_seq"] == 0
    assert mod.created == []
    # 受領記録を差し替えられても、記録した SHA-256 と合わなければ拒否する
    s3.overwrite(f"backups/prod/{receipt['key']}", b'{"format": "warilink-offsite-receipt-1"}')
    latest = s3.store[f"backups/prod/{receipt['key']}"][-1]["id"]
    problems, _ = mod.verify_offsite_copy(_args(provider=kms), S3Storage(s3, S3_LOC), off["prefix"], latest,
                                          tmp_path / "f2", expected_receipt_sha256=receipt["sha256"])
    assert problems and "受領記録" in problems[0]


@pytest.mark.parametrize("damage", ["tampered_read", "access_denied_on_read", "journal_swapped"])
def test_backup_without_a_verified_readback_is_not_success(mod, tmp_path, monkeypatch, capsys, damage):
    monkeypatch.setenv("WARILINK_ENVIRONMENT", "production")
    runs = []
    monkeypatch.setattr(mod, "record_run", lambda args, db, job, *, ok: runs.append(ok))
    s3 = FakeS3()
    if damage == "tampered_read":
        orig = s3.get_object

        def get_object(**kw):  # 読み戻しのときだけ db.dump.enc を壊す
            r = orig(**kw)
            if kw["Key"].endswith("db.dump.enc"):
                data = r["Body"].read()
                r["Body"] = io.BytesIO(bytes([data[0] ^ 1]) + data[1:])
                r.pop("ChecksumSHA256")
            return r

        s3.get_object = get_object
    elif damage == "access_denied_on_read":
        s3.fail["get_object"] = ["AccessDenied"] * 20
    else:  # バックアップの後、読み戻しの前に削除ジャーナルを別物へ差し替えた
        j = _journal(tmp_path)
        _deleted(j, 1)
        orig_upload = mod.upload

        def upload_then_swap(*a, **kw):
            r = orig_upload(*a, **kw)
            monkeypatch.setenv("WARILINK_DELETION_JOURNAL", f"local:{tmp_path / 'other-journal'}")
            return r

        monkeypatch.setattr(mod, "upload", upload_then_swap)
    _, rc = _make(mod, tmp_path, FakeKmsProvider(), storage=S3Storage(s3, S3_LOC))
    assert rc == 4 and runs == [False]  # 成功を記録しない
    report = capsys.readouterr().out
    assert "offsite_failed" in report and "backups/prod" not in report and FAKE_BUCKET not in report


def test_production_refuses_a_bucket_without_versioning_lock_and_kms(mod, tmp_path, monkeypatch):
    monkeypatch.setenv("WARILINK_ENVIRONMENT", "production")
    s3 = FakeS3(versioning=False, lock=None, sse="AES256")
    with pytest.raises(mod.Refused, match="前提条件"):
        _make(mod, tmp_path, FakeKmsProvider(), storage=S3Storage(s3, S3_LOC))
    assert not [c for c in s3.calls if c[0] == "put"]


def test_kms_backups_restore_by_manifest_provider_and_env_key_backups_still_open(mod, tmp_path, monkeypatch, capsys):
    kms_client = FakeKms()
    arn = kms_client.create_key("alias/warilink-backup")
    monkeypatch.setattr(mod.AwsKmsProvider, "from_env",
                        classmethod(lambda cls, env=None, client=None: cls(
                            kms_client, os.environ.get("WARILINK_BACKUP_KMS_KEY_ID"))))
    env_key = base64.b64encode(os.urandom(32)).decode()
    # 1. 環境変数の鍵で作った(STEP 02 より前の)バックアップ
    monkeypatch.setenv("WARILINK_BACKUP_KEYS", f"old:{env_key}")
    monkeypatch.setenv("WARILINK_BACKUP_ACTIVE_KEY", "old")
    legacy, rc = _make(mod, tmp_path, None, name="legacy")
    assert rc == 0 and json.loads((legacy / "manifest.json").read_text())["key"]["provider"] == "env"
    # 2. KMS に切り替えた後のバックアップ(manifest には ARN を書く)
    monkeypatch.setenv("WARILINK_BACKUP_KMS_KEY_ID", "alias/warilink-backup")
    kmsb, rc = _make(mod, tmp_path, None, name="kms")
    key = json.loads((kmsb / "manifest.json").read_text())["key"]
    assert rc == 0 and key == {**key, "provider": "aws-kms", "key_id": arn}
    # 復元は manifest の提供元で鍵をほどく(今の設定が KMS でも、古い環境変数の鍵のバックアップを開ける)
    for d in (legacy, kmsb):
        inner, dek = mod.open_encrypted(_args(), d, json.loads((d / "manifest.json").read_text()))
        assert mod.verify_encrypted(d, json.loads((d / "manifest.json").read_text()), inner, dek) == []
    # 古い環境変数の鍵を消すと、その鍵のバックアップは開けない(拒否。KMS のバックアップは開ける)
    monkeypatch.delenv("WARILINK_BACKUP_KEYS")
    with pytest.raises(mod.Refused, match="WARILINK_BACKUP_KEYS"):
        mod.open_encrypted(_args(), legacy, json.loads((legacy / "manifest.json").read_text()))
    mod.open_encrypted(_args(), kmsb, json.loads((kmsb / "manifest.json").read_text()))
    # テスト用の偽 KMS(fake-kms)で作ったものは、提供元を渡さなければ開かない
    fake, _ = _make(mod, tmp_path, FakeKmsProvider(), name="fake")
    with pytest.raises(mod.Refused, match="未対応の鍵の提供元"):
        mod.open_encrypted(_args(), fake, json.loads((fake / "manifest.json").read_text()))
    # 鍵 ID を書き換えた manifest は KMS が拒否する
    outer = json.loads((kmsb / "manifest.json").read_text())
    other = kms_client.create_key()
    outer["key"]["key_id"] = other
    with pytest.raises(BackupCryptoError):
        mod.open_encrypted(_args(), kmsb, outer)


FETCH = SCRIPT.parent / "warilink_fetch_backup.py"


@pytest.fixture
def fetch_mod(mod):
    sys.modules["warilink_backup"] = mod  # 同じ(DB を差し替えた)モジュールを使わせる
    spec = importlib.util.spec_from_file_location("warilink_fetch_under_test", FETCH)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    yield m
    sys.modules.pop("warilink_backup", None)


def test_fetch_script_verifies_an_isolated_copy_and_refuses_unsafe_requests(mod, fetch_mod, tmp_path, capsys):
    s3, kms = FakeS3(), FakeKmsProvider()
    _, rc = _make(mod, tmp_path, kms, storage=S3Storage(s3, S3_LOC))
    off = _last_json(capsys)["offsite"]
    args = ["--from", "s3://unused/x", "--backup", off["prefix"], "--receipt-version", off["receipt"]["version_id"],
            "--receipt-sha256", off["receipt"]["sha256"]]
    st = S3Storage(s3, S3_LOC)
    out = tmp_path / "fetched"
    assert fetch_mod.main([*args, "--out", str(out)], storage=st, provider=kms) == 0
    report = _last_json(capsys)
    assert report["verified"] is True and report["files"] == 5 and FAKE_BUCKET not in json.dumps(report)
    assert sorted(p.name for p in out.iterdir()) == ["db.dump.enc", "manifest.enc", "manifest.json", "receipts"]
    assert fetch_mod.main([*args, "--out", str(out)], storage=st, provider=kms) == 3  # 取得先が空でない
    assert fetch_mod.main([*args[:4], "--out", str(tmp_path / "x")], storage=st, provider=kms) == 3  # 世代なし
    assert fetch_mod.main(["--from", "s3://bucket", "--backup", "p", "--receipt-version", "v", "--out",
                           str(tmp_path / "y")]) == 3  # 接頭辞の無い設定・接続の許可なし
    s3.tamper_get.add(f"backups/prod/{off['prefix']}/receipts/a.jpg.enc")
    assert fetch_mod.main([*args, "--out", str(tmp_path / "z")], storage=st, provider=kms) == 1
    assert _last_json(capsys)["verified"] is False
    assert mod.created == []  # 取得と検証では DB に触れない


def test_backup_status_is_recorded_with_the_record_user(mod, tmp_path, monkeypatch):
    """STEP 04: ダンプのロール(読み取り専用)とは別のロールで成否を記録できる。既定は従来どおり --user。"""
    users = []

    def psql(args, db, sql):
        users.append(args.user)
        return ""

    spec = importlib.util.spec_from_file_location("backup_record_user_test", SCRIPT)
    assert spec and spec.loader
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    monkeypatch.setattr(real, "psql", psql)
    real.record_run(_args(user="warilink_backup", record_user="warilink_app"), "warilink", "backup", ok=True)
    real.record_run(_args(user="warilink_backup"), "warilink", "backup", ok=False)
    assert users == ["warilink_app", "warilink_backup"]
    (tmp_path / "root").mkdir()
    assert real.main(["--record-user", "warilink_app", "prune", "--root", str(tmp_path / "root")]) == 0  # CLI の引数
