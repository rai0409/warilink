"""STEP 04: 負荷の簡易確認(scripts/warilink_load_smoke.py)の安全装置と計測の形。

DB を使わない偽の API(同じ経路・同じ応答の形)へつなぐ。実際の API での実測は一時コンテナの隔離環境で別に行い、
docs/TEST_AND_DELIVERY.md「STEP 04」に記録する(このテストの数値は性能の根拠にしない)。
"""

import importlib.util
import itertools
import json
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "warilink_load_smoke.py"


def fake_api(environment="test", fail_expense_every=0):
    counter = itertools.count(1)

    async def health(r: Request):
        return JSONResponse({"status": "ok", "environment": environment})

    async def guest(r: Request):
        assert r.headers.get("x-warilink-csrf") == "1"
        return JSONResponse({}, status_code=201)

    async def group(r: Request):
        body = await r.json()
        assert body["other_member_names"] and "Idempotency-Key" in r.headers
        return JSONResponse({"public_id": f"g{next(counter)}", "members": [{"id": f"m{i}"} for i in range(4)]},
                            status_code=201)

    async def expense(r: Request):
        body = await r.json()
        assert isinstance(body["amount_minor"], int) and body["amount_minor"] > 0  # 整数円
        n = next(counter)
        if fail_expense_every and n % fail_expense_every == 0:
            return JSONResponse({"code": "X"}, status_code=500)
        return JSONResponse({}, status_code=201)

    async def preview(r: Request):
        return JSONResponse({"optimization": {"method": "exact_subset_dp"}})

    async def view(r: Request):
        return JSONResponse({})

    return Starlette(routes=[
        Route("/api/v1/health", health), Route("/api/v1/identity/guest", guest, methods=["POST"]),
        Route("/api/v1/groups", group, methods=["POST"]),
        Route("/api/v1/groups/{pid}/expenses", expense, methods=["POST"]),
        Route("/api/v1/groups/{pid}/settlements/preview", preview, methods=["POST"]),
        Route("/api/v1/groups/{pid}", view)])


@pytest.fixture
def ls():
    spec = importlib.util.spec_from_file_location("warilink_load_smoke_under_test", SCRIPT)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _factory(app):
    return lambda: TestClient(app, base_url="http://127.0.0.1:18100", headers={"X-WariLink-CSRF": "1"})


def _ns(ls, **kw):
    base = {"base_url": "http://127.0.0.1:18100", "users": 3, "concurrency": 2, "expenses": 2, "max_seconds": 60,
            "execute": True}
    return ls.argparse.Namespace(**{**base, **kw})


@pytest.mark.parametrize("url", ["https://warilink.example.com", "http://warilink.example.com:8000",
                                 "http://10.0.0.5:8000", "http://127.0.0.1", "http://127.0.0.1:8000/api",
                                 "http://user@127.0.0.1:8000", "ftp://127.0.0.1:21"])
def test_only_local_isolated_targets_are_allowed(ls, url, capsys):
    def boom():
        raise AssertionError("対象外へ要求を送らない")

    assert ls.main(["--base-url", url, "--execute"], client_factory=boom) == 3
    out = json.loads(capsys.readouterr().out)
    assert out["result"] == "REFUSED" and "対象は http://127.0.0.1" in out["reason"]  # 接続の失敗ではなく対象の拒否


@pytest.mark.parametrize("args", [["--users", "51"], ["--users", "0"], ["--concurrency", "11"],
                                  ["--expenses", "21"], ["--max-seconds", "301"]])
def test_limits_are_bounded(ls, args, capsys):
    assert ls.main(["--base-url", "http://127.0.0.1:18100", *args, "--execute"]) == 3


def test_plan_mode_sends_nothing(ls, capsys):
    def boom():
        raise AssertionError("計画では要求を送らない")

    assert ls.main(["--base-url", "http://127.0.0.1:18100"], client_factory=boom) == 0
    assert json.loads(capsys.readouterr().out)["result"] == "PLANNED"


@pytest.mark.parametrize("env", ["production", None, "staging"])
def test_non_development_apis_are_refused(ls, capsys, env):
    assert ls.main(["--base-url", "http://127.0.0.1:18100", "--execute"],
                   client_factory=_factory(fake_api(environment=env))) == 3
    assert "本番" in json.loads(capsys.readouterr().out)["reason"]


def test_measures_only_what_ran(ls, tmp_path, capsys):
    report = tmp_path / "load.json"
    rc = ls.main(["--base-url", "http://127.0.0.1:18100", "--users", "3", "--concurrency", "2", "--expenses", "2",
                  "--report", str(report), "--execute"], client_factory=_factory(fake_api()))
    out = json.loads(report.read_text())
    assert rc == 0 and out["result"] == "PASS" and out["requests"] == {"succeeded": 3 * 6, "failed": 0}
    assert set(out["operations"]) == {"guest_create", "group_create", "expense_create", "settlement_preview",
                                      "group_view"}
    assert out["operations"]["expense_create"]["count"] == 6
    for op in out["operations"].values():
        assert op["p50_ms"] <= op["p95_ms"] <= op["p99_ms"] <= op["max_ms"]
    assert out["settlement_methods"] == {"exact_subset_dp": 3} and out["success_rate"] == 1.0
    assert out["api_environment"] == "test" and out["target"] == "127.0.0.1:18100"


def test_failures_are_counted_by_operation_and_status(ls):
    out = ls.run(_ns(ls, users=4, expenses=3), client_factory=_factory(fake_api(fail_expense_every=3)))
    assert out["result"] == "FAIL" and out["errors"]["expense_create"] == {"500": out["requests"]["failed"]}
    assert 0 < out["success_rate"] < 1


def test_time_limit_stops_new_requests_and_nothing_measured_is_not_pass(ls):
    ticks = iter([0.0] + [10_000.0] * 100)
    out = ls.run(_ns(ls, users=4, max_seconds=1), client_factory=_factory(fake_api()), clock=lambda: next(ticks))
    assert out["users_cut_by_time_limit"] == 4 and out["requests"] == {"succeeded": 0, "failed": 0}
    assert out["result"] == "FAIL" and out["success_rate"] is None and out["operations"] == {}


def test_percentile_is_nearest_rank(ls):
    v = [float(x) for x in range(1, 101)]
    assert (ls.percentile(v, 50), ls.percentile(v, 95), ls.percentile(v, 99)) == (50.0, 95.0, 99.0)
    assert ls.percentile([7.0], 99) == 7.0
