"""STEP 04: 隔離した復元の検証(scripts/warilink_restore_verify.py)。

暗号化・受領記録・取得と検証は本物(ローカルの外部保管)。DB への復元(pg_restore)だけを差し替える。
実際のコンテナでの実行は docs/TEST_AND_DELIVERY.md「STEP 04」に記録する。
"""

import importlib.util
import json
import sys
import uuid

import pytest

from app.ops.backup_crypto import FakeKmsProvider
from app.ops.deletion_journal import LocalDirJournal
from app.ops.offsite import LocalDirStorage

from .test_backup_script import SCRIPT, _make, mod  # noqa: F401 - mod はフィクスチャ

VERIFY = SCRIPT.parent / "warilink_restore_verify.py"


@pytest.fixture
def rv(mod, monkeypatch):  # noqa: F811
    sys.modules["warilink_backup"] = mod
    spec = importlib.util.spec_from_file_location("warilink_restore_verify_under_test", VERIFY)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    drops, restores = [], []

    def cmd_restore(ns):
        restores.append(ns)
        print(json.dumps({"restored": True, "checks": {"row_counts": True, "deletions_hold": True},
                          "journal": {"status": "verified"}, "rows": 17, "images": 2,
                          "reapplied": {"identity": 1, "destination": 0, "group": 0}}))
        return 0

    monkeypatch.setattr(mod, "cmd_restore", cmd_restore)
    monkeypatch.setattr(mod, "cmd_drop", lambda ns: drops.append(ns.target_db) or 0)
    m.drops, m.restores = drops, restores
    yield m
    sys.modules.pop("warilink_backup", None)


def _backup(mod, tmp_path, capsys, kms):  # noqa: F811
    j = LocalDirJournal(tmp_path / "journal")
    op = uuid.uuid4()
    j.prepare(op, [("identity", uuid.UUID(int=1))])
    j.finalize(op)
    _, rc = _make(mod, tmp_path, kms, storage=LocalDirStorage(tmp_path / "remote"))
    assert rc == 0
    report = tmp_path / "last-backup.json"
    report.write_text(capsys.readouterr().out)
    return report, j


def _args(tmp_path, report, **over):
    base = {"--source": f"local:{tmp_path / 'remote'}", "--from-report": str(report),
            "--container": "warilink-drill-db", "--target-db": "warilink_restore_verify_t",
            "--workdir": str(tmp_path / "work"), "--receipt-out": str(tmp_path / "receipt.json")}
    base.update(over)
    return [x for k, v in base.items() if v is not None for x in (k, v)]


def test_plan_mode_checks_guards_and_touches_nothing(rv, mod, tmp_path, capsys):  # noqa: F811
    kms = FakeKmsProvider()
    report, _ = _backup(mod, tmp_path, capsys, kms)
    assert rv.main(_args(tmp_path, report), provider=kms) == 0
    out = json.loads((tmp_path / "receipt.json").read_text())
    assert out["result"] == "PLANNED" and out["mode"] == "plan" and out["backup"]["receipt_version_pinned"] is False
    assert not (tmp_path / "work").exists() and rv.restores == [] and rv.drops == [] and mod.created == []


@pytest.mark.parametrize("over, needle", [
    ({"--container": "warilink-db-1"}, "隔離した一時コンテナ"),
    ({"--container": "hakobu-postgres"}, "隔離した一時コンテナ"),
    ({"--container": "warilink-prod-db-1"}, "隔離した一時コンテナ"),
    ({"--target-db": "warilink"}, "warilink_restore_"),
    ({"--source": "s3://warilink-backups/prod"}, "WARILINK_AWS_LIVE"),
])
def test_unsafe_targets_are_refused_before_any_db_access(rv, mod, tmp_path, capsys, over, needle):  # noqa: F811
    kms = FakeKmsProvider()
    report, _ = _backup(mod, tmp_path, capsys, kms)
    assert rv.main(_args(tmp_path, report, **over) + ["--execute"], provider=kms) == 3
    out = json.loads((tmp_path / "receipt.json").read_text())
    assert out["result"] == "REFUSED" and needle in out["reason"]
    assert rv.restores == [] and mod.created == []


def test_missing_journal_keys_or_a_used_workdir_are_refused(rv, mod, tmp_path, capsys, monkeypatch):  # noqa: F811
    kms = FakeKmsProvider()
    report, _ = _backup(mod, tmp_path, capsys, kms)
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "x").write_text("x")
    assert rv.main(_args(tmp_path, report) + ["--execute"], provider=kms) == 3  # 作業ディレクトリが空でない
    (tmp_path / "work" / "x").unlink()
    monkeypatch.delenv("WARILINK_DELETION_JOURNAL")
    assert rv.main(_args(tmp_path, report) + ["--execute"], provider=kms) == 3
    assert "削除ジャーナル" in json.loads((tmp_path / "receipt.json").read_text())["reason"]
    monkeypatch.setenv("WARILINK_DELETION_JOURNAL", f"local:{tmp_path / 'journal'}")
    monkeypatch.delenv("WARILINK_BACKUP_KEYS", raising=False)
    monkeypatch.delenv("WARILINK_BACKUP_KMS_KEY_ID", raising=False)
    assert rv.main(_args(tmp_path, report) + ["--execute"]) == 3  # 鍵が無い
    assert "鍵" in json.loads((tmp_path / "receipt.json").read_text())["reason"]
    assert rv.main(_args(tmp_path, None, **{"--from-report": None}) + ["--execute"], provider=kms) == 3
    assert rv.restores == [] and mod.created == []


def test_execute_records_every_step_and_cleans_up_only_the_isolated_copy(rv, mod, tmp_path, capsys):  # noqa: F811
    kms = FakeKmsProvider()
    report, _ = _backup(mod, tmp_path, capsys, kms)
    assert rv.main(_args(tmp_path, report) + ["--execute"], provider=kms) == 0
    out = json.loads((tmp_path / "receipt.json").read_text())
    assert out["result"] == "PASS" and [s["name"] for s in out["steps"]] == [
        "journal_integrity", "fetch_and_verify", "restore_and_reapply", "verify_restored"]
    assert all(s["status"] == "PASS" for s in out["steps"]) and out["measured_restore_seconds"] is not None
    assert out["restored"] == {"rows": 17, "images": 2, "reapplied": {"identity": 1, "destination": 0, "group": 0}}
    assert rv.drops == ["warilink_restore_verify_t"] and not (tmp_path / "work").exists()
    tomb = rv.restores[0].tombstones
    assert tomb.endswith("tombstones.jsonl")  # 削除済み一覧はジャーナルの確定済みの記録から作る
    assert rv.restores[0].target_db == "warilink_restore_verify_t"
    assert "wrapped_dek" not in (tmp_path / "receipt.json").read_text()


def test_execute_fails_closed_at_each_step(rv, mod, tmp_path, capsys, monkeypatch):  # noqa: F811
    kms = FakeKmsProvider()
    report, j = _backup(mod, tmp_path, capsys, kms)
    # 1. 確定していない削除がある → 復元しない
    j.prepare(uuid.uuid4(), [("identity", uuid.UUID(int=2))])
    assert rv.main(_args(tmp_path, report) + ["--execute"], provider=kms) == 1
    out = json.loads((tmp_path / "receipt.json").read_text())
    assert [s["status"] for s in out["steps"]] == ["FAIL"] and rv.restores == [] and rv.drops == []
    assert out["measured_restore_seconds"] is None
    j.abort(uuid.UUID(next(iter(j.state().pending))))
    # 2. 外部保管のファイルが書き換えられている → 取得の検証で止まる
    remote = next((tmp_path / "remote").rglob("db.dump.enc"))
    original = remote.read_bytes()
    remote.write_bytes(b"x" + original[1:])
    assert rv.main(_args(tmp_path, report) + ["--execute"], provider=kms) == 1
    out = json.loads((tmp_path / "receipt.json").read_text())
    assert [s["status"] for s in out["steps"]] == ["PASS", "FAIL"] and rv.restores == []
    remote.write_bytes(original)
    # 3. 復元の検査が失敗 → FAIL。隔離した復元先は消す
    monkeypatch.setattr(mod, "cmd_restore", lambda ns: print(json.dumps(
        {"restored": False, "checks": {"deletions_hold": False}, "journal": {"status": "verified"}})) or 1)
    assert rv.main(_args(tmp_path, report) + ["--execute"], provider=kms) == 1
    out = json.loads((tmp_path / "receipt.json").read_text())
    assert out["result"] == "FAIL" and out["steps"][-1]["name"] == "restore_and_reapply"
    assert rv.drops == ["warilink_restore_verify_t"] and not (tmp_path / "work").exists()


def test_tombstones_from_the_journal_use_the_finalize_time(rv, tmp_path):
    j = LocalDirJournal(tmp_path / "j")
    a, b = uuid.uuid4(), uuid.uuid4()
    j.prepare(a, [("identity", uuid.UUID(int=1)), ("destination", uuid.UUID(int=2))])
    j.finalize(a)
    j.prepare(b, [("group", uuid.UUID(int=3))])
    j.abort(b)
    st = j.state()
    rows = rv.tombstones_from_journal(st)
    fin = next(r["at"] for r in st.records if r["type"] == "finalize")
    assert rows == [{"kind": "identity", "target_id": str(uuid.UUID(int=1)), "op_id": str(a), "created_at": fin},
                    {"kind": "destination", "target_id": str(uuid.UUID(int=2)), "op_id": str(a), "created_at": fin}]
