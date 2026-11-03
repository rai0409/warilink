"""計算ストレステスト(`make test-stress`)の共通部品。tests/api の API 経由のテストからも使う。

- 乱数の種を固定し、何度実行しても同じ入力になるようにする(失敗を再現できる)。
- 件数・時間を集計し、`WARILINK_STRESS_REPORT`(既定 var/stress-report.json)に書き出す。
"""

from __future__ import annotations

import hashlib
import os
import statistics
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from fractions import Fraction

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


# ---------------------------------------------------------------- 独立オラクル
#
# 本体(app/domain)の戻り値を使わず、仕様(docs/SETTLEMENT_ENGINE.md)から期待値を組み立てる。
# 端数は Fraction の理想値から決め、divmod や本体の関数は使わない。単体テストとストレステストの両方で使う。


def oracle_apportion(total: int, weights: Mapping[str, int], seed: str) -> dict[str, int]:
    """最大剰余法の仕様どおりの配分。理想値 |total|·w/W の整数部を配り、残りの 1 円は
    小数部の大きい順(同値は sha256(seed + "\\x1f" + key) の16進文字列の昇順)に 1 人 1 円ずつ。符号は最後に戻す。"""
    w_sum = sum(weights.values())
    mag = abs(total)
    ideal = {k: Fraction(mag * w, w_sum) for k, w in weights.items()}
    floor = {k: q.numerator // q.denominator for k, q in ideal.items()}
    order = sorted(weights, key=lambda k: (-(ideal[k] - floor[k]),
                                           hashlib.sha256(f"{seed}\x1f{k}".encode()).hexdigest()))
    out = dict(floor)
    for k in order[: mag - sum(floor.values())]:
        out[k] += 1
    sign = -1 if total < 0 else 1
    return {k: sign * v for k, v in out.items()}


def hamilton_violations(result: Mapping[str, int], total: int, weights: Mapping[str, int]) -> list[str]:
    """配分結果が最大剰余法の性質を満たすかを、配り方の手順に依らずに調べる(空なら合格)。

    - 合計が total と一致し、キーが同じ
    - 各人が理想値の floor か ceil(理想値が整数ならそのもの)
    - 「切り上げられた人」の小数部が、「切り捨てられた人」の小数部より小さいことはない
    """
    errs: list[str] = []
    if set(result) != set(weights):
        errs.append("keys")
    if sum(result.values()) != total:
        errs.append("sum")
    w_sum = sum(weights.values())
    mag = abs(total)
    sign = -1 if total < 0 else 1
    up: list[Fraction] = []
    down: list[Fraction] = []
    for k, w in weights.items():
        q = Fraction(mag * w, w_sum)
        v = sign * result.get(k, 0)
        frac = q - (q.numerator // q.denominator)
        if v == q:
            continue
        if not (q - 1 < v < q + 1):
            errs.append(f"{k}: {v} は理想値 {q} の floor/ceil ではない")
        elif v > q:
            up.append(frac)
        else:
            down.append(frac)
    if up and down and min(up) < max(down):
        errs.append("小数部の小さい人が切り上げられている")
    return errs


def oracle_receipt(spec, seed: str) -> dict[str, dict[str, int]]:
    """商品別レシートの期待値(商品ごと・調整ごとの配分、メンバー別合計)を仕様から組み立てる。

    正しいレシート(問題が出ないもの)だけを対象にする。種は仕様どおり `{seed}:item:{id}` / `{seed}:adj:{id}`。
    """
    out: dict[str, dict[str, int]] = {}
    for it in spec.items:
        a = it.allocation
        if a.mode == "exact":
            out[it.id] = dict(a.shares)
        else:
            w = dict.fromkeys(a.shares, 1) if a.mode == "equal" else dict(a.shares)
            out[it.id] = oracle_apportion(it.line_minor, w, f"{seed}:item:{it.id}")
    for adj in spec.adjustments:
        r = adj.rule
        s = f"{seed}:adj:{adj.id}"
        if r.type == "exact":
            out[adj.id] = dict(r.amounts)
            continue
        if r.type == "members":
            out[adj.id] = oracle_apportion(adj.amount_minor, dict.fromkeys(r.member_ids, 1), s)
            continue
        if r.scope_item_ids is not None:
            scope = [it for it in spec.items if it.id in r.scope_item_ids]
        elif r.scope_tax_group is not None:
            scope = [it for it in spec.items if it.tax_group == r.scope_tax_group]
        else:
            scope = list(spec.items)
        base: dict[str, int] = {}
        for it in scope:
            for m, v in out[it.id].items():
                base[m] = base.get(m, 0) + v
        if r.type == "equal":
            who = [m for m, v in base.items() if v != 0] or list(base)
            out[adj.id] = oracle_apportion(adj.amount_minor, dict.fromkeys(who, 1), s)
        else:
            out[adj.id] = oracle_apportion(adj.amount_minor, base, s)
    totals: dict[str, int] = {}
    for alloc in out.values():
        for m, v in alloc.items():
            totals[m] = totals.get(m, 0) + v
    return {"allocations": out, "member_totals": totals}
