"""PR C: 監視(python -m app.ops.monitor)。確認の内容・通知の重複の抑制・通知に個人情報や URL を含めないこと。"""

from datetime import timedelta

from sqlalchemy.orm import Session

from app.core.db import get_engine
from app.core.security import now
from app.modules.identity.retention import record_run
from app.ops import monitor

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
