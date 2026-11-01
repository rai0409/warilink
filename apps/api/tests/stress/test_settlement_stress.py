"""精算の大量計算: 支出 2,000 件の台帳・最少送金回数の全探索との照合・時間切れ時の切り替え。"""

from __future__ import annotations

import random

import pytest

from app.domain.allocation import split_simple
from app.domain.settlement import (
    ExpenseLedgerEntry,
    compute_balances,
    greedy_transfers,
    optimize_transfers,
    verify_transfers,
)

from .support import SEED, Timer, record

pytestmark = pytest.mark.stress


def _members(n: int) -> list[str]:
    return [f"m{i:02d}" for i in range(n)]


def _ledger(rng: random.Random, members: list[str], n_expenses: int) -> list[ExpenseLedgerEntry]:
    out = []
    for e in range(n_expenses):
        amount = rng.randint(1, 300_000)
        payers_n = 1 if rng.random() < 0.8 else rng.randint(2, min(4, len(members)))
        payer_ids = rng.sample(members, payers_n)
        payers = split_simple(amount, "weight", {p: rng.randint(1, 5) for p in payer_ids}, f"p{e}")
        sharers = rng.sample(members, rng.randint(1, len(members)))
        mode = rng.choice(["equal", "weight"])
        weights = dict.fromkeys(sharers, 1) if mode == "equal" else {m: rng.randint(1, 4) for m in sharers}
        shares = split_simple(amount, mode, weights, f"e{e}")  # type: ignore[arg-type]
        out.append(ExpenseLedgerEntry(expense_id=f"e{e}", payers=payers, shares=shares))
    return out


def test_ledger_2000_expenses():
    """2〜50 人のグループで支出 2,000 件。残高の和は 0、精算案どおりに送金すると全員 0 になる。"""
    rng = random.Random(SEED + 10)
    rows = []
    timer = Timer()
    for n in (2, 3, 5, 8, 12, 20, 21, 30, 50):
        members = _members(n)
        ledger = _ledger(rng, members, 2_000)
        bal = compute_balances(members, ledger)
        assert sum(bal.values()) == 0
        with timer.measure():
            res = optimize_transfers(bal)
        verify_transfers(bal, res.transfers)
        nonzero = sum(1 for v in bal.values() if v != 0)
        assert len(res.transfers) <= max(0, nonzero - 1)  # どの方法でも n-1 件以下
        if res.is_exact_minimum:
            assert len(res.transfers) <= len(greedy_transfers(bal))  # 最少は貪欲法より多くならない
        rows.append({"members": n, "expenses": 2_000, "method": res.method, "transfers": len(res.transfers),
                     "elapsed_ms": res.elapsed_ms})
    record("ledger_2000_expenses", groups=rows, **timer.summary())


def _min_transfers_bruteforce(values: list[int]) -> int:
    """全探索の答え: 合計 0 の部分集合への分割の最大個数 k から n - k。"""
    n = len(values)
    best = 0

    def go(rest: list[int], parts: int) -> None:
        nonlocal best
        if not rest:
            best = max(best, parts)
            return
        first, others = rest[0], rest[1:]
        m = len(others)
        for mask in range(1 << m):
            group = [others[i] for i in range(m) if mask >> i & 1]
            if first + sum(group) == 0:
                go([others[i] for i in range(m) if not mask >> i & 1], parts + 1)

    go(values, 0)
    return n - best


def test_exact_minimum_matches_bruteforce():
    """8 人以下では、最少送金回数が全探索の答えと一致する(2,000 ケース)。"""
    rng = random.Random(SEED + 11)
    timer = Timer()
    for case in range(2_000):
        n = rng.randint(2, 8)
        vals = [rng.choice([rng.randint(-50, 50), rng.randint(-5_000, 5_000)]) for _ in range(n - 1)]
        vals.append(-sum(vals))
        bal = {f"m{i}": v for i, v in enumerate(vals)}
        with timer.measure():
            res = optimize_transfers(bal)
        assert res.is_exact_minimum, case
        verify_transfers(bal, res.transfers)
        expected = _min_transfers_bruteforce([v for v in vals if v != 0])
        assert len(res.transfers) == expected, (case, vals, len(res.transfers), expected)
    record("exact_vs_bruteforce", cases=2_000, max_members=8, **timer.summary())


def test_time_limit_falls_back_to_greedy():
    """上限時間を超えたら貪欲法に切り替え、それでも残高は必ず 0 になる。"""
    rng = random.Random(SEED + 12)
    fallbacks = 0
    overshoot: list[float] = []
    for case in range(200):
        n = 20
        vals = [rng.randint(-10**9, 10**9) for _ in range(n - 1)]
        vals.append(-sum(vals))
        bal = {f"m{i:02d}": v for i, v in enumerate(vals)}
        res = optimize_transfers(bal, time_limit_ms=1)
        verify_transfers(bal, res.transfers)
        if not res.is_exact_minimum:
            assert res.method == "greedy_fallback" and res.fallback_reason == "time_limit", case
            fallbacks += 1
            overshoot.append(res.elapsed_ms - 1)
    over = {f"m{i:02d}": v for i, v in enumerate([1] * 25 + [-25])}
    res = optimize_transfers(over)
    verify_transfers(over, res.transfers)
    assert res.fallback_reason == "participant_limit"
    # 上限を超えてから切り替わるまでの遅れ(既定の上限 1,500 ms に対する余分な待ち時間の目安)
    record("time_limit_fallback", cases=200, fell_back=fallbacks, participant_limit_checked=True,
           overshoot_p50_ms=round(sorted(overshoot)[len(overshoot) // 2], 1) if overshoot else None,
           overshoot_max_ms=round(max(overshoot), 1) if overshoot else None)


def test_refunds_zero_amounts_and_member_limit():
    """返品(負の支出)・0 円・API の上限(100 人)でも、残高の和は 0 で、精算案どおりに送金すると全員 0。"""
    rng = random.Random(SEED + 13)
    rows = []
    for n in (2, 20, 50, 100):
        members = _members(n)
        ledger = _ledger(rng, members, 500)
        for e in range(200):  # 返品・取消(負の金額)と 0 円の支出を混ぜる
            amount = rng.choice([0, -rng.randint(1, 50_000)])
            payer = rng.choice(members)
            sharers = rng.sample(members, rng.randint(1, n))
            shares = split_simple(amount, "equal", dict.fromkeys(sharers, 1), f"r{e}")
            ledger.append(ExpenseLedgerEntry(expense_id=f"r{e}", payers={payer: amount}, shares=shares))
        bal = compute_balances(members, ledger)
        assert sum(bal.values()) == 0
        res = optimize_transfers(bal)
        verify_transfers(bal, res.transfers)
        assert all(t.amount_minor > 0 for t in res.transfers)
        rows.append({"members": n, "method": res.method, "transfers": len(res.transfers)})
    record("refunds_zero_member_limit", groups=rows)


def test_slow_cpu_at_the_20_person_boundary():
    """CPU が遅く上限時間に届かない場合を、上限を縮めて再現する。どちらの方法でも金額は正しく、厳密解は貪欲法以下。"""
    rng = random.Random(SEED + 14)
    outcomes: dict[str, int] = {}
    for case in range(30):
        vals = [rng.randint(-10**6, 10**6) for _ in range(19)]
        vals.append(-sum(vals))
        bal = {f"m{i:02d}": v for i, v in enumerate(vals)}
        greedy = len(greedy_transfers(bal))
        for limit_ms in (50, 200, 1500):
            res = optimize_transfers(bal, time_limit_ms=limit_ms)
            verify_transfers(bal, res.transfers)
            assert len(res.transfers) <= greedy, case
            key = f"{limit_ms}ms:{res.method}"
            outcomes[key] = outcomes.get(key, 0) + 1
    record("slow_cpu_20_boundary", cases=30, outcomes=outcomes)
