"""STEP 02: S3 の外部保管(app/ops/s3_storage.py)と S3 の削除ジャーナル。

AWS には接続しない。boto3 と同じ呼び方をするメモリ上の偽 S3(FakeS3: バージョン管理・条件付き書き込み・
SHA-256 の検証・一覧のページ分け・エラーの注入)を使い、ソケットの接続をテストの間は禁止する。
"""

import base64
import hashlib
import importlib.util
import io
import json
import socket
import sys
import uuid
from pathlib import Path

import pytest

from app.ops import s3_storage
from app.ops.deletion_journal import JournalCorrupt, JournalUnavailable
from app.ops.offsite import OffsiteError, fetch, put_receipt, read_receipt, storage_from_env, upload
from app.ops.s3_storage import (
    AwsUnavailable,
    S3Journal,
    S3Location,
    S3Storage,
    aws_client,
    parse_s3_url,
)

BUCKET = "warilink-test-bucket-name"
LOC = S3Location(BUCKET, "backups/prod")


class ClientError(Exception):
    """botocore の ClientError と同じく response["Error"]["Code"] を持つ。

    メッセージには秘密を入れて、表示されないことを確かめる。"""

    def __init__(self, code: str):
        super().__init__(f"{code}: bucket={BUCKET} secret-token=AKIASECRET")
        self.response = {"Error": {"Code": code, "Message": f"bucket {BUCKET} denied"}}


class _Body:
    def __init__(self, data: bytes):
        self._b = io.BytesIO(data)

    def read(self, n: int = -1) -> bytes:
        return self._b.read(n)


def _b64sha(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


class FakeS3:
    """S3 の必要な部分だけ。キーごとに世代の一覧を持つ。ETag はわざと内容と無関係な値にする(使わないことの確認)。"""

    def __init__(self, *, versioning=True, lock=("COMPLIANCE", 30), sse="aws:kms", page=3):
        self.versioning, self.lock, self.sse, self.page = versioning, lock, sse, page
        self.store: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail: dict[str, list[str | None]] = {}  # 操作 → 順に出すエラーコード(None は成功)
        self.tamper_get: set[str] = set()  # 読み戻しで中身を 1 バイト変える

    def _maybe_fail(self, op):
        codes = self.fail.get(op)
        if codes:
            code = codes.pop(0)
            if code:
                raise ClientError(code)

    def _latest(self, key):
        vs = self.store.get(key) or []
        return vs[-1] if vs else None

    def put_object(self, *, Bucket, Key, Body, ChecksumSHA256=None, Metadata=None, IfNoneMatch=None, IfMatch=None):
        assert Bucket == BUCKET
        self.calls.append(("put", Key))
        self._maybe_fail("put_object")
        data = Body.read() if hasattr(Body, "read") else bytes(Body)
        if ChecksumSHA256 is not None and ChecksumSHA256 != _b64sha(data):
            raise ClientError("BadDigest")
        cur = self._latest(Key)
        live = cur is not None and not cur["marker"]
        if IfNoneMatch == "*" and live:
            raise ClientError("PreconditionFailed")
        if IfMatch is not None and (not live or cur["etag"] != IfMatch):
            raise ClientError("PreconditionFailed")
        v = {"id": uuid.uuid4().hex if self.versioning else "null", "data": data, "marker": False,
             "etag": f'"{uuid.uuid4().hex}"', "sha": _b64sha(data)}
        if self.versioning:
            self.store.setdefault(Key, []).append(v)
        else:
            self.store[Key] = [v]
        out = {"ETag": v["etag"], "ChecksumSHA256": v["sha"]}
        if self.versioning:
            out["VersionId"] = v["id"]
        return out

    def head_object(self, *, Bucket, Key, ChecksumMode=None):
        self.calls.append(("head", Key))
        self._maybe_fail("head_object")
        cur = self._latest(Key)
        if cur is None or cur["marker"]:
            raise ClientError("404")
        return {"VersionId": cur["id"], "ETag": cur["etag"], "ChecksumSHA256": cur["sha"]}

    def get_object(self, *, Bucket, Key, VersionId=None, ChecksumMode=None):
        self.calls.append(("get", Key))
        self._maybe_fail("get_object")
        vs = self.store.get(Key) or []
        if VersionId is None:
            v = vs[-1] if vs else None
            if v is None or v["marker"]:
                raise ClientError("NoSuchKey")
        else:
            v = next((x for x in vs if x["id"] == VersionId), None)
            if v is None:
                raise ClientError("NoSuchVersion")
        data = v["data"]
        if Key in self.tamper_get:
            data = bytes([data[0] ^ 1]) + data[1:]
        return {"Body": _Body(data), "VersionId": v["id"], "ETag": v["etag"], "ChecksumSHA256": v["sha"]}

    def list_object_versions(self, *, Bucket, Prefix, KeyMarker=None, VersionIdMarker=None):
        self._maybe_fail("list_object_versions")
        flat = [(k, v) for k in sorted(self.store) if k.startswith(Prefix) for v in self.store[k]]
        start = int(KeyMarker) if KeyMarker else 0
        chunk = flat[start:start + self.page]
        more = start + self.page < len(flat)
        return {"Versions": [{"Key": k, "VersionId": v["id"]} for k, v in chunk if not v["marker"]],
                "DeleteMarkers": [{"Key": k, "VersionId": v["id"]} for k, v in chunk if v["marker"]],
                "IsTruncated": more, "NextKeyMarker": str(start + self.page) if more else None,
                "NextVersionIdMarker": "x" if more else None}

    def get_bucket_versioning(self, *, Bucket):
        self._maybe_fail("get_bucket_versioning")
        return {"Status": "Enabled"} if self.versioning else {}

    def get_object_lock_configuration(self, *, Bucket):
        self._maybe_fail("get_object_lock_configuration")
        if not self.lock:
            raise ClientError("ObjectLockConfigurationNotFoundError")
        mode, days = self.lock
        return {"ObjectLockConfiguration": {"ObjectLockEnabled": "Enabled",
                                            "Rule": {"DefaultRetention": {"Mode": mode, "Days": days}}}}

    def get_bucket_encryption(self, *, Bucket):
        self._maybe_fail("get_bucket_encryption")
        return {"ServerSideEncryptionConfiguration": {"Rules": [
            {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": self.sse}}]}}

    # 攻撃・事故の再現(権限のある誰かが直接操作した)
    def overwrite(self, key, data: bytes):
        self.store[key].append({"id": uuid.uuid4().hex, "data": data, "marker": False, "etag": '"x"',
                                "sha": _b64sha(data)})

    def delete_marker(self, key):
        self.store[key].append({"id": uuid.uuid4().hex, "data": b"", "marker": True, "etag": '"x"', "sha": ""})

    def drop(self, key):
        del self.store[key]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """このモジュールのテストの間は、どこにも接続させない(AWS へ送っていないことの保証)。"""
    def refuse(*a, **kw):
        raise AssertionError("ネットワークに接続しようとしました")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield
    assert "boto3" not in sys.modules


def _no_secret(msg: str):
    assert BUCKET not in msg and "AKIASECRET" not in msg and "backups/prod" not in msg


# ---------------------------------------------------------------- 設定・接続の許可


def test_url_syntax():
    assert parse_s3_url("s3://warilink-backups/prod/db") == S3Location("warilink-backups", "prod/db")
    assert parse_s3_url("s3://warilink-backups/prod/") == S3Location("warilink-backups", "prod")
    for bad in ("s3://warilink-backups", "s3://warilink-backups/", "s3://Bad_Bucket/x", "s3://ab/x",
                "s3://warilink-backups/../x", "s3://warilink-backups//x", "s3://warilink-backups/a b",
                "local:/x", "https://warilink-backups.s3.amazonaws.com/x"):
        with pytest.raises(OffsiteError):
            parse_s3_url(bad)


def test_no_aws_client_without_explicit_live_flag(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIASECRET")  # 資格情報があっても自動では接続しない
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    with pytest.raises(AwsUnavailable, match="WARILINK_AWS_LIVE=1"):
        aws_client("s3")
    with pytest.raises(OffsiteError, match="WARILINK_AWS_LIVE=1"):
        storage_from_env({"WARILINK_BACKUP_OFFSITE": "s3://warilink-backups/prod"})
    with pytest.raises(OffsiteError):
        storage_from_env({"WARILINK_BACKUP_OFFSITE": "s3://bucket"})  # 接頭辞が無い
    if importlib.util.find_spec("boto3") is None:  # CI・開発の既定(boto3 は任意の依存グループ)
        with pytest.raises(AwsUnavailable, match="uv sync --group aws"):
            aws_client("s3", {"WARILINK_AWS_LIVE": "1"})
    journal = S3Journal(location=LOC)
    with pytest.raises(JournalUnavailable, match="WARILINK_AWS_LIVE=1"):
        journal.state()


# ---------------------------------------------------------------- 保管(put / stat / get)


def test_put_records_version_and_sha256_and_get_reads_that_version(tmp_path):
    s3 = FakeS3()
    st = S3Storage(s3, LOC)
    src = tmp_path / "a.enc"
    src.write_bytes(b"one")
    v1 = st.put("x/a.enc", src)
    assert st.stat("x/a.enc") == (hashlib.sha256(b"one").hexdigest(), v1)
    src.write_bytes(b"two")
    v2 = st.put("x/a.enc", src)
    assert v1 != v2
    assert st.get("x/a.enc", v1, tmp_path / "out1") == hashlib.sha256(b"one").hexdigest()
    assert (tmp_path / "out1").read_bytes() == b"one"  # 記録した世代を読む(最新ではない)
    assert ("put", "backups/prod/x/a.enc") in s3.calls
    with pytest.raises(OffsiteError):
        st.get("x/a.enc", None, tmp_path / "out2")  # 世代の指定は必須
    with pytest.raises(OffsiteError):
        st.put("../escape", src)
    assert st.stat("missing") is None


def test_versioning_must_be_enabled(tmp_path):
    st = S3Storage(FakeS3(versioning=False), LOC)
    (tmp_path / "a").write_bytes(b"a")
    with pytest.raises(OffsiteError, match="バージョン管理"):
        st.put("a", tmp_path / "a")


def test_tampered_or_mismatched_reads_are_rejected(tmp_path):
    s3 = FakeS3()
    st = S3Storage(s3, LOC)
    (tmp_path / "a").write_bytes(b"payload")
    vid = st.put("a", tmp_path / "a")
    s3.tamper_get.add("backups/prod/a")
    with pytest.raises(OffsiteError, match="SHA-256"):
        st.get("a", vid, tmp_path / "out")
    assert not (tmp_path / "out").exists() and not (tmp_path / "out.part").exists()
    with pytest.raises(OffsiteError, match="NoSuchVersion"):
        st.get("a", "no-such-version", tmp_path / "out")


@pytest.mark.parametrize("code, needle", [("AccessDenied", "権限"), ("SignatureDoesNotMatch", "権限"),
                                          ("ExpiredToken", "権限"), ("RequestTimeout", "時間切れ"),
                                          ("SlowDown", "失敗")])
def test_errors_are_safe_and_fail_closed(tmp_path, code, needle):
    s3 = FakeS3()
    s3.fail["put_object"] = [code]
    (tmp_path / "a").write_bytes(b"a")
    with pytest.raises(OffsiteError) as e:
        S3Storage(s3, LOC).put("a", tmp_path / "a")
    assert needle in str(e.value) and code in str(e.value)
    _no_secret(str(e.value))


def _backup_dir(tmp: Path) -> Path:
    d = tmp / "b"
    (d / "receipts").mkdir(parents=True)
    (d / "db.dump.enc").write_bytes(b"d" * 10)
    (d / "manifest.enc").write_bytes(b"m" * 5)
    (d / "receipts" / "x.enc").write_bytes(b"x")
    (d / "manifest.json").write_text("{}")
    return d


def test_upload_is_manifest_last_idempotent_and_records_versions(tmp_path):
    s3 = FakeS3()
    st = S3Storage(s3, LOC)
    d = _backup_dir(tmp_path)
    versions: dict = {}
    assert upload(st, d, "2026-11-04-abc", versions=versions) == {"uploaded": 4, "skipped": 0}
    puts = [k for op, k in s3.calls if op == "put"]
    assert puts[-1].endswith("/manifest.json")  # manifest は最後
    assert set(versions) == {"db.dump.enc", "manifest.enc", "receipts/x.enc", "manifest.json"}
    assert all(v["version_id"] and len(v["sha256"]) == 64 for v in versions.values())
    again: dict = {}
    assert upload(st, d, "2026-11-04-abc", versions=again) == {"uploaded": 0, "skipped": 4}  # 重複して置かない
    assert again == versions
    receipt = put_receipt(st, "2026-11-04-abc", versions, tmp_path)
    assert receipt["version_id"] and receipt["key"] == "2026-11-04-abc/upload-receipt.json"
    body, digest = read_receipt(st, "2026-11-04-abc", receipt["version_id"], tmp_path / "r")
    assert digest == receipt["sha256"] and body["objects"] == versions
    dest = tmp_path / "fetched"
    assert fetch(st, "2026-11-04-abc", body, dest) == []
    assert (dest / "receipts" / "x.enc").read_bytes() == b"x"
    # 後から同じキーを上書きされても、記録した世代を読む
    s3.overwrite("backups/prod/2026-11-04-abc/db.dump.enc", b"evil")
    assert fetch(st, "2026-11-04-abc", body, tmp_path / "fetched2") == []
    assert (tmp_path / "fetched2" / "db.dump.enc").read_bytes() == b"d" * 10


def test_partial_failure_retries_then_fails_without_manifest(tmp_path):
    s3 = FakeS3()
    d = _backup_dir(tmp_path)
    s3.fail["put_object"] = ["RequestTimeout"] + [None] * 10  # 1 回目だけ失敗 → 再試行で成功
    assert upload(S3Storage(s3, LOC), d, "p1", sleep=lambda s: None)["uploaded"] == 4
    s3b = FakeS3()
    s3b.fail["put_object"] = [None, "AccessDenied", "AccessDenied", "AccessDenied"]
    with pytest.raises(OffsiteError) as e:
        upload(S3Storage(s3b, LOC), d, "p2", sleep=lambda s: None)
    _no_secret(str(e.value))
    assert not any(k.endswith("manifest.json") for k in s3b.store)  # そろっていなければ manifest を置かない


def test_fetch_detects_wrong_bytes_and_incomplete_receipts(tmp_path):
    s3 = FakeS3()
    st = S3Storage(s3, LOC)
    d = _backup_dir(tmp_path)
    versions: dict = {}
    upload(st, d, "p", versions=versions)
    s3.tamper_get.add("backups/prod/p/db.dump.enc")
    assert any("db.dump.enc" in p for p in fetch(st, "p", {"format": "warilink-offsite-receipt-1", "prefix": "p",
                                                         "objects": versions}, tmp_path / "f"))
    no_manifest = {k: v for k, v in versions.items() if k != "manifest.json"}
    assert fetch(st, "p", {"format": "warilink-offsite-receipt-1", "prefix": "p", "objects": no_manifest},
                 tmp_path / "g") == ["受領記録に manifest.json がありません(不完全なアップロード)"]
    assert fetch(st, "other", {"format": "warilink-offsite-receipt-1", "prefix": "p", "objects": versions},
                 tmp_path / "h")
    evil = {**versions, "../x": versions["manifest.json"]}
    assert any("不正" in p for p in fetch(st, "p", {"format": "warilink-offsite-receipt-1", "prefix": "p",
                                                     "objects": evil}, tmp_path / "i"))


def test_bucket_preconditions_are_checked_not_applied():
    assert S3Storage(FakeS3(), LOC).check_bucket() == []
    s3 = FakeS3(versioning=False, lock=("GOVERNANCE", 7), sse="AES256")
    problems = S3Storage(s3, LOC).check_bucket()
    assert len(problems) == 3 and any("バージョン管理" in p for p in problems)
    assert not [c for c in s3.calls if c[0] == "put"]  # 設定を変えない
    assert len(S3Storage(FakeS3(lock=None), LOC).check_bucket()) == 1
    denied = FakeS3()
    for op in ("get_bucket_versioning", "get_object_lock_configuration", "get_bucket_encryption"):
        denied.fail[op] = ["AccessDenied"]
    problems = S3Storage(denied, LOC).check_bucket()
    assert len(problems) == 3
    for p in problems:
        _no_secret(p)


# ---------------------------------------------------------------- 削除ジャーナル(S3)


def _journal(s3) -> S3Journal:
    return S3Journal(location=S3Location(BUCKET, "journal/prod"), client_=s3)


def test_s3_journal_keeps_the_step01_contract():
    s3 = FakeS3()
    j = _journal(s3)
    a, b = uuid.uuid4(), uuid.uuid4()
    j.prepare(a, [("identity", uuid.UUID(int=1))])
    j.prepare(a, [("identity", uuid.UUID(int=1))])  # 冪等
    j.prepare(b, [("group", uuid.UUID(int=2))])
    j.finalize(a)
    j.abort(b)
    st = _journal(s3).state()  # 別のプロセス(キャッシュ無し)から読む。ページ分けも通る
    assert st.checkpoint.seq == 4 and list(st.finalized) == [str(a)] and st.aborted == {str(b)}
    head = json.loads(s3.store["journal/prod/HEAD"][-1]["data"])
    assert head["seq"] == 4 and head["head"] == st.records[-1]["hash"]
    keys = sorted(k for k in s3.store if "/records/" in k)
    assert keys == [f"journal/prod/records/{i:012d}.json" for i in range(1, 5)]
    puts = [k for op, k in s3.calls if op == "put" and "/records/" in k]
    assert len(puts) == 4  # 冪等な再試行は書かない


def test_concurrent_writers_never_share_a_sequence_number():
    s3 = FakeS3()
    a, b = _journal(s3), _journal(s3)
    a.prepare(uuid.uuid4(), [("identity", uuid.UUID(int=1))])
    b.state()  # b は seq 1 までを読んだ
    a.prepare(uuid.uuid4(), [("identity", uuid.UUID(int=2))])  # a が seq 2 を書く
    orig = b._state_locked
    calls = []

    def stale_once():  # 1 回目は古い状態(seq 1 まで)を返し、seq 2 に書こうとして競合させる
        calls.append(1)
        if len(calls) == 1:
            st = orig()
            st.records = st.records[:1]
            from app.ops.deletion_journal import replay  # noqa: PLC0415
            return replay(st.records, None)
        return orig()

    b._state_locked = stale_once
    b.prepare(uuid.uuid4(), [("identity", uuid.UUID(int=3))])
    assert len(calls) == 2  # 競合 → 読み直して再試行
    st = _journal(s3).state()
    assert st.checkpoint.seq == 3 and len(st.pending) == 3


@pytest.mark.parametrize("damage", ["overwrite", "overwrite_rehashed", "delete_marker", "drop_middle", "drop_tail",
                                    "head_missing", "head_rollback"])
def test_s3_journal_detects_generation_swaps_and_missing_records(damage):
    s3 = FakeS3()
    j = _journal(s3)
    for n in range(3):
        j.prepare(uuid.uuid4(), [("identity", uuid.UUID(int=n + 1))])
    k2, k3 = "journal/prod/records/000000000002.json", "journal/prod/records/000000000003.json"
    if damage in ("overwrite", "overwrite_rehashed"):
        rec = json.loads(s3.store[k2][-1]["data"])
        rec["targets"][0]["target_id"] = str(uuid.UUID(int=99))
        s3.overwrite(k2, json.dumps(rec).encode())  # 新しい世代(古い世代は残る)
    elif damage == "delete_marker":
        s3.delete_marker(k2)
    elif damage == "drop_middle":
        s3.drop(k2)
    elif damage == "drop_tail":
        s3.drop(k3)
        s3.drop("journal/prod/records/000000000002.json")
    elif damage == "head_missing":
        s3.drop("journal/prod/HEAD")
    else:
        s3.overwrite("journal/prod/HEAD", s3.store["journal/prod/HEAD"][0]["data"])  # 古い HEAD に戻す
    with pytest.raises(JournalCorrupt):
        _journal(s3).state()


def test_s3_journal_write_failures_are_unavailable_not_success():
    s3 = FakeS3()
    j = _journal(s3)
    s3.fail["put_object"] = ["AccessDenied"]
    with pytest.raises(JournalUnavailable) as e:
        j.prepare(uuid.uuid4(), [("identity", uuid.UUID(int=1))])
    _no_secret(str(e.value))
    assert _journal(s3).state().checkpoint.seq == 0
    # 記録は置けたが HEAD の更新が他の書き手と競合した: 記録は耐久化済み。HEAD の 1 つ遅れは読み取りで許す
    s3.fail["put_object"] = [None, "PreconditionFailed"]
    j.prepare(uuid.uuid4(), [("identity", uuid.UUID(int=2))])
    assert _journal(s3).state().checkpoint.seq == 1
    s3.fail["list_object_versions"] = ["RequestTimeout"]
    with pytest.raises(JournalUnavailable):
        _journal(s3).state()


def test_s3_journal_uses_the_same_record_format_as_the_local_journal(tmp_path):
    """ローカルと S3 の記録は同じ形式(STEP 01 の hash の計算)で、片方の記録をもう片方で検証できる。"""
    from app.ops.deletion_journal import LocalDirJournal, replay  # noqa: PLC0415

    s3 = FakeS3()
    _journal(s3).prepare(uuid.uuid4(), [("destination", uuid.UUID(int=5))])
    recs = [json.loads(s3.store[k][-1]["data"]) for k in sorted(s3.store) if "/records/" in k]
    assert replay(recs, None).checkpoint.seq == 1
    local = LocalDirJournal(tmp_path / "j")
    local.prepare(uuid.uuid4(), [("destination", uuid.UUID(int=5))])
    assert set(local.state().records[0]) == set(recs[0])


def test_monkeypatched_module_has_no_live_client():
    assert s3_storage.S3Journal(location=LOC).client_ is None
