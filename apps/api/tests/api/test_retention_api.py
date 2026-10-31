"""PR B: 保存期間の方針(設定値)と、削除ジョブの実績(記録)を分けて返す。"""

import argparse
import re
from datetime import timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_engine
from app.core.security import now
from app.modules.identity.retention import RETENTION_JOB, record_run
from app.modules.meta.router import AUTOMATIC_DELETION_MAX_AGE_HOURS, BACKUP_RETENTION_DAYS
from app.ocr.cli import cmd_purge

from .conftest import API, User

ROOT = Path(__file__).resolve().parents[4]


def _get(u: User) -> dict:
    r = u.c.get(f"{API}/retention")
    assert r.status_code == 200, r.text
    return r.json()


def test_policies_come_from_settings_and_backup_script(user_factory):
    s = get_settings()
    body = _get(user_factory())
    days = {p["key"]: p["days"] for p in body["policies"]}
    assert days == {
        "receipt_images": s.receipt_image_retention_days, "audit_logs": s.audit_log_retention_days,
        "expired_sessions": s.expired_session_retention_days, "expired_invites": s.expired_invite_retention_days,
        "orphaned_groups": s.orphaned_group_retention_days, "closed_groups": s.closed_group_retention_days,
        "backups": BACKUP_RETENTION_DAYS, "deletion_tombstones": s.tombstone_retention_days,
    }
    # バックアップの日数は運用スクリプトと同じ(画面と実際の prune がずれないように)
    script = (ROOT / "scripts/warilink_backup.py").read_text()
    assert re.search(rf"^BACKUP_RETENTION_DAYS = {BACKUP_RETENTION_DAYS}\b", script, re.M)
    # 削除済み一覧はバックアップより短くしない(古いバックアップから戻したときに削除を再適用できるように)
    assert days["deletion_tombstones"] >= days["backups"]
    assert body["backup_encrypted_offsite"] is False


def test_automatic_deletion_is_unverified_until_the_job_actually_succeeds(user_factory):
    u = user_factory()
    ad = _get(u)["automatic_deletion"]
    assert ad == {"status": "unverified", "last_success_at": None, "last_failure_at": None,
                  "max_age_hours": AUTOMATIC_DELETION_MAX_AGE_HOURS}
    with Session(get_engine()) as db:
        record_run(db, RETENTION_JOB, ok=False)
    assert _get(u)["automatic_deletion"]["status"] == "unverified"  # 失敗だけでは「動いている」としない
    with Session(get_engine()) as db:
        record_run(db, RETENTION_JOB, ok=True, at=now() - timedelta(hours=AUTOMATIC_DELETION_MAX_AGE_HOURS + 1))
    assert _get(u)["automatic_deletion"]["status"] == "unverified"  # 古い成功は根拠にしない
    # 実際の削除ジョブ(purge-images)を 1 回動かすと、成功が記録される
    assert cmd_purge(argparse.Namespace(dry_run=False, json=True)) == 0
    ad = _get(u)["automatic_deletion"]
    assert ad["status"] == "verified" and ad["last_success_at"] is not None and ad["last_failure_at"] is not None


def test_dry_run_does_not_count_as_a_run(user_factory):
    assert cmd_purge(argparse.Namespace(dry_run=True, json=True)) == 0
    assert _get(user_factory())["automatic_deletion"]["last_success_at"] is None


def test_plans_response_is_unchanged(user_factory):
    """既存 API のキーを変えない(後方互換)。"""
    body = user_factory().c.get(f"{API}/plans").json()
    assert set(body) == {"passkeys_available", "payment_destinations_available", "ocr_available", "ads_available",
                         "billing_available", "ocr_free_monthly_scans", "ocr_reward_scans_per_ad",
                         "ocr_monthly_scan_cap", "plans"}
