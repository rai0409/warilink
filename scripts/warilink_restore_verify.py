#!/usr/bin/env python3
"""隔離した復元の検証(STEP 04)。既定は計画の表示だけで、--execute を付けたときだけ実行する。

  warilink_restore_verify.py --source s3://バケット/接頭辞 --from-report /var/lib/warilink/last-backup.json \\
      --container warilink-drill-db --port 55433 --target-db warilink_restore_verify_YYYYMMDD \\
      --workdir 空のディレクトリ --receipt-out restore-receipt.json [--execute] [--keep]

実行の流れ(結果は 1 つの記録 = receipt にまとめる。値・鍵・中身は書かない):
  1. 削除ジャーナルの完全性(STEP 01): 連番・hash・HEAD、確定していない削除が無いこと
  2. 外部保管からの取得と検証(STEP 02): 受領記録の世代・SHA-256・暗号の認証・チェックポイント
  3. 隔離した DB への復元と削除の再適用(warilink_backup.py restore)。削除済み一覧はジャーナルの確定済みの記録から作る
  4. 復元の検査(件数・画像・外部キー・削除が保たれていること)
  5. 片付け: 復元先の DB(warilink_restore_* だけ)と作業ディレクトリ(復号した画像を含む)を消す(--keep で残す)

拒否する条件(DB に触れる前): 復元先が warilink_restore_* でない・既にある、コンテナが隔離用(warilink-drill- /
warilink-restore- で始まる)でない、作業ディレクトリが空でない、削除ジャーナル・鍵が無い、確定していない削除がある、
S3 で受領記録の世代が無い。
終了コード: 0 = PASS(計画の確認なら計画が有効)/ 1 = FAIL / 3 = 拒否。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import warilink_backup as wb  # noqa: E402

from app.ops.deletion_journal import JournalError  # noqa: E402
from app.ops.offsite import LocalDirStorage, OffsiteError, storage_from_env  # noqa: E402

ISOLATED_PREFIXES = ("warilink-drill-", "warilink-restore-")
FORBIDDEN_CONTAINERS = ("warilink-db-1", "hakobu-postgres")


class Refused(Exception):
    pass


def _commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=wb.ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def resolve_backup(a: argparse.Namespace) -> dict:
    """取得するバックアップ(接頭辞・受領記録の世代・SHA-256)。--from-report はバックアップの出力(JSON)。"""
    if a.from_report:
        try:
            report = json.loads(Path(a.from_report).read_text(encoding="utf-8").strip().splitlines()[-1])
            off = report["offsite"]
            return {"prefix": off["prefix"], "version": off["receipt"]["version_id"],
                    "sha256": off["receipt"]["sha256"]}
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            raise Refused("--from-report のバックアップの出力を読めません(外部保管の受領記録が無い)") from None
    if not a.backup:
        raise Refused("--backup(と --receipt-version)か --from-report を指定してください")
    return {"prefix": a.backup, "version": a.receipt_version, "sha256": a.receipt_sha256}


def guards(a: argparse.Namespace, *, check_db: bool) -> tuple[object, dict, object]:
    """DB に触れる前の検査。戻り値は (保管先, 取得するバックアップ, 削除ジャーナル)。"""
    if a.container in FORBIDDEN_CONTAINERS or not a.container.startswith(ISOLATED_PREFIXES):
        raise Refused("復元は隔離した一時コンテナ(warilink-drill- / warilink-restore- で始まる名前)でだけ行います")
    try:
        wb.guard_target(a, a.target_db)
    except wb.Refused as e:
        raise Refused(str(e)) from None
    work = Path(a.workdir)
    if work.exists() and any(work.iterdir()):
        raise Refused("作業ディレクトリが空ではありません")
    source = a.source or os.environ.get("WARILINK_BACKUP_OFFSITE", "")
    try:
        storage = storage_from_env({"WARILINK_BACKUP_OFFSITE": source, **{
            k: v for k, v in os.environ.items() if k == "WARILINK_AWS_LIVE"}})
    except OffsiteError as e:
        raise Refused(str(e)) from None
    if storage is None:
        raise Refused("--source(外部保管の場所)を指定してください")
    backup = resolve_backup(a)
    if not isinstance(storage, LocalDirStorage) and not backup["version"]:
        raise Refused("S3 から取得するときは受領記録の世代が必要です")
    try:
        journal = wb.journal_for(a)
    except wb.Refused as e:
        raise Refused(str(e)) from None
    if journal is None:
        raise Refused("削除ジャーナル(--journal か WARILINK_DELETION_JOURNAL)が必要です")
    if getattr(a, "provider", None) is None and not (os.environ.get("WARILINK_BACKUP_KMS_KEY_ID")
                                                      or os.environ.get("WARILINK_BACKUP_KEYS")):
        raise Refused("バックアップの鍵(WARILINK_BACKUP_KMS_KEY_ID か WARILINK_BACKUP_KEYS)がありません")
    if check_db:
        try:
            if wb.db_exists(a, a.target_db):
                raise Refused("復元先の DB が既にあります")
        except (subprocess.CalledProcessError, OSError):
            raise Refused("隔離用のコンテナに接続できません") from None
    return storage, backup, journal


def tombstones_from_journal(state) -> list[dict]:
    """確定済みの削除を、restore の削除済み一覧の形にする(created_at は確定を記録した時刻)。"""
    finalized_at = {r["op_id"]: r["at"] for r in state.records if r["type"] == "finalize"}
    return [{"kind": k, "target_id": t, "op_id": op, "created_at": finalized_at[op]}
            for op, targets in state.finalized.items() for k, t in targets]


def execute(a: argparse.Namespace, storage, backup: dict, journal) -> dict:
    steps: list[dict] = []
    work = Path(a.workdir)
    work.mkdir(parents=True, exist_ok=True)
    os.chmod(work, 0o700)
    result = "FAIL"
    created_db = False

    def step(name: str, fn):
        t0 = time.monotonic()
        try:
            status, detail = fn()
        except (JournalError, OffsiteError, wb.Refused, Refused) as e:
            status, detail = "FAIL", str(e)[:300]
        steps.append({"name": name, "status": status, "seconds": round(time.monotonic() - t0, 2), "detail": detail})
        return status == "PASS"

    state_box: dict = {}

    def journal_step():
        st = journal.state()
        if st.pending:
            return "FAIL", f"確定していない削除が {len(st.pending)} 件あります"
        state_box["st"] = st
        return "PASS", f"seq {st.checkpoint.seq}, finalized {len(st.finalized)}"

    def fetch_step():
        problems, summary = wb.verify_offsite_copy(a, storage, backup["prefix"], backup["version"], work / "fetched",
                                                   expected_receipt_sha256=backup["sha256"])
        if problems:
            return "FAIL", " / ".join(problems[:5])
        return "PASS", f"files {summary.get('files')}"

    restore_out: dict = {}

    def restore_step():
        nonlocal created_db
        tomb = work / "tombstones.jsonl"
        tomb.write_text("".join(json.dumps(r) + "\n" for r in tombstones_from_journal(state_box["st"])))
        ns = argparse.Namespace(**{**vars(a), "from": str(work / "fetched"), "receipts_target": str(work / "receipts"),
                                   "tombstones": str(tomb), "accept_missing_images": False})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            created_db = True
            rc = wb.cmd_restore(ns)
        lines = [ln for ln in buf.getvalue().splitlines() if ln.startswith("{")]
        restore_out.update(json.loads(lines[-1]) if lines else {})
        return ("PASS" if rc == 0 else "FAIL"), f"rc {rc}"

    def verify_step():
        checks = restore_out.get("checks", {})
        bad = sorted(k for k, v in checks.items() if not v)
        ok = bool(checks) and not bad and restore_out.get("journal", {}).get("status") == "verified"
        return ("PASS" if ok else "FAIL"), ("all checks true, journal verified" if ok else f"failed: {bad}")

    t_start = time.monotonic()
    ok = step("journal_integrity", journal_step) and step("fetch_and_verify", fetch_step) \
        and step("restore_and_reapply", restore_step) and step("verify_restored", verify_step)
    rto = round(time.monotonic() - t_start, 2)
    result = "PASS" if ok else "FAIL"
    # 片付け: 隔離した復元先と作業ディレクトリ(復号した画像を含む)だけを消す
    cleanup = []
    if not a.keep:
        if created_db:
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    wb.cmd_drop(argparse.Namespace(**{**vars(a)}))
                cleanup.append("dropped target db")
            except (wb.Refused, subprocess.CalledProcessError, OSError):
                cleanup.append("drop failed")
                result = "FAIL"
        shutil.rmtree(work, ignore_errors=True)
        cleanup.append("removed workdir")
    return {"result": result, "steps": steps, "measured_restore_seconds": rto if ok else None,
            "restored": {k: restore_out.get(k) for k in ("rows", "images", "reapplied") if k in restore_out},
            "cleanup": cleanup}


def main(argv: list[str] | None = None, *, storage=None, provider=None) -> int:
    p = argparse.ArgumentParser(prog="warilink_restore_verify.py")
    p.add_argument("--source", help="local:ディレクトリ か s3://バケット/接頭辞(既定は WARILINK_BACKUP_OFFSITE)")
    p.add_argument("--from-report", help="バックアップの出力(JSON)。接頭辞・受領記録の世代・SHA-256 を読む")
    p.add_argument("--backup", help="保管先でのバックアップの接頭辞")
    p.add_argument("--receipt-version")
    p.add_argument("--receipt-sha256")
    p.add_argument("--container", required=True, help="隔離した一時コンテナ(warilink-drill-* / warilink-restore-*)")
    p.add_argument("--port", default="55433")
    p.add_argument("--user", default=os.environ.get("POSTGRES_USER", "warilink"))
    p.add_argument("--target-db", required=True, help="warilink_restore_* の新しい DB")
    p.add_argument("--workdir", required=True, help="空の作業ディレクトリ(終わったら消す)")
    p.add_argument("--journal", help="削除ジャーナル(既定は WARILINK_DELETION_JOURNAL)")
    p.add_argument("--receipt-out", help="結果の記録(JSON)の書き出し先")
    p.add_argument("--execute", action="store_true", help="実行する(付けなければ計画の表示だけ)")
    p.add_argument("--keep", action="store_true", help="復元先の DB と作業ディレクトリを残す(調査用)")
    a = p.parse_args(argv)
    a.provider = provider
    receipt = {"tool": "warilink_restore_verify", "commit": _commit(), "mode": "execute" if a.execute else "plan",
               "started_at": datetime.now(UTC).isoformat(), "container": a.container, "target_db": a.target_db}
    try:
        st, backup, journal = guards(a, check_db=a.execute)
        if storage is not None:
            st = storage
        receipt["backup"] = {"prefix": backup["prefix"], "receipt_version_pinned": bool(backup["version"])}
        if not a.execute:
            receipt.update({"result": "PLANNED", "plan": ["journal_integrity", "fetch_and_verify",
                                                          "restore_and_reapply", "verify_restored", "cleanup"]})
            rc = 0
        else:
            receipt.update(execute(a, st, backup, journal))
            rc = 0 if receipt["result"] == "PASS" else 1
    except Refused as e:
        receipt.update({"result": "REFUSED", "reason": str(e)})
        rc = 3
    receipt["finished_at"] = datetime.now(UTC).isoformat()
    text = json.dumps(receipt, ensure_ascii=False, indent=1)
    if a.receipt_out:
        Path(a.receipt_out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return rc


if __name__ == "__main__":
    sys.exit(main())
