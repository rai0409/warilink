#!/usr/bin/env python3
"""負荷の簡易確認(STEP 04)。自分で用意した隔離環境の API(localhost)だけを対象にする。本番には使わない。

  warilink_load_smoke.py --base-url http://127.0.0.1:18100 --users 20 --concurrency 5 --expenses 5 \\
      --max-seconds 60 --report load-report.json --execute

- 対象は http://127.0.0.1 / localhost / [::1] のポート付きの URL だけ。
  /api/v1/health の environment が開発・テスト以外なら拒否。
- 架空の利用者・架空の JPY の金額で、ゲストの作成 → グループの作成 → 支出の登録 → 精算案 → グループの表示を行う。
- 上限: 利用者 50・並列 10・支出 20 件/人・時間 300 秒。時間の上限を過ぎたら新しい要求を出さない。
- --execute が無ければ計画を表示するだけ(要求を送らない)。
- 出力は実測だけ: 操作ごとの p50/p95/p99(ms)・成功率・エラーの内訳・精算の方式(厳密解/近似)・環境・commit。
  測っていない値は出さない。

終了コード: 0 = すべての要求が成功 / 1 = 失敗した要求がある / 3 = 拒否(対象・上限・本番)。
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
LIMITS = {"users": (1, 50), "concurrency": (1, 10), "expenses": (1, 20), "max_seconds": (1, 300)}
API = "/api/v1"


class Refused(Exception):
    pass


def check_target(base_url: str) -> None:
    u = urlsplit(base_url)
    if u.scheme != "http" or u.hostname not in LOCAL_HOSTS or not u.port or u.path not in ("", "/") \
            or u.username or u.query:
        raise Refused("対象は http://127.0.0.1:ポート(localhost)の隔離した API だけです")


def check_limits(a: argparse.Namespace) -> None:
    for name, (lo, hi) in LIMITS.items():
        v = getattr(a, name)
        if not lo <= v <= hi:
            raise Refused(f"--{name.replace('_', '-')} は {lo}〜{hi} にしてください")


def percentile(values: list[float], p: float) -> float:
    """最近順位法(実測値のどれか 1 つを返す。補間しない)。"""
    s = sorted(values)
    return s[max(0, math.ceil(p / 100 * len(s)) - 1)]


class Recorder:
    def __init__(self, deadline: float, clock=time.monotonic) -> None:
        self.deadline, self.clock = deadline, clock
        self.lock = threading.Lock()
        self.samples: dict[str, list[float]] = defaultdict(list)
        self.errors: dict[str, Counter] = defaultdict(Counter)
        self.methods: Counter = Counter()
        self.skipped_users = 0

    def call(self, c: httpx.Client, op: str, method: str, path: str, expect: int, **kw) -> httpx.Response | None:
        if self.clock() > self.deadline:
            raise TimeoutError
        t0 = time.perf_counter()
        try:
            r = c.request(method, f"{API}{path}", **kw)
            status = str(r.status_code)
        except httpx.HTTPError as e:
            r, status = None, type(e).__name__
        ms = (time.perf_counter() - t0) * 1000
        with self.lock:
            if r is not None and r.status_code == expect:
                self.samples[op].append(ms)
            else:
                self.errors[op][status] += 1
        return r if r is not None and r.status_code == expect else None


def one_user(make_client, n: int, expenses: int, rec: Recorder) -> None:
    with make_client() as c:
        try:
            if rec.call(c, "guest_create", "POST", "/identity/guest", 201) is None:
                return
            g = rec.call(c, "group_create", "POST", "/groups", 201, headers={"Idempotency-Key": str(uuid.uuid4())},
                         json={"title": f"負荷確認 {n}", "my_name": "Aさん",
                               "other_member_names": ["Bさん", "Cさん", "Dさん"]})
            if g is None:
                return
            body = g.json()
            pid, ids = body["public_id"], [m["id"] for m in body["members"]]
            for k in range(expenses):
                amount = 1000 + (n * 37 + k * 211) % 9000  # 架空の金額(整数円)
                rec.call(c, "expense_create", "POST", f"/groups/{pid}/expenses", 201,
                         headers={"Idempotency-Key": str(uuid.uuid4())},
                         json={"title": f"支出 {k}", "amount_minor": amount,
                               "payers": [{"member_id": ids[k % len(ids)], "amount_minor": amount}],
                               "split": {"mode": "equal", "shares": [{"member_id": m, "value": 1} for m in ids]}})
            p = rec.call(c, "settlement_preview", "POST", f"/groups/{pid}/settlements/preview", 200)
            if p is not None:
                with rec.lock:
                    rec.methods[(p.json().get("optimization") or {}).get("method", "unknown")] += 1
            rec.call(c, "group_view", "GET", f"/groups/{pid}", 200)
        except TimeoutError:
            with rec.lock:
                rec.skipped_users += 1


def _commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def run(a: argparse.Namespace, *, client_factory=None, clock=time.monotonic) -> dict:
    """client_factory は利用者ごとの HTTP クライアントを作る(既定は httpx。テストではアプリへ直接つなぐ)。"""
    check_target(a.base_url)
    check_limits(a)
    report: dict = {"tool": "warilink_load_smoke", "commit": _commit(), "mode": "execute" if a.execute else "plan",
                    "params": {"users": a.users, "concurrency": a.concurrency, "expenses_per_user": a.expenses,
                               "max_seconds": a.max_seconds},
                    "target": f"{urlsplit(a.base_url).hostname}:{urlsplit(a.base_url).port}"}
    if not a.execute:
        return {**report, "result": "PLANNED"}
    make_client = client_factory or (lambda: httpx.Client(base_url=a.base_url, headers={"X-WariLink-CSRF": "1"},
                                                          timeout=10))
    with make_client() as c:
        try:
            env = c.get(f"{API}/health").json().get("environment")
        except (httpx.HTTPError, ValueError):
            raise Refused("対象の API の /health に接続できません") from None
    if env not in ("development", "test"):
        raise Refused("対象の API が開発・テストの環境ではありません(本番には負荷をかけない)")
    rec = Recorder(clock() + a.max_seconds, clock)
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=a.concurrency) as pool:
        for f in [pool.submit(one_user, make_client, n, a.expenses, rec) for n in range(a.users)]:
            f.result()  # 想定外の例外(バグ)は握りつぶさない
    wall = time.monotonic() - t0
    ok = sum(len(v) for v in rec.samples.values())
    bad = sum(sum(v.values()) for v in rec.errors.values())
    ops = {op: {"count": len(v), "p50_ms": round(percentile(v, 50), 1), "p95_ms": round(percentile(v, 95), 1),
                "p99_ms": round(percentile(v, 99), 1), "max_ms": round(max(v), 1)}
           for op, v in sorted(rec.samples.items()) if v}
    return {**report, "api_environment": env, "measured_at": datetime.now(UTC).isoformat(),
            "runtime": {"python": platform.python_version(), "platform": f"{platform.system()} {platform.machine()}"},
            "wall_seconds": round(wall, 2), "requests": {"succeeded": ok, "failed": bad},
            "success_rate": round(ok / (ok + bad), 4) if ok + bad else None,
            "operations": ops, "errors": {op: dict(c) for op, c in rec.errors.items()},
            "settlement_methods": dict(rec.methods), "users_cut_by_time_limit": rec.skipped_users,
            "result": "PASS" if ok and not bad else "FAIL"}


def main(argv: list[str] | None = None, *, client_factory=None) -> int:
    p = argparse.ArgumentParser(prog="warilink_load_smoke.py")
    p.add_argument("--base-url", required=True)
    p.add_argument("--users", type=int, default=10)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--expenses", type=int, default=5)
    p.add_argument("--max-seconds", dest="max_seconds", type=int, default=60)
    p.add_argument("--report", help="結果(JSON)の書き出し先")
    p.add_argument("--execute", action="store_true", help="実行する(付けなければ計画の表示だけ)")
    a = p.parse_args(argv)
    try:
        out = run(a, client_factory=client_factory)
        rc = 0 if out["result"] in ("PASS", "PLANNED") else 1
    except Refused as e:
        out, rc = {"tool": "warilink_load_smoke", "result": "REFUSED", "reason": str(e)}, 3
    text = json.dumps(out, ensure_ascii=False, indent=1)
    if a.report:
        Path(a.report).write_text(text + "\n", encoding="utf-8")
    print(text)
    return rc


if __name__ == "__main__":
    sys.exit(main())
