import random
import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.money import InvariantViolation, MoneyError
from app.domain.settlement import (
    ExpenseLedgerEntry,
    Transfer,
    compute_balances,
    greedy_transfers,
    naive_transfer_count,
    optimize_transfers,
    verify_transfers,
)


def brute_force_min_transfers(balances: list[int]) -> int:
    """独立したオラクル: 古典的なバックトラッキング全探索(DP とは別アルゴリズム)。"""
    debts = [b for b in balances if b != 0]

    def dfs(start: int) -> int:
        while start < len(debts) and debts[start] == 0:
            start += 1
        if start == len(debts):
            return 0
        best = 10**9
        for i in range(start + 1, len(debts)):
            if debts[i] * debts[start] < 0:
                debts[i] += debts[start]
                best = min(best, 1 + dfs(start + 1))
                debts[i] -= debts[start]
        return best

    return dfs(0)


@st.composite
def zero_sum_balances(draw, max_n=7, max_abs=50):
    n = draw(st.integers(1, max_n))
    vals = draw(st.lists(st.integers(-max_abs, max_abs), min_size=n, max_size=n))
    vals.append(-sum(vals))
    return {f"m{i}": v for i, v in enumerate(vals)}


@settings(max_examples=1500, deadline=None)
@given(bal=zero_sum_balances())
def test_exact_matches_brute_force(bal):
    res = optimize_transfers(bal)
    assert res.is_exact_minimum and res.method in ("exact_subset_dp", "none")
    assert len(res.transfers) == brute_force_min_transfers(list(bal.values()))
    assert res.minimum_transfer_count == len(res.transfers)
    verify_transfers(bal, res.transfers)


@settings(max_examples=300, deadline=None)
@given(bal=zero_sum_balances(max_n=8, max_abs=5))
def test_exact_matches_brute_force_many_ties(bal):
    # 小さい値域 → 部分集合で 0 になる組合せが多く、貪欲法との差が出やすい
    res = optimize_transfers(bal)
    assert len(res.transfers) == brute_force_min_transfers(list(bal.values()))


def test_greedy_is_not_always_minimal_but_exact_is():
    # 厳密解は全探索と一致し、貪欲法は件数が多くなる反例が存在することを確認する
    bal = {"a": 6, "b": 4, "c": -5, "d": -5}
    exact = optimize_transfers(bal)
    assert len(exact.transfers) == brute_force_min_transfers(list(bal.values()))
    found = False
    rng = random.Random(1)
    for _ in range(3000):
        vals = [rng.randint(-20, 20) for _ in range(6)]
        vals.append(-sum(vals))
        b = {f"m{i}": v for i, v in enumerate(vals)}
        if len(greedy_transfers(b)) > len(optimize_transfers(b).transfers):
            found = True
            break
    assert found, "貪欲法が非最小となる反例が見つかるはず"


@settings(max_examples=500, deadline=None)
@given(bal=zero_sum_balances(max_n=30, max_abs=10**9))
def test_any_result_settles_to_zero(bal):
    # 厳密解(≤12人)と貪欲法(>12人)の両経路で、送金後の残高が必ず0になることを検証
    res = optimize_transfers(bal, exact_max_participants=12)
    rest = dict(bal)
    for t in res.transfers:
        assert isinstance(t.amount_minor, int) and t.amount_minor > 0 and t.from_member != t.to_member
        rest[t.from_member] += t.amount_minor
        rest[t.to_member] -= t.amount_minor
    assert all(v == 0 for v in rest.values())
    nz = sum(1 for v in bal.values() if v)
    assert len(res.transfers) <= max(nz - 1, 0)


def test_participant_limit_fallback():
    rng = random.Random(7)
    vals = [rng.randint(-10**6, 10**6) for _ in range(40)]
    vals.append(-sum(vals))
    bal = {f"m{i}": v for i, v in enumerate(vals)}
    res = optimize_transfers(bal)
    assert res.method == "greedy_fallback" and not res.is_exact_minimum
    assert res.fallback_reason == "participant_limit" and res.minimum_transfer_count is None
    verify_transfers(bal, res.transfers)


def test_time_limit_fallback_is_safe():
    rng = random.Random(11)
    vals = [rng.randint(-10**6, 10**6) for _ in range(19)]
    vals.append(-sum(vals))
    bal = {f"m{i}": v for i, v in enumerate(vals)}
    t0 = time.perf_counter()
    res = optimize_transfers(bal, exact_max_participants=30, time_limit_ms=50)
    assert (time.perf_counter() - t0) < 3
    assert res.method == "greedy_fallback" and res.fallback_reason == "time_limit"
    assert not res.is_exact_minimum
    verify_transfers(bal, res.transfers)


def test_exact_20_people_within_default_limit():
    rng = random.Random(3)
    vals = [rng.randint(-50_000, 50_000) for _ in range(19)]
    vals.append(-sum(vals))
    res = optimize_transfers({f"m{i}": v for i, v in enumerate(vals)})
    assert res.is_exact_minimum, res.fallback_reason


def test_opposite_pairs_shortcut():
    res = optimize_transfers({"a": 500, "b": -500, "c": 300, "d": -100, "e": -200})
    assert len(res.transfers) == 3 and res.is_exact_minimum


def test_empty_and_rejects_nonzero_sum():
    assert optimize_transfers({"a": 0, "b": 0}).transfers == []
    with pytest.raises(MoneyError):
        optimize_transfers({"a": 1, "b": 0})


def test_verify_transfers_detects_errors():
    with pytest.raises(InvariantViolation):
        verify_transfers({"a": 100, "b": -100}, [Transfer("b", "a", 99)])
    with pytest.raises(InvariantViolation):
        verify_transfers({"a": 100, "b": -100}, [Transfer("b", "b", 100)])


# ---- 複数の立替と精算による残高整合性


@settings(max_examples=500, deadline=None)
@given(data=st.data())
def test_balances_multi_expense_and_partial_settlement(data):
    from app.domain.allocation import ExpenseSpec, compute_expense

    members = [f"m{i}" for i in range(data.draw(st.integers(2, 10)))]
    entries = []
    for k in range(data.draw(st.integers(1, 15))):
        amount = data.draw(st.integers(1, 10**7))
        payer_set = data.draw(st.lists(st.sampled_from(members), min_size=1, max_size=3, unique=True))
        cut = data.draw(st.integers(0, amount)) if len(payer_set) > 1 else amount
        payers = {payer_set[0]: cut}
        if len(payer_set) > 1:
            payers[payer_set[1]] = payers.get(payer_set[1], 0) + amount - cut
        sharers = data.draw(st.lists(st.sampled_from(members), min_size=1, unique=True))
        r = compute_expense(ExpenseSpec(amount, payers, "equal", {m: 1 for m in sharers}), f"e{k}", set(members))
        r.require_valid()
        entries.append(ExpenseLedgerEntry(f"e{k}", payers, r.shares))
    bal = compute_balances(members, entries)
    assert sum(bal.values()) == 0
    plan = optimize_transfers(bal)
    # 一部だけ送金済み(ロック)にしても残りの再最適化で残高は0になる
    locked = plan.transfers[: data.draw(st.integers(0, len(plan.transfers)))]
    residual = compute_balances(members, entries, locked)
    assert sum(residual.values()) == 0
    again = optimize_transfers(residual)
    verify_transfers(residual, again.transfers)
    assert naive_transfer_count(entries) >= 0
