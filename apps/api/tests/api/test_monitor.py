"""PR C: 監視(python -m app.ops.monitor)。確認の内容・通知の重複の抑制・通知に個人情報や URL を含めないこと。"""

import uuid
from datetime import timedelta

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_engine
from app.core.security import now
from app.models.tables import DeletionJournalOp
from app.modules.identity.retention import record_run
from app.ops import monitor
from app.ops.deletion_journal import LocalDirJournal

SECRET_URL = "https://api.example.invalid/?token=secret-invite-token"


def _record(job, ok=True, hours_ago=1):
    with Session(get_engine()) as db:
        record_run(db, job, ok=ok, at=now() - timedelta(hours=hours_ago))


def _by_name(checks):
    return {c.name: (c.ok, c.detail) for c in checks}


def test_jobs_must_have_recent_successes():
    c = _by_name(monitor.run_checks(env={}))
    assert c["db"] == (True, "ok")
    assert c["job:retention_purge"] == (False, "never") and c["job:backup"] == (False, "never")
    _record("retention_purge")
    _record("backup", hours_ago=30)
    _record("tombstone_export")
    _record("tombstone_export", ok=False, hours_ago=0)  # 成功のあとに失敗した
    c = _by_name(monitor.run_checks(env={}))
    assert c["job:retention_purge"][0] is True
    assert c["job:backup"] == (False, "stale 30h")
    assert c["job:tombstone_export"] == (False, "last_run_failed")
    c = _by_name(monitor.run_checks(env={"WARILINK_MONITOR_MAX_AGE_BACKUP_HOURS": "48"}))
    assert c["job:backup"][0] is True  # 許容時間は設定で変えられる


def test_health_check_never_leaks_the_url():
    def boom(url):
        raise RuntimeError(f"cannot connect to {url}")

    for get, expected in ((lambda u: 200, (True, "http 200")), (lambda u: 503, (False, "http 503")),
                          (boom, (False, "unreachable"))):
        checks = monitor.run_checks(env={"WARILINK_MONITOR_API_URL": SECRET_URL}, http_get=get)
        assert _by_name(checks)["api:health"] == expected
        assert "secret" not in monitor.summarize(checks) and "example" not in monitor.summarize(checks)


def test_alerts_are_deduplicated_and_recovery_is_sent_once(tmp_path):
    state, sent = tmp_path / "s.json", []
    t0 = now()

    def run(cmd, msg):
        sent.append(msg)
        return 0

    kw = {"state_path": state, "dedup_hours": 6, "command": "notify", "run": run}
    bad = "warilink monitor: job:backup=never"
    assert monitor.deliver(bad, True, at=t0, **kw) == "sent"
    assert monitor.deliver(bad, True, at=t0 + timedelta(hours=1), **kw) == "suppressed"
    assert monitor.deliver(bad + ", db=unreachable", True, at=t0 + timedelta(hours=2), **kw) == "sent"  # 内容が変わった
    assert monitor.deliver(bad + ", db=unreachable", True, at=t0 + timedelta(hours=9), **kw) == "sent"  # 時間が経った
    assert monitor.deliver("warilink monitor: recovered", False, at=t0 + timedelta(hours=10), **kw) == "sent"
    assert monitor.deliver("warilink monitor: recovered", False, at=t0 + timedelta(hours=11), **kw) == "not_needed"
    assert len(sent) == 4


def test_failed_or_missing_alert_delivery_is_reported(tmp_path, capsys):
    state = tmp_path / "s.json"
    r = monitor.deliver("warilink monitor: db=unreachable", True, at=now(), state_path=state, dedup_hours=6,
                        command="notify", run=lambda c, m: 1)
    assert r == "failed" and not state.exists()  # 送れなかったら次回また送る
    r = monitor.deliver("warilink monitor: db=unreachable", True, at=now(), state_path=state, dedup_hours=6,
                        command=None)
    assert r == "not_configured" and "db=unreachable" in capsys.readouterr().err  # ALERT_DELIVERY=BLOCKED


def test_main_exit_codes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WARILINK_MONITOR_STATE", str(tmp_path / "s.json"))
    monkeypatch.delenv("WARILINK_MONITOR_API_URL", raising=False)
    monkeypatch.setenv("WARILINK_ALERT_CMD", "exit 1")  # 通知コマンドの失敗
    assert monitor.main(["--json"]) == 2
    monkeypatch.setenv("WARILINK_ALERT_CMD", "cat > /dev/null")
    assert monitor.main(["--json"]) == 1  # 異常あり(ジョブの記録が無い)
    for job in monitor.JOBS:
        _record(job)
    monkeypatch.setenv("WARILINK_FEATURE_OCR", "false")
    from app.core.config import get_settings

    get_settings.cache_clear()
    assert monitor.main(["--json"]) == 0
    get_settings.cache_clear()


def test_job_timestamp_order_and_future():
    from types import SimpleNamespace

    t = now()
    for success, failure, expected in (
        (t - timedelta(hours=1), t, False),
        (t - timedelta(hours=1), t - timedelta(hours=2), True),
        (t - timedelta(hours=1), t - timedelta(hours=1), False),
        (t - timedelta(hours=49), None, False),
        (t + timedelta(hours=1), None, False),
    ):
        row = SimpleNamespace(job="backup", last_success_at=success, last_failure_at=failure)
        db = SimpleNamespace(query=lambda model, current=row: SimpleNamespace(all=lambda: [current]))
        assert monitor.check_jobs(db, t, {"backup": 48})[0].ok is expected


# ---------------------------------------------------------------- STEP 04: 未構成・削除ジャーナル


def test_production_reports_unconfigured_monitoring_instead_of_ok(monkeypatch):
    """本番で外形監視の URL・通知先・外部保管・削除ジャーナルが無ければ「未構成」の異常(測れないものを正常にしない)。"""
    monkeypatch.setattr(get_settings(), "environment", "production")
    for job in monitor.JOBS:
        _record(job)
    c = _by_name(monitor.run_checks(env={}))
    assert c["api:health"] == (False, "not_configured")
    for name in ("config:alert_cmd", "config:api_url", "config:offsite", "config:deletion_journal"):
        assert c[name] == (False, "not_configured"), name
    env = {"WARILINK_ALERT_CMD": "logger", "WARILINK_MONITOR_API_URL": SECRET_URL,
           "WARILINK_BACKUP_OFFSITE": "s3://warilink-backup-prod/daily"}
    monkeypatch.setattr(get_settings(), "deletion_journal", "s3://warilink-journal-prod/deletions")
    checks = monitor.run_checks(env=env, http_get=lambda u: 200)
    c = _by_name(checks)
    assert all(c[n][0] for n in ("config:alert_cmd", "config:api_url", "config:offsite", "config:deletion_journal"))
    assert c["api:health"] == (True, "http 200")
    summary = monitor.summarize(checks)
    assert "secret" not in summary and "warilink-backup-prod" not in summary and "logger" not in summary


def test_non_production_keeps_the_previous_checks():
    c = _by_name(monitor.run_checks(env={}))
    assert "api:health" not in c and not any(n.startswith("config:") for n in c)  # 開発・テストは従来どおり


def test_deletion_journal_health_and_required_jobs(tmp_path, monkeypatch):
    assert "deletion_journal" not in _by_name(monitor.run_checks(env={}))  # 未設定(開発)
    root = tmp_path / "secret-journal-dir"
    monkeypatch.setenv("WARILINK_DELETION_JOURNAL", f"local:{root}")
    get_settings.cache_clear()
    try:
        for job in ("retention_purge", "backup"):
            _record(job)
        j = LocalDirJournal(root)
        op = uuid.uuid4()
        j.prepare(op, [("identity", uuid.uuid4())])
        c = _by_name(monitor.run_checks(env={}))
        assert c["deletion_journal"] == (True, "ok seq 1")
        assert "job:tombstone_export" not in c  # ジャーナルを使い、一覧ファイルを置かない構成では必須にしない
        assert "job:tombstone_export" in _by_name(monitor.run_checks(env={"WARILINK_TOMBSTONE_FILE": "/x"}))
        late = monitor.run_checks(env={}, at=now() + timedelta(hours=2))
        assert _by_name(late)["deletion_journal"] == (False, "pending 1")
        j.finalize(op)
        assert _by_name(monitor.run_checks(env={}, at=now() + timedelta(hours=2)))["deletion_journal"][0] is True
        # DB 側で確定の印が付いていない操作(ジャーナルの確定の後に DB を更新できなかった)も知らせる
        with Session(get_engine()) as db:
            db.add(DeletionJournalOp(op_id=uuid.uuid4(), created_at=now() - timedelta(hours=3)))
            db.commit()
        assert _by_name(monitor.run_checks(env={}))["deletion_journal"] == (False, "unconfirmed 1")
        (root / "HEAD").write_text("{")
        checks = monitor.run_checks(env={})
        assert _by_name(checks)["deletion_journal"] == (False, "corrupt")
        assert "secret-journal-dir" not in monitor.summarize(checks)
        monkeypatch.setenv("WARILINK_DELETION_JOURNAL", "gs://bucket/x")
        get_settings.cache_clear()
        assert _by_name(monitor.run_checks(env={}))["deletion_journal"] == (False, "misconfigured")
    finally:
        get_settings.cache_clear()
