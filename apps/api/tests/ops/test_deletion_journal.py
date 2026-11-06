"""STEP 01: 削除ジャーナル(app/ops/deletion_journal.py)の形式・耐久化・改ざんと欠落の検出(DB を使わない)。

DB と組み合わせた削除の経路(prepare → commit → finalize と、その途中の失敗)は tests/api/test_account_deletion.py。
"""

import json
import multiprocessing
import os
import stat
import uuid
from pathlib import Path

import pytest

from app.ops.deletion_journal import (
    EMPTY,
    GENESIS,
    Checkpoint,
    JournalConfigError,
    JournalCorrupt,
    JournalError,
    JournalUnavailable,
    LocalDirJournal,
    MemoryJournal,
    journal_from_url,
    journal_problems,
    record_hash,
    verify_checkpoint,
)

A, B = uuid.UUID(int=1), uuid.UUID(int=2)


def _lines(root: Path) -> list[str]:
    return (root / "journal.jsonl").read_text().splitlines()


def _write_lines(root: Path, lines: list[str]) -> None:
    (root / "journal.jsonl").write_text("".join(ln + "\n" for ln in lines))


@pytest.fixture(params=["memory", "local"])
def journal(request, tmp_path):
    return MemoryJournal() if request.param == "memory" else LocalDirJournal(tmp_path / "j")


def test_two_phase_records_and_idempotent_retries(journal):
    op1, op2, op3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert journal.state().checkpoint == EMPTY
    journal.prepare(op1, [("identity", A), ("destination", B)])
    journal.prepare(op1, [("identity", A), ("destination", B)])  # 再試行は追記しない
    journal.prepare(op2, [("group", A)])
    journal.prepare(op3, [("identity", B)])
    journal.finalize(op1)
    journal.finalize(op1)  # 冪等
    journal.abort(op2)
    st = journal.state()
    assert st.checkpoint.seq == 5 and len(st.records) == 5
    assert st.finalized == {str(op1): [("identity", str(A)), ("destination", str(B))]}
    assert set(st.pending) == {str(op3)} and st.aborted == {str(op2)}
    assert st.finalized_targets() == [("identity", str(A)), ("destination", str(B))]
    # 取り消した操作は確定・再開できず、prepare の無い操作は確定できない
    with pytest.raises(JournalError):
        journal.finalize(op2)
    with pytest.raises(JournalError):
        journal.prepare(op2, [("group", A)])
    with pytest.raises(JournalError):
        journal.finalize(uuid.uuid4())
    with pytest.raises(ValueError):
        journal.prepare(uuid.uuid4(), [("receipt", A)])  # 未対応の種類
    with pytest.raises(ValueError):
        journal.prepare(uuid.uuid4(), [])


def test_records_hold_only_ids_and_utc_times_and_are_hash_chained(journal):
    op = uuid.uuid4()
    journal.prepare(op, [("identity", A)])
    journal.finalize(op)
    recs = journal.state().records
    assert recs[0]["prev"] == GENESIS and recs[1]["prev"] == recs[0]["hash"]
    for r in recs:
        assert r["hash"] == record_hash(r)
        assert r["at"].endswith("+00:00")  # UTC
        assert set(r) <= {"v", "seq", "type", "op_id", "at", "targets", "prev", "hash"}
    assert recs[0]["targets"] == [{"kind": "identity", "target_id": str(A)}]


def test_local_journal_is_durable_private_and_survives_reopen(tmp_path):
    root = tmp_path / "j"
    op = uuid.uuid4()
    LocalDirJournal(root).prepare(op, [("identity", A)])
    LocalDirJournal(root).finalize(op)  # 別のインスタンス(別プロセスの再起動を模す)
    st = LocalDirJournal(root).state()
    assert str(op) in st.finalized
    head = json.loads((root / "HEAD").read_text())
    assert head == {"format": "warilink-deletion-journal-1", "seq": 2, "head": st.records[-1]["hash"]}
    for name in ("journal.jsonl", "HEAD"):
        assert stat.S_IMODE(os.stat(root / name).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700
    with pytest.raises(JournalConfigError):
        LocalDirJournal(Path("relative"))


def _three(root: Path) -> list[str]:
    j = LocalDirJournal(root)
    for _ in range(3):
        j.prepare(uuid.uuid4(), [("identity", uuid.uuid4())])
    return _lines(root)


@pytest.mark.parametrize("damage", ["edit_target", "drop_middle", "reorder", "drop_tail", "head_missing",
                                    "head_other", "garbage"])
def test_tampering_and_missing_records_are_detected(tmp_path, damage):
    root = tmp_path / "j"
    lines = _three(root)
    if damage == "edit_target":
        r = json.loads(lines[1])
        r["targets"][0]["target_id"] = str(uuid.uuid4())  # hash を直さない書き換え
        lines[1] = json.dumps(r)
    elif damage == "drop_middle":
        del lines[1]
    elif damage == "reorder":
        lines[0], lines[1] = lines[1], lines[0]
    elif damage == "drop_tail":
        del lines[2]
        del lines[1]  # HEAD(seq 3)より 2 件少ない = 切り詰め
    elif damage == "head_missing":
        (root / "HEAD").unlink()
    elif damage == "head_other":
        (root / "HEAD").write_text(json.dumps(Checkpoint(3, "f" * 64).to_json()))
    else:
        lines[2] = "{not json"
    _write_lines(root, lines)
    with pytest.raises(JournalCorrupt):
        LocalDirJournal(root).state()
    with pytest.raises(JournalCorrupt):  # 壊れた記録には追記しない
        LocalDirJournal(root).prepare(uuid.uuid4(), [("identity", A)])


def test_recalculated_hashes_still_break_the_chain(tmp_path):
    """1 件だけ書き換えて hash を計算し直しても、次の記録の prev と HEAD が合わない。"""
    root = tmp_path / "j"
    lines = _three(root)
    r = json.loads(lines[1])
    r["targets"][0]["target_id"] = str(uuid.uuid4())
    r["hash"] = record_hash(r)
    lines[1] = json.dumps(r)
    _write_lines(root, lines)
    with pytest.raises(JournalCorrupt, match="seq 3"):
        LocalDirJournal(root).state()


def test_crash_windows_are_recovered_without_losing_records(tmp_path):
    root = tmp_path / "j"
    j = LocalDirJournal(root)
    lines = _three(root)
    head_before_last = json.loads(lines[1])["hash"]
    # 1. 記録を書いた後、HEAD を更新する前に落ちた: HEAD が 1 つ遅れているだけなら受け入れる
    (root / "HEAD").write_text(json.dumps(Checkpoint(2, head_before_last).to_json()))
    assert j.state().checkpoint.seq == 3
    # 2. 記録の途中で落ちた(最後の行が改行で終わらない): HEAD がその前を指していれば読み飛ばし、次の追記で切り捨てる
    (root / "HEAD").write_text(json.dumps(Checkpoint(3, json.loads(lines[2])["hash"]).to_json()))
    with (root / "journal.jsonl").open("a") as f:
        f.write('{"v": 1, "seq": 4, "type": "prep')
    assert j.state().checkpoint.seq == 3
    op = uuid.uuid4()
    j.prepare(op, [("group", A)])
    st = j.state()
    assert st.checkpoint.seq == 4 and str(op) in st.pending
    assert all(json.loads(ln) for ln in _lines(root))  # 壊れた行は残っていない


def test_checkpoint_from_backup_must_be_part_of_the_journal(tmp_path):
    j = LocalDirJournal(tmp_path / "j")
    j.prepare(uuid.uuid4(), [("identity", A)])
    cp = j.state().checkpoint
    j.prepare(uuid.uuid4(), [("identity", B)])  # バックアップの後の削除
    verify_checkpoint(j.state(), cp)
    verify_checkpoint(j.state(), EMPTY)
    other = LocalDirJournal(tmp_path / "other")  # 別のジャーナル(差し替え)
    other.prepare(uuid.uuid4(), [("identity", A)])
    with pytest.raises(JournalCorrupt):
        verify_checkpoint(other.state(), cp)
    with pytest.raises(JournalCorrupt):
        verify_checkpoint(LocalDirJournal(tmp_path / "empty").state(), cp)  # バックアップより短い
    with pytest.raises(JournalCorrupt):
        Checkpoint.from_json({"format": "x", "seq": 1, "head": "0" * 64})


def test_unwritable_journal_is_unavailable_not_success(tmp_path):
    root = tmp_path / "j"
    j = LocalDirJournal(root)
    j.prepare(uuid.uuid4(), [("identity", A)])
    os.chmod(root / "journal.jsonl", 0o400)
    try:
        with pytest.raises(JournalUnavailable):
            j.prepare(uuid.uuid4(), [("identity", B)])
    finally:
        os.chmod(root / "journal.jsonl", 0o600)
    assert j.state().checkpoint.seq == 1  # 失敗した追記は残らない
    (tmp_path / "file-not-dir").write_text("")
    with pytest.raises(JournalUnavailable):  # 置き場所を作れない
        LocalDirJournal(tmp_path / "file-not-dir" / "x").state()


def _append_many(root: str, n: int) -> None:
    j = LocalDirJournal(Path(root))
    for _ in range(n):
        op = uuid.uuid4()
        j.prepare(op, [("identity", uuid.uuid4())])
        j.finalize(op)


def test_concurrent_processes_keep_one_valid_chain(tmp_path):
    root = tmp_path / "j"
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_append_many, args=(str(root), 10)) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    st = LocalDirJournal(root).state()
    assert st.checkpoint.seq == 80 and len(st.finalized) == 40 and not st.pending


def test_configuration():
    assert journal_from_url(None) is None and journal_from_url("") is None
    assert isinstance(journal_from_url("local:/srv/j"), LocalDirJournal)
    # STEP 02: s3://バケット/接頭辞 を受け付ける(作るだけでは接続しない)。接頭辞の無いもの・不正な名前は拒否
    s3 = journal_from_url("s3://warilink-journal-test/deletions")
    assert s3 is not None and s3.name == "s3" and s3.client_ is None
    for bad in ("s3://bucket", "s3://Bad_Bucket/j", "s3://bucket/../j", "gs://bucket/j", "local:", "local:relative",
                "/srv/j"):
        with pytest.raises(JournalConfigError):
            journal_from_url(bad)
        assert journal_problems(bad)
        assert "bucket" not in " ".join(journal_problems(bad))  # 値をメッセージに出さない
    assert journal_problems(None) and journal_problems("local:/srv/j") == []
