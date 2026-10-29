"""計算ストレステスト(`make test-stress`)の共通部品。tests/api の API 経由のテストからも使う。

- 乱数の種を固定し、何度実行しても同じ入力になるようにする(失敗を再現できる)。
- 件数・時間を集計し、`WARILINK_STRESS_REPORT`(既定 var/stress-report.json)に書き出す。
"""

from __future__ import annotations

import os
import statistics
import time
from collections.abc import Iterator
from contextlib import contextmanager

SEED = int(os.environ.get("WARILINK_STRESS_SEED", "20261030"))

REPORT: dict[str, dict] = {}


class Timer:
    """1件ごとの処理時間(ミリ秒)を集める。"""

    def __init__(self) -> None:
        self.samples: list[float] = []

    @contextmanager
    def measure(self) -> Iterator[None]:
        t = time.perf_counter()
        yield
        self.samples.append((time.perf_counter() - t) * 1000)

    def summary(self) -> dict[str, float]:
        s = sorted(self.samples)
        if not s:
            return {}
        return {
            "count": len(s),
            "p50_ms": round(statistics.median(s), 3),
            "p95_ms": round(s[min(len(s) - 1, int(len(s) * 0.95))], 3),
            "max_ms": round(s[-1], 3),
            "total_s": round(sum(s) / 1000, 2),
        }


def record(name: str, **fields) -> None:
    """レポートに 1 項目の結果を残す(書き出しは tests/stress/conftest.py)。"""
    REPORT[name] = fields
