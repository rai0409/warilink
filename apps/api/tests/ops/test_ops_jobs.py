"""STEP 04: 運用ジョブ(systemd のユニット・scripts/warilink_purge_job.sh)。インストール・実行はしない。

ユニットは静的に確かめる(構文は Linux の一時コンテナで systemd-analyze verify も実行し、記録に残す)。
削除ジョブは Python と docker を偽物に差し替えて、終了コード・削除ジャーナル・二重起動の防止・通知を確かめる。
"""

import configparser
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
UNITS = ROOT / "infra" / "systemd"
PURGE = ROOT / "scripts" / "warilink_purge_job.sh"


def _unit(name: str) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(strict=False, interpolation=None, delimiters=("=",))
    cp.optionxform = str  # type: ignore[assignment,method-assign]
    cp.read_string(UNITS.joinpath(name).read_text())
    return cp


SERVICES = ("warilink-purge", "warilink-backup", "warilink-monitor", "warilink-restore-verify")


def test_every_service_has_a_timer_and_nothing_is_installed_here():
    names = sorted(p.name for p in UNITS.iterdir())
    assert names == sorted([f"{s}.service" for s in SERVICES] + [f"{s}.timer" for s in SERVICES])
    for s in SERVICES:
        assert _unit(f"{s}.timer")["Timer"]["Unit"] == f"{s}.service"
        assert _unit(f"{s}.timer")["Install"]["WantedBy"] == "timers.target"
    # このリポジトリのどこからも systemctl を実行しない(導入は運用者)
    for p in list((ROOT / "scripts").iterdir()) + [ROOT / "Makefile"]:
        if p.is_file():
            assert not re.search(r"^[^#]*systemctl (enable|start|restart)", p.read_text(errors="ignore"), re.M), p


@pytest.mark.parametrize("name", SERVICES)
def test_services_are_bounded_unprivileged_and_hold_no_secrets(name):
    u = _unit(f"{name}.service")
    svc = u["Service"]
    assert svc["Type"] == "oneshot" and svc["User"] == "warilink" and svc["WorkingDirectory"] == "/srv/warilink"
    assert svc["TimeoutStartSec"].endswith(("min", "h"))  # 時間の上限
    assert svc["NoNewPrivileges"] == "yes" and svc["CapabilityBoundingSet"] == "" and svc["UMask"] == "0077"
    assert svc.get("Restart", "no") in ("no", "on-failure")  # always にしない
    if svc.get("Restart") == "on-failure":
        assert int(u["Unit"]["StartLimitBurst"]) <= 3  # 再試行は有限
    assert all(v.startswith("/etc/warilink/") for k, v in svc.items() if k == "EnvironmentFile")
    text = UNITS.joinpath(f"{name}.service").read_text()
    assert not re.search(r"(PASSWORD|SECRET|TOKEN)=[^$\"\s]", text)  # 秘密は env ファイルから読むだけ
    assert "docker stop" not in text and "down -v" not in text and "warilink-prod-db-1" not in text.replace(
        "--container warilink-prod-db-1", "")


def test_schedules_match_the_runbook():
    assert _unit("warilink-purge.timer")["Timer"]["OnCalendar"] == "*-*-* 03:30:00 Asia/Tokyo"
    assert _unit("warilink-backup.timer")["Timer"]["OnCalendar"] == "*-*-* 04:00:00 Asia/Tokyo"
    assert _unit("warilink-monitor.timer")["Timer"]["OnUnitActiveSec"] == "15min"
    assert _unit("warilink-restore-verify.timer")["Timer"]["OnCalendar"] == "*-*-01 05:00:00 Asia/Tokyo"
    for t in ("warilink-purge", "warilink-backup", "warilink-restore-verify"):
        assert _unit(f"{t}.timer")["Timer"]["Persistent"] == "true"


def test_backup_and_restore_units_keep_the_step01_to_03_contracts():
    backup = UNITS.joinpath("warilink-backup.service").read_text()
    assert "--user warilink_backup --record-user warilink_app" in backup  # ダンプは読み取り専用、記録は app
    assert "> /var/lib/warilink/last-backup.tmp && mv" in backup  # 失敗の出力で受領記録を上書きしない
    assert "Environment=WARILINK_ENVIRONMENT=production" in backup
    restore = UNITS.joinpath("warilink-restore-verify.service").read_text()
    assert "--execute" in restore and "--container warilink-drill-db" in restore
    assert "--target-db warilink_restore_" in restore and "--from-report /var/lib/warilink/last-backup.json" in restore
    assert restore.count("docker rm -f warilink-drill-db") == 2  # 自分で作った一時コンテナだけを片付ける
    monitor = UNITS.joinpath("warilink-monitor.service").read_text()
    assert "exit 2" in monitor and "outbox" in monitor  # 通知の失敗は終了コード 2(既存の契約)
    cron = (ROOT / "infra/ops/warilink-purge.cron.example").read_text()
    assert "timer と同時に使わない" in cron


# ---------------------------------------------------------------- 削除ジョブ(偽の Python・docker)

STUB = """#!/bin/sh
echo "$@" >> "$STUB_LOG"
case "$*" in
  *purge-images*) echo '{"deleted": 0}'; exit ${STUB_PURGE_RC:-0} ;;
  *check-storage*) echo 'ok'; exit ${STUB_CHECK_RC:-0} ;;
  *journal-reconcile*) echo '{"reconciled": {"finalized": 0, "pending": 0}}'; exit ${STUB_JOURNAL_RC:-0} ;;
  *export-tombstones*) echo '{"appended": 0}'; exit 0 ;;
esac
exit 0
"""


@pytest.fixture
def job(tmp_path):
    stub = tmp_path / "python"
    stub.write_text(STUB)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    docker = tmp_path / "bin" / "docker"
    docker.parent.mkdir()
    docker.write_text(STUB)
    docker.chmod(0o755)
    log = tmp_path / "calls.log"
    alerts = tmp_path / "alerts.txt"
    base = {"PATH": f"{docker.parent}:{os.environ['PATH']}", "STUB_LOG": str(log), "WARILINK_PYTHON": str(stub),
            "WARILINK_LOCK_DIR": str(tmp_path), "WARILINK_ALERT_CMD": f"cat >> {alerts}", "HOME": str(tmp_path)}

    def run(**env):
        e = {**base, **{k: str(v) for k, v in env.items() if v is not None}}
        r = subprocess.run(["bash", str(PURGE)], env=e, capture_output=True, text=True, timeout=60, check=False)
        calls = log.read_text().splitlines() if log.exists() else []
        log.unlink(missing_ok=True)
        sent = alerts.read_text() if alerts.exists() else ""
        alerts.unlink(missing_ok=True)
        return r, calls, sent
    return run


def test_without_journal_the_tombstone_file_is_still_required(job, tmp_path):
    r, calls, sent = job()
    assert r.returncode == 1 and "tombstones_rc=1" in sent
    r, calls, sent = job(WARILINK_TOMBSTONE_FILE=tmp_path / "t.jsonl")
    assert r.returncode == 0 and sent == "" and any("export-tombstones" in c for c in calls)


def test_with_journal_reconcile_replaces_the_tombstone_file(job):
    r, calls, sent = job(WARILINK_DELETION_JOURNAL="s3://warilink-journal/prod")
    assert r.returncode == 0, r.stderr
    assert any("journal-reconcile" in c for c in calls) and not any("export-tombstones" in c for c in calls)
    r, calls, sent = job(WARILINK_DELETION_JOURNAL="s3://warilink-journal/prod", STUB_JOURNAL_RC=1)
    assert r.returncode == 1 and "journal_rc=1" in sent  # 確定できない削除が残れば失敗
    assert "warilink-journal" not in sent  # 要約に設定値を出さない


def test_compose_mode_runs_inside_the_production_network(job, tmp_path):
    env_file = tmp_path / "production.env"
    env_file.write_text("WARILINK_DELETION_JOURNAL=s3://warilink-journal/prod\n")
    r, calls, sent = job(WARILINK_COMPOSE_FILE="/srv/warilink/infra/compose.production.yaml",
                         WARILINK_ENV_FILE=env_file)
    assert r.returncode == 0, r.stderr
    assert all(c.startswith(f"compose -f /srv/warilink/infra/compose.production.yaml --env-file {env_file} "
                            "run --rm -T --no-deps api python -m ") for c in calls)
    assert any("journal-reconcile" in c for c in calls)  # env ファイルのジャーナルを読む
    r, calls, sent = job(WARILINK_COMPOSE_FILE="/x.yaml", WARILINK_ENV_FILE=tmp_path / "missing.env")
    assert r.returncode == 1 and calls == []


def test_double_start_is_refused_and_the_lock_is_released(job, tmp_path):
    (tmp_path / "warilink-purge-job.lock").mkdir()
    r, calls, _ = job(WARILINK_TOMBSTONE_FILE=tmp_path / "t.jsonl")
    assert r.returncode == 1 and calls == [] and "already running" in r.stderr
    (tmp_path / "warilink-purge-job.lock").rmdir()
    r, _, _ = job(WARILINK_TOMBSTONE_FILE=tmp_path / "t.jsonl")
    assert r.returncode == 0 and not (tmp_path / "warilink-purge-job.lock").exists()
    r, _, sent = job(WARILINK_TOMBSTONE_FILE=tmp_path / "t.jsonl", STUB_PURGE_RC=1)
    assert r.returncode == 1 and "purge_rc=1" in sent and not (tmp_path / "warilink-purge-job.lock").exists()
