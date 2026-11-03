import random
from fractions import Fraction

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from app.domain.money import MAX_ABS_AMOUNT, MoneyError, apportion

from ..stress.support import hamilton_violations, oracle_apportion

amounts = st.integers(min_value=-MAX_ABS_AMOUNT, max_value=MAX_ABS_AMOUNT)
weight_maps = st.dictionaries(
    st.text("abcdefghijklmnop", min_size=1, max_size=4), st.integers(0, 10_000), min_size=1, max_size=40
).filter(lambda d: sum(d.values()) > 0)


@settings(max_examples=2000)
@given(total=amounts, weights=weight_maps, seed=st.text(max_size=8))
def test_apportion_sum_and_bounds(total, weights, seed):
    res = apportion(total, weights, seed)
    assert sum(res.values()) == total
    assert set(res) == set(weights)
    W = sum(weights.values())
    for k, w in weights.items():
        assert isinstance(res[k], int)
        ideal = Fraction(abs(total) * w, W)
        # 各人は理想値の floor か ceil(絶対値ベース) = 1円以内の公平性
        assert int(ideal) <= abs(res[k]) <= int(ideal) + (0 if ideal.denominator == 1 else 1)
        if w == 0:
            assert res[k] == 0
        if total != 0 and res[k] != 0:
            assert (res[k] > 0) == (total > 0)


@settings(max_examples=500)
@given(total=amounts, weights=weight_maps, seed=st.text(max_size=8), rnd=st.randoms())
def test_apportion_order_independent(total, weights, seed, rnd):
    items = list(weights.items())
    rnd.shuffle(items)
    assert apportion(total, dict(items), seed) == apportion(total, weights, seed)


@given(total=amounts, weights=weight_maps)
def test_apportion_negative_is_mirror(total, weights):
    assert apportion(-total, weights, "s") == {k: -v for k, v in apportion(total, weights, "s").items()}


def test_jpy_example_4252_by_3():
    res = apportion(4252, {"a": 1, "b": 1, "c": 1}, "exp-1")
    assert sorted(res.values()) == [1417, 1417, 1418]
    assert sum(res.values()) == 4252
    # 決定論的: 何度呼んでも同じ人が1円多い
    assert all(apportion(4252, {"c": 1, "a": 1, "b": 1}, "exp-1") == res for _ in range(5))


def test_tiebreak_varies_with_seed_and_is_fair_over_many_expenses():
    # 同じメンバー構成でも支出ごとに端数を受ける人が変わる(特定の人に偏らない)
    counts = {"a": 0, "b": 0, "c": 0}
    for i in range(3000):
        res = apportion(1, {"a": 1, "b": 1, "c": 1}, f"exp-{i}")
        counts[max(res, key=res.get)] += 1
    assert all(800 < c < 1200 for c in counts.values()), counts


@pytest.mark.parametrize(
    "total,weights,code",
    [
        (100, {}, "NO_PARTICIPANTS"),
        (100, {"a": 0}, "ZERO_WEIGHT"),
        (100, {"a": -1, "b": 2}, "INVALID_WEIGHT"),
        (1.5, {"a": 1}, "INVALID_AMOUNT"),
        (True, {"a": 1}, "INVALID_AMOUNT"),
        (MAX_ABS_AMOUNT + 1, {"a": 1}, "AMOUNT_OUT_OF_RANGE"),
        (100, {"a": 1.0}, "INVALID_WEIGHT"),
    ],
)
def test_apportion_rejects_invalid(total, weights, code):
    with pytest.raises(MoneyError) as e:
        apportion(total, weights, "s")
    assert e.value.code == code


def test_apportion_large_random_cases():
    rng = random.Random(20261018)
    for _ in range(20_000):
        n = rng.randint(1, 60)
        weights = {f"m{i}": rng.randint(0, 1000) for i in range(n)}
        if sum(weights.values()) == 0:
            continue
        total = rng.randint(-10**9, 10**9)
        assert sum(apportion(total, weights, str(_)).values()) == total


# ---- 独立オラクル(Fraction の理想値と仕様どおりの同順位の決め方)との完全一致


@settings(max_examples=3000, deadline=None)
@given(
    total=st.integers(-2_000, 2_000),
    weights=st.dictionaries(st.sampled_from(list("ABCDEFGH")), st.integers(0, 6), min_size=1),
    seed=st.text(max_size=4),
)
def test_apportion_matches_fraction_oracle_small(total, weights, seed):
    """小さい金額・小さい重みは同順位(剰余が等しい人)が多い。誰が 1 円多く払うかまで一致させる。"""
    assume(sum(weights.values()) > 0)
    got = apportion(total, weights, seed)
    assert got == oracle_apportion(total, weights, seed)
    assert hamilton_violations(got, total, weights) == []


@settings(max_examples=1000, deadline=None)
@given(
    total=st.sampled_from([-MAX_ABS_AMOUNT, -(10**12) + 1, -1, 0, 1, 10**12 - 1, MAX_ABS_AMOUNT]),
    weights=st.dictionaries(st.text(min_size=1, max_size=3), st.integers(0, 10**6), min_size=1, max_size=12),
)
def test_apportion_matches_oracle_at_bounds(total, weights):
    assume(sum(weights.values()) > 0)
    got = apportion(total, weights, "b")
    assert got == oracle_apportion(total, weights, "b")
    assert hamilton_violations(got, total, weights) == []


def test_apportion_bound_is_inclusive():
    assert sum(apportion(MAX_ABS_AMOUNT, {"a": 1, "b": 2}, "s").values()) == MAX_ABS_AMOUNT
    with pytest.raises(MoneyError) as e:
        apportion(MAX_ABS_AMOUNT + 1, {"a": 1}, "s")
    assert e.value.code == "AMOUNT_OUT_OF_RANGE"
