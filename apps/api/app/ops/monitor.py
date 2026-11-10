"""監視(PR C)。1 回分の確認をして、異常があれば WARILINK_ALERT_CMD に短い要約を渡す。定期実行は導入先で登録する。

  python -m app.ops.monitor [--json]

確認すること:
- API の /health(WARILINK_MONITOR_API_URL を設定したときだけ)と DB への接続
- 定期ジョブの最後の成功(maintenance_runs): 保持期限の削除・バックアップ・削除済み一覧の書き出し
- OCR を有効にしている場合は、ワーカーの heartbeat
- 削除ジャーナル(WARILINK_DELETION_JOURNAL を設定したとき、STEP 04): 記録を読めて壊れていないこと、確定していない削除が
  WARILINK_MONITOR_JOURNAL_PENDING_HOURS(既定 1)を超えて残っていないこと。ジャーナルを使う構成では、旧方式の
  削除済み一覧の書き出し(tombstone_export)は WARILINK_TOMBSTONE_FILE を設定したときだけ必須にする
- 本番(WARILINK_ENVIRONMENT=production、STEP 04): 外形監視の URL・通知コマンド・外部保管・削除ジャーナルが未設定なら
  「未構成」の異常にする(測れないものを正常として扱わない)

通知:
- 要約に入れるのは確認の名前と状態だけ(URL・口座・レシート・トークン・鍵・個人情報は入れない)。
- 同じ内容の通知は WARILINK_MONITOR_DEDUP_HOURS(既定 6)の間は送らない。異常が直ったら 1 回だけ「回復」を送る。
- 通知先が未設定なら標準エラーに出すだけ(ALERT_DELIVERY=BLOCKED)。通知コマンドが失敗したら終了コード 2。
終了コード: 0 = 正常、1 = 異常あり、2 = 通知の送信に失敗。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_engine
from app.core.security import now
from app.models.tables import DeletionJournalOp, MaintenanceRun
from app.ops.deletion_journal import DeletionJournal, JournalCorrupt, JournalError

# ジョブ名 → 最後の成功からの許容時間(1 日 1 回のジョブに少し余裕を持たせる)
JOBS = {"retention_purge": 26, "backup": 26, "tombstone_export": 26}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str  # 状態だけ(例: "stale 49h", "never", "unreachable")。個人情報・URL は入れない


def check_jobs(db: Session, at: datetime, limits: dict[str, int]) -> list[Check]:
    rows = {r.job: r for r in db.query(MaintenanceRun).all()}
    out = []
    for job, hours in limits.items():
        r = rows.get(job)
        if r is None or r.last_success_at is None:
            out.append(Check(f"job:{job}", False, "never"))
            continue
        age = (at - r.last_success_at) / timedelta(hours=1)
        failed_after = r.last_failure_at is not None and r.last_failure_at >= r.last_success_at
        if age < 0:
            out.append(Check(f"job:{job}", False, "future"))
        elif age > hours:
            out.append(Check(f"job:{job}", False, f"stale {age:.0f}h"))
        elif failed_after:
            out.append(Check(f"job:{job}", False, "last_run_failed"))
        else:
            out.append(Check(f"job:{job}", True, f"ok {age:.0f}h"))
    return out


def check_journal(db: Session, journal: DeletionJournal, at: datetime, hours: float) -> Check:
    """DB の外の削除ジャーナルの健全性と、確定していない削除の件数(ID・パスは出さない)。"""
    try:
        st = journal.state()
    except JournalCorrupt:
        return Check("deletion_journal", False, "corrupt")
    except JournalError:
        return Check("deletion_journal", False, "unreadable")
    cut = at - timedelta(hours=hours)
    pending = sum(1 for _, t in st.pending.values() if datetime.fromisoformat(t) <= cut)
    if pending:
        return Check("deletion_journal", False, f"pending {pending}")
    unconfirmed = db.query(DeletionJournalOp).filter(DeletionJournalOp.finalized_at.is_(None),
                                                     DeletionJournalOp.created_at <= cut).count()
    if unconfirmed:
        return Check("deletion_journal", False, f"unconfirmed {unconfirmed}")
    return Check("deletion_journal", True, f"ok seq {st.checkpoint.seq}")


def check_config(e: dict[str, str] | os._Environ[str], journal_setting: str | None) -> list[Check]:
    """本番で必要な監視・通知・保管の設定(値は出さない)。未設定は「未構成」の異常。"""
    values = {"config:alert_cmd": e.get("WARILINK_ALERT_CMD"), "config:api_url": e.get("WARILINK_MONITOR_API_URL"),
              "config:offsite": e.get("WARILINK_BACKUP_OFFSITE"), "config:deletion_journal": journal_setting}
    return [Check(name, bool(v), "ok" if v else "not_configured") for name, v in values.items()]


def required_jobs(e: dict[str, str] | os._Environ[str], journal_configured: bool) -> dict[str, int]:
    """監視するジョブ。削除ジャーナルを使い、旧方式の一覧ファイルを置かない構成では tombstone_export を外す。"""
    jobs = dict(JOBS)
    if journal_configured and not e.get("WARILINK_TOMBSTONE_FILE"):
        jobs.pop("tombstone_export")
    return jobs


def run_checks(*, at: datetime | None = None, env: dict[str, str] | None = None,
               http_get: Callable[[str], int] | None = None) -> list[Check]:
    e = os.environ if env is None else env
    t = at or now()
    checks: list[Check] = []
    settings = get_settings()
    production = settings.environment == "production"
    if production:
        checks += check_config(e, settings.deletion_journal)
    url = e.get("WARILINK_MONITOR_API_URL", "")
    if not url and production:
        checks.append(Check("api:health", False, "not_configured"))  # 測れないものを正常にしない
    if url:
        get = http_get or (lambda u: httpx.get(u, timeout=5).status_code)
        try:
            status = get(url.rstrip("/") + "/api/v1/health")
            checks.append(Check("api:health", status == 200, f"http {status}"))
        except Exception:  # noqa: BLE001 - 接続できない理由(例外の文字列)は URL を含みうるので出さない
            checks.append(Check("api:health", False, "unreachable"))
    try:
        with Session(get_engine()) as db:
            db.execute(text("SELECT 1"))
            checks.append(Check("db", True, "ok"))
            from app.modules.groups.lifecycle import current_journal  # noqa: PLC0415

            try:
                journal = current_journal()
            except JournalError:
                checks.append(Check("deletion_journal", False, "misconfigured"))
                journal = None
            jobs = required_jobs(e, journal is not None)
            limits = {j: int(e.get(f"WARILINK_MONITOR_MAX_AGE_{j.upper()}_HOURS", h)) for j, h in jobs.items()}
            checks += check_jobs(db, t, limits)
            if journal is not None:
                checks.append(check_journal(db, journal, t,
                                            float(e.get("WARILINK_MONITOR_JOURNAL_PENDING_HOURS", "1"))))
            if get_settings().feature_ocr:
                from app.ocr.jobs import worker_available  # noqa: PLC0415

                alive = worker_available(db)
                checks.append(Check("ocr:worker", alive, "ok" if alive else "no_heartbeat"))
    except Exception:  # noqa: BLE001 - 接続文字列を含みうるため例外の文字列は出さない
        checks.append(Check("db", False, "unreachable"))
    return checks


def summarize(checks: list[Check]) -> str:
    bad = sorted(f"{c.name}={c.detail}" for c in checks if not c.ok)
    return "warilink monitor: " + (", ".join(bad) if bad else "recovered")


def deliver(message: str, problems: bool, *, state_path: Path, dedup_hours: float, at: datetime,
            command: str | None, run: Callable[[str, str], int] | None = None) -> str:
    """通知を送るか判断して送る。戻り値: sent / suppressed / not_needed / not_configured / failed。"""
    state: dict[str, Any] = {}
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text())
        except ValueError:
            state = {}
    key = hashlib.sha256(message.encode()).hexdigest()
    last_key, last_at = state.get("key"), state.get("sent_at")
    alerted = bool(state.get("alerting"))
    if not problems and not alerted:
        return "not_needed"
    if problems and last_key == key and last_at and at - datetime.fromisoformat(last_at) < timedelta(hours=dedup_hours):
        return "suppressed"
    if not command:
        print(message, file=sys.stderr)
        result = "not_configured"
    else:
        send = run or (lambda cmd, msg: subprocess.run(["sh", "-c", cmd], input=msg, text=True, timeout=30,
                                                         check=False).returncode)
        try:
            result = "sent" if send(command, message) == 0 else "failed"
        except (OSError, subprocess.TimeoutExpired):
            result = "failed"
    if result != "failed":
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"key": key, "sent_at": at.isoformat(), "alerting": problems}))
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.ops.monitor")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    at = now()
    checks = run_checks(at=at)
    problems = any(not c.ok for c in checks)
    message = summarize(checks)
    delivery = deliver(message, problems,
                       state_path=Path(os.environ.get("WARILINK_MONITOR_STATE", "var/monitor-state.json")),
                       dedup_hours=float(os.environ.get("WARILINK_MONITOR_DEDUP_HOURS", "6")), at=at,
                       command=os.environ.get("WARILINK_ALERT_CMD") or None)
    report = {"ok": not problems, "checks": [c.__dict__ for c in checks], "alert": delivery}
    print(json.dumps(report, ensure_ascii=False) if a.json else message)
    if delivery == "failed":
        return 2
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
