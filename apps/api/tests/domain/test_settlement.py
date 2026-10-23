import functools
import random
import time
from types import SimpleNamespace

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


def enumerated_min_transfers(values: list[int]) -> int:
    """独立したオラクル(DP とは別手順): 和0の部分集合を全列挙し、それらで全体を覆う最大分割を探索する。

    最小送金件数 = 非ゼロ人数 - 「和0の部分集合への最大分割数」。20人規模でも、和0の部分集合は少数なので
    全列挙(2^n 回の加算)と、最下位の要素を含む和0部分集合を順に選ぶ探索で厳密に求まる。
    """
    n = len(values)
    full = (1 << n) - 1
    zero_masks: list[int] = []
    sums = [0] * (1 << n)
    for mask in range(1, 1 << n):
        low = mask & -mask
        sums[mask] = sums[mask ^ low] + values[low.bit_length() - 1]
        if sums[mask] == 0:
            zero_masks.append(mask)

    @functools.cache
    def best(rest: int) -> int:
        if rest == 0:
            return 0
        low = rest & -rest
        return max(1 + best(rest ^ m) for m in zero_masks if m & low and m & rest == m)

    return n - best(full)


@functools.cache
def _twenty_people_cases() -> list[tuple[dict[str, int], int]]:
    # CI で失敗した固定入力(seed 3)
    rng = random.Random(3)
    vals = [rng.randint(-50_000, 50_000) for _ in range(19)]
    vals.append(-sum(vals))
    cases = [({f"m{i}": v for i, v in enumerate(vals)}, enumerated_min_transfers([v for v in vals if v != 0]))]
    # 和0のグループを4つ埋め込んだ20人(+a/-a の組は含めない)。受取側は互いに異なる 2 のべき乗(×37)、
    # 支払側は各グループの受取合計。2進表現の一意性から和0の部分集合はグループの和集合に限られ、最小件数は 20 - 4 = 16
    planted: list[int] = []
    exp = 0
    for size in (6, 5, 5, 4):
        part = [37 * 2 ** (exp + k) for k in range(size - 1)]
        exp += size - 1
        part.append(-sum(part))
        planted.extend(part)
    assert len(planted) == 20 and len({abs(v) for v in planted}) == 20
    assert enumerated_min_transfers(planted) == 16
    cases.append(({f"p{i:02d}": v for i, v in enumerate(planted)}, 16))
    return cases


@pytest.mark.parametrize("case", range(2))
def test_exact_20_people_matches_independent_oracle(case):
    """厳密解の正しさ。CI の速度に左右されないよう時間制限を十分に取り、件数はオラクルと照合する。"""
    bal, expected = _twenty_people_cases()[case]
    res = optimize_transfers(bal, time_limit_ms=120_000)
    assert res.is_exact_minimum and res.method == "exact_subset_dp", res.fallback_reason
    assert res.minimum_transfer_count == expected == len(res.transfers)
    verify_transfers(bal, res.transfers)


@pytest.mark.parametrize("case", range(2))
def test_20_people_default_limit_contract(case):
    """既定の 1500ms では、速い環境なら厳密解、遅い環境なら安全なフォールバック。どちらでも契約を満たす。"""
    bal, expected = _twenty_people_cases()[case]
    res = optimize_transfers(bal)
    if res.is_exact_minimum:
        assert res.minimum_transfer_count == expected == len(res.transfers)
    else:
        assert res.method == "greedy_fallback" and res.fallback_reason == "time_limit"
        assert res.minimum_transfer_count is None
        assert len(res.transfers) >= expected
    verify_transfers(bal, res.transfers)


def test_time_limit_fallback_deterministic(monkeypatch):
    """時間切れ分岐を、遅い CPU を模した時計で決定的に確かめる(実時間に依存しない)。"""
    from app.domain import settlement as mod

    clock = {"t": 0.0}

    def slow_clock() -> float:
        clock["t"] += 1.0  # 呼ばれるたびに1秒進む = 極端に遅い環境
        return clock["t"]

    # time モジュール全体ではなく settlement が参照する名前だけを差し替える
    monkeypatch.setattr(mod, "time", SimpleNamespace(perf_counter=slow_clock))
    bal, _ = _twenty_people_cases()[1]
    res = optimize_transfers(bal)
    assert res.method == "greedy_fallback" and res.fallback_reason == "time_limit"
    assert not res.is_exact_minimum and res.minimum_transfer_count is None
    verify_transfers(bal, res.transfers)
    assert sum(t.amount_minor for t in res.transfers) > 0


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
