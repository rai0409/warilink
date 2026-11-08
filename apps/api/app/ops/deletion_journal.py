"""削除ジャーナル(STEP 01)。退会・振込先の消去・グループの完全な削除を、DB の外に独立して記録する。

目的: 古いバックアップから DB を戻しても、利用者に「削除した」と返した操作を失わないこと。DB 内の削除済み一覧
(deletion_tombstones)は DB と一緒に古くなるため、復元のときはこのジャーナルを正とする。

記録の形式(format warilink-deletion-journal-1、1 行 1 件の JSON、UTF-8):
  {"v": 1, "seq": 1, "type": "prepare", "op_id": "<uuid>", "at": "<UTC ISO8601>",
   "targets": [{"kind": "identity", "target_id": "<uuid>"}], "prev": "<直前の hash>", "hash": "<sha256>"}
- type は prepare(削除の前に書く)/ finalize(DB の commit の後に書く)/ abort(確実にロールバックした)。
- hash は hash 以外のキーを sort_keys・区切り (",", ":")・ensure_ascii で並べた JSON の sha256。
  最初の prev は 64 個の 0。
- seq は 1 から連番。チェックポイント {seq, head} は最後の記録の seq と hash。バックアップの manifest に入れて、
  復元のときに「バックアップの時点までの記録がそろっていること」を確かめる。
- 記録するのは ID と時刻だけ(名前・口座・レシート・トークンは書かない)。時刻はすべて UTC。

保証の境界(分散トランザクションではない):
  prepare の耐久化 → DB の削除と tombstone・操作記録を 1 つのトランザクションで commit → finalize の耐久化
  → 成功の応答。
  prepare だけが残った操作(commit の直後に落ちた・finalize を書けなかった)は「未確定」とし、DB に操作記録があると
  確かめられたときだけ finalize する(reconcile)。DB に無いことを根拠に自動で abort にはしない(DB が古い状態に
  戻っている可能性があるため)。未確定が残っている間は、復元を拒否する。

保管先の実装は _AppendJournal の _locked / _state_locked / _write を満たせばよい
(ローカルディレクトリ・テスト用のメモリ・S3 = app/ops/s3_storage.py の S3Journal、STEP 02)。
ローカルの実装は外部保管ではない。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

FORMAT = "warilink-deletion-journal-1"
VERSION = 1
GENESIS = "0" * 64
KINDS = ("identity", "destination", "group")
TYPES = ("prepare", "finalize", "abort")


class JournalError(Exception):
    """ジャーナルの失敗。メッセージにパス・URL・個人情報を含めない。"""


class JournalUnavailable(JournalError):
    """書けない・読めない(再試行で直る可能性がある)。"""


class JournalConflict(JournalUnavailable):
    """同時に書いた別の書き手と番号が重なった(読み直して再試行する)。"""


class JournalCorrupt(JournalError):
    """連番・hash・HEAD が一致しない(欠落・改ざん・切り詰め)。復元に使ってはいけない。"""


class JournalConfigError(JournalError):
    """WARILINK_DELETION_JOURNAL の値が不正。"""


@dataclass(frozen=True)
class Checkpoint:
    seq: int
    head: str

    def to_json(self) -> dict:
        return {"format": FORMAT, "seq": self.seq, "head": self.head}

    @staticmethod
    def from_json(d: dict) -> Checkpoint:
        if not isinstance(d, dict) or d.get("format") != FORMAT:
            raise JournalCorrupt("チェックポイントの形式が違います")
        seq, head = d.get("seq"), d.get("head")
        if not isinstance(seq, int) or seq < 0 or not isinstance(head, str) or len(head) != 64:
            raise JournalCorrupt("チェックポイントの値が不正です")
        return Checkpoint(seq, head)


EMPTY = Checkpoint(0, GENESIS)


@dataclass
class JournalState:
    """検証済みの記録から求めた状態。"""

    records: list[dict]
    checkpoint: Checkpoint
    finalized: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # op_id → [(kind, target_id)]
    pending: dict[str, tuple[list[tuple[str, str]], str]] = field(default_factory=dict)  # op_id → (targets, at)
    aborted: set[str] = field(default_factory=set)

    def finalized_targets(self) -> list[tuple[str, str]]:
        seen: dict[tuple[str, str], None] = {}
        for targets in self.finalized.values():
            for t in targets:
                seen.setdefault(t, None)
        return list(seen)


def record_hash(rec: dict) -> str:
    body = {k: v for k, v in rec.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
                          .encode()).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _norm_targets(targets: Iterable[tuple[str, uuid.UUID | str]]) -> list[dict]:
    out = []
    for kind, tid in targets:
        if kind not in KINDS:
            raise ValueError(f"未対応の削除の種類です: {kind}")
        out.append({"kind": kind, "target_id": str(uuid.UUID(str(tid)))})
    if not out:
        raise ValueError("削除の対象がありません")
    return out


def replay(records: list[dict], head: Checkpoint | None) -> JournalState:
    """記録の連番・hash・前後関係を検証して状態を求める。HEAD は最後の記録か、その 1 つ前(HEAD を書く前に
    落ちた場合)だけを許す。それ以外は JournalCorrupt。"""
    prev, state = GENESIS, JournalState(records=records, checkpoint=EMPTY)
    hashes: list[str] = []
    for i, r in enumerate(records, start=1):
        if not isinstance(r, dict) or r.get("v") != VERSION or r.get("seq") != i or r.get("prev") != prev:
            raise JournalCorrupt(f"記録の連番が途切れています(seq {i})")
        if r.get("hash") != record_hash(r):
            raise JournalCorrupt(f"記録の内容が書き換えられています(seq {i})")
        t, op = r.get("type"), r.get("op_id")
        if t not in TYPES or not isinstance(op, str) or not isinstance(r.get("at"), str):
            raise JournalCorrupt(f"記録の形式が不正です(seq {i})")
        try:
            datetime.fromisoformat(r["at"])
        except ValueError:
            raise JournalCorrupt(f"記録の時刻が不正です(seq {i})") from None
        if t == "prepare":
            if op in state.pending or op in state.finalized or op in state.aborted:
                raise JournalCorrupt(f"同じ操作の prepare が重複しています(seq {i})")
            try:
                targets = [(x["kind"], x["target_id"]) for x in _norm_targets(
                    (x["kind"], x["target_id"]) for x in r.get("targets") or [])]
            except (KeyError, TypeError, ValueError):
                raise JournalCorrupt(f"削除の対象が不正です(seq {i})") from None
            state.pending[op] = (targets, r["at"])
        else:
            if op not in state.pending:
                raise JournalCorrupt(f"prepare の無い {t} です(seq {i})")
            targets, _ = state.pending.pop(op)
            if t == "finalize":
                state.finalized[op] = targets
            else:
                state.aborted.add(op)
        prev = r["hash"]
        hashes.append(prev)
    last = Checkpoint(len(records), prev)
    if head is not None and head != last:
        lagging = head.seq == last.seq - 1 and head.head == (hashes[-2] if len(hashes) >= 2 else GENESIS)
        if not lagging:
            raise JournalCorrupt("HEAD と記録が一致しません(記録の欠落・切り詰めのおそれ)")
    state.checkpoint = last
    return state


def verify_checkpoint(state: JournalState, cp: Checkpoint) -> None:
    """バックアップの時点のチェックポイントが、この記録の一部であることを確かめる。"""
    if cp.seq == 0:
        if cp.head != GENESIS:
            raise JournalCorrupt("チェックポイントの値が不正です")
        return
    if cp.seq > len(state.records) or state.records[cp.seq - 1]["hash"] != cp.head:
        raise JournalCorrupt("バックアップの時点の記録がジャーナルに見つかりません(別のジャーナル・欠落・改ざん)")


class DeletionJournal(Protocol):
    """削除ジャーナルの入口。実装は追記の耐久化(fsync・条件付き書込みなど)を済ませてから戻ること。"""

    name: str

    def prepare(self, op_id: uuid.UUID, targets: Iterable[tuple[str, uuid.UUID | str]]) -> None: ...
    def finalize(self, op_id: uuid.UUID) -> None: ...
    def abort(self, op_id: uuid.UUID) -> None: ...
    def state(self) -> JournalState: ...


class _AppendJournal:
    """連番・hash・重複の扱いを共通にした実装。保管先ごとに _locked / _load / _write を用意する。"""

    name = "abstract"

    CONFLICT_ATTEMPTS = 5

    def prepare(self, op_id: uuid.UUID, targets: Iterable[tuple[str, uuid.UUID | str]]) -> None:
        norm = _norm_targets(targets)
        op = str(op_id)
        for _ in range(self.CONFLICT_ATTEMPTS):
            with self._locked():
                st = self._state_locked()
                if op in st.pending or op in st.finalized:
                    return  # 同じ操作の再試行(冪等)
                if op in st.aborted:
                    raise JournalError("取り消した操作は再開できません")
                try:
                    self._append(st, {"type": "prepare", "op_id": op, "targets": norm})
                    return
                except JournalConflict:
                    continue  # 別の書き手が先に同じ番号を書いた。読み直す
        raise JournalUnavailable("削除ジャーナルへの書き込みが競合し続けています")

    def finalize(self, op_id: uuid.UUID) -> None:
        self._close(op_id, "finalize")

    def abort(self, op_id: uuid.UUID) -> None:
        self._close(op_id, "abort")

    def _close(self, op_id: uuid.UUID, kind: str) -> None:
        op = str(op_id)
        for _ in range(self.CONFLICT_ATTEMPTS):
            with self._locked():
                st = self._state_locked()
                done = st.finalized if kind == "finalize" else st.aborted
                if op in done:
                    return  # 冪等
                if op not in st.pending:
                    raise JournalError(f"{kind} できる未確定の操作がありません")
                try:
                    self._append(st, {"type": kind, "op_id": op})
                    return
                except JournalConflict:
                    continue
        raise JournalUnavailable("削除ジャーナルへの書き込みが競合し続けています")

    def state(self) -> JournalState:
        with self._locked():
            return self._state_locked()

    def _append(self, st: JournalState, body: dict) -> None:
        rec = {"v": VERSION, "seq": st.checkpoint.seq + 1, "at": _utc_now(), "prev": st.checkpoint.head, **body}
        rec["hash"] = record_hash(rec)
        self._write(rec)

    # 保管先ごとの実装
    @contextmanager
    def _locked(self) -> Iterator[None]:  # pragma: no cover - 抽象
        raise NotImplementedError
        yield

    def _state_locked(self) -> JournalState:  # pragma: no cover - 抽象
        raise NotImplementedError

    def _write(self, rec: dict) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError


class MemoryJournal(_AppendJournal):
    """テスト用(プロセスが終われば消える。本番では使えない)。"""

    name = "memory"

    def __init__(self) -> None:
        self.records: list[dict] = []
        self.head: Checkpoint | None = None

    @contextmanager
    def _locked(self) -> Iterator[None]:
        yield

    def _state_locked(self) -> JournalState:
        return replay(list(self.records), self.head)

    def _write(self, rec: dict) -> None:
        self.records.append(rec)
        self.head = Checkpoint(rec["seq"], rec["hash"])


class LocalDirJournal(_AppendJournal):
    """ローカルディレクトリ(journal.jsonl と HEAD)。追記と HEAD の更新ごとに fsync する。

    同じホストの複数プロセスからの追記は、ディレクトリ内の lock ファイル(flock)で直列にする。
    最後の行が改行で終わっていない(書込みの途中で落ちた)場合、HEAD がその前の記録を指していれば、
    次の追記の前に切り捨てる。DB と同じディスクに置くと独立した保管にならない(導入先で別の保管先にする)。
    """

    name = "local"
    FILE, HEAD, LOCK = "journal.jsonl", "HEAD", ".lock"

    def __init__(self, root: Path) -> None:
        if not root.is_absolute():
            raise JournalConfigError("削除ジャーナルの置き場所は絶対パスにしてください")
        self.root = root

    @contextmanager
    def _locked(self) -> Iterator[None]:
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.root / self.LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            raise JournalUnavailable("削除ジャーナルを開けません") from None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _read(self) -> tuple[list[dict], Checkpoint | None, int | None]:
        """(記録, HEAD, 切り捨てる位置)。読めなければ JournalUnavailable、形式が壊れていれば JournalCorrupt。"""
        try:
            data = (self.root / self.FILE).read_bytes() if (self.root / self.FILE).exists() else b""
            head_raw = (self.root / self.HEAD).read_text() if (self.root / self.HEAD).exists() else None
        except OSError:
            raise JournalUnavailable("削除ジャーナルを読めません") from None
        torn = None
        if data and not data.endswith(b"\n"):
            torn = data.rfind(b"\n") + 1
            data = data[:torn]
        try:
            records = [json.loads(ln) for ln in data.decode("utf-8").splitlines()]
            head = Checkpoint.from_json(json.loads(head_raw)) if head_raw is not None else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise JournalCorrupt("削除ジャーナルの形式が壊れています") from None
        if head is None and records:
            raise JournalCorrupt("削除ジャーナルの HEAD がありません")
        return records, head, torn

    def _state_locked(self) -> JournalState:
        records, head, torn = self._read()
        st = replay(records, head)
        if torn is not None and head != st.checkpoint:
            raise JournalCorrupt("削除ジャーナルの最後の記録が途中で切れています")
        return st

    def _write(self, rec: dict) -> None:
        line = (json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
        path, head = self.root / self.FILE, self.root / self.HEAD
        try:
            _, _, torn = self._read()
            fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
            try:
                if torn is not None:
                    os.ftruncate(fd, torn)
                os.lseek(fd, 0, os.SEEK_END)
                os.write(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)
            tmp = self.root / (self.HEAD + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, json.dumps(Checkpoint(rec["seq"], rec["hash"]).to_json()).encode())
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, head)
            dfd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            raise JournalUnavailable("削除ジャーナルに書き込めません") from None


def journal_from_url(value: str | None) -> DeletionJournal | None:
    """WARILINK_DELETION_JOURNAL の値から作る。未設定は None(開発・テストだけで許す)。"""
    if not value:
        return None
    scheme, _, rest = value.partition(":")
    if scheme == "local" and rest:
        return LocalDirJournal(Path(rest))
    if scheme == "s3":
        from app.ops.offsite import OffsiteError  # noqa: PLC0415
        from app.ops.s3_storage import S3Journal, parse_s3_url  # noqa: PLC0415

        try:
            return S3Journal(location=parse_s3_url(value))  # 接続は最初に使うとき(WARILINK_AWS_LIVE=1 が必要)
        except OffsiteError as e:
            raise JournalConfigError(str(e)) from None
    raise JournalConfigError("WARILINK_DELETION_JOURNAL は local:/絶対パス か s3://バケット/接頭辞 にしてください")


def journal_problems(value: str | None) -> list[str]:
    """本番の設定検査用。値(パス)はメッセージに含めない。"""
    if not value:
        return ["WARILINK_DELETION_JOURNAL(DB の外の削除ジャーナル)を設定してください"]
    try:
        journal_from_url(value)
    except JournalConfigError as e:
        return [str(e)]
    return []
