"""配分エンジンの大量計算: ランダム配分 10 万件・明細 5,000 件・1 円不一致 1 万件。"""

from __future__ import annotations

import random
from fractions import Fraction

import pytest

from app.domain.allocation import (
    Adjustment,
    AdjustmentRule,
    Item,
    ItemAllocation,
    ReceiptSpec,
    compute_receipt,
    split_simple,
    verify_receipt,
)

from .support import SEED, Timer, record

pytestmark = pytest.mark.stress

MODES = ("equal", "weight", "percentage", "exact")


def _amount(rng: random.Random) -> int:
    """端数が出やすい値・境界値を多めに混ぜた金額(0〜10 億円、まれに負=返品)。"""
    r = rng.random()
    if r < 0.05:
        return rng.choice([0, 1, 2, 99, 100, 101, 999_999_999, 1_000_000_000])
    if r < 0.10:
        return -rng.randint(1, 100_000)
    if r < 0.60:
        return rng.randint(1, 100_000)
    return rng.randint(1, 1_000_000_000)


def _members(n: int) -> list[str]:
    return [f"m{i:02d}" for i in range(n)]


def _shares(rng: random.Random, mode: str, members: list[str], total: int) -> dict[str, int]:
    if mode == "equal":
        return dict.fromkeys(members, 1)
    if mode == "weight":
        w = {m: rng.randint(0, 20) for m in members}
        if sum(w.values()) == 0:
            w[members[0]] = 1
        return w
    if mode == "percentage":
        cuts = sorted(rng.randint(0, 10_000) for _ in range(len(members) - 1))
        parts = [b - a for a, b in zip([0, *cuts], [*cuts, 10_000], strict=True)]
        return dict(zip(members, parts, strict=True))
    # exact: 合計が total になる任意の内訳
    rest, out = total, {}
    for m in members[:-1]:
        v = rng.randint(min(0, rest), max(0, rest)) if rest else 0
        out[m], rest = v, rest - v
    out[members[-1]] = rest
    return out


def test_random_splits_100k():
    rng = random.Random(SEED)
    timer = Timer()
    by_mode = dict.fromkeys(MODES, 0)
    max_diff = Fraction(0)
    for case in range(100_000):
        mode = MODES[case % len(MODES)]
        members = _members(rng.randint(1, 50))
        total = _amount(rng)
        shares = _shares(rng, mode, members, total)
        seed = f"s{case}"
        with timer.measure():
            out = split_simple(total, mode, shares, seed)  # type: ignore[arg-type]

        assert sum(out.values()) == total, (case, mode)
        assert set(out) == set(shares)
        if mode != "exact":
            weights = dict.fromkeys(shares, 1) if mode == "equal" else shares
            w_sum = sum(weights.values())
            for m, v in out.items():
                ideal = Fraction(total * weights[m], w_sum)
                diff = abs(v - ideal)
                assert diff < 1, (case, mode, m, v, ideal)  # 理想値との差は 1 円未満(floor か ceil)
                if weights[m] == 0:
                    assert v == 0
                max_diff = max(max_diff, diff)
        # 同じ入力なら同じ結果・並び順に依存しない(一部で確認)
        if case % 50 == 0:
            assert split_simple(total, mode, shares, seed) == out  # type: ignore[arg-type]
            rev = dict(reversed(list(shares.items())))
            assert split_simple(total, mode, rev, seed) == out  # type: ignore[arg-type]
        by_mode[mode] += 1

    record("random_splits", cases=100_000, by_mode=by_mode, max_member_count=50,
           max_abs_deviation_from_ideal_yen=float(max_diff), **timer.summary())


def _big_receipt(rng: random.Random, n_items: int, members: list[str]) -> ReceiptSpec:
    items: list[Item] = []
    for i in range(n_items):
        qty = rng.choice([1, 1, 1, 2, 3, 4, 6])
        line = rng.randint(1, 5_000) * qty
        if rng.random() < 0.01:
            line = -rng.randint(1, 500)  # 返品・取消
        r = rng.random()
        if r < 0.5:
            alloc = ItemAllocation("equal", dict.fromkeys(rng.sample(members, rng.randint(1, len(members))), 1))
        elif r < 0.7:
            picked = rng.sample(members, rng.randint(1, min(6, len(members))))
            alloc = ItemAllocation("weight", {m: rng.randint(1, 5) for m in picked})
        elif r < 0.9:
            picked = rng.sample(members, rng.randint(1, min(qty, len(members))))
            units = dict.fromkeys(picked, 0)
            for k in range(qty):
                units[picked[k % len(picked)]] += 1
            alloc = ItemAllocation("units", units)
        else:
            a, b = rng.sample(members, 2)
            x = rng.randint(min(0, line), max(0, line))
            alloc = ItemAllocation("exact", {a: x, b: line - x})
        items.append(Item(id=f"i{i}", name=f"商品{i}", line_minor=line, quantity=qty,
                          tax_group=rng.choice(["8", "10"]), allocation=alloc))
    taxable8 = sum(it.line_minor for it in items if it.tax_group == "8")
    taxable10 = sum(it.line_minor for it in items if it.tax_group == "10")
    # 税は対象の商品があるときだけ入れる(画面も 0 円の調整行は送らない)
    adjustments = [
        Adjustment(f"tax{g}", "tax", max(0, base * int(g) // 100), f"消費税{g}%", AdjustmentRule(scope_tax_group=g))
        for g, base in (("8", taxable8), ("10", taxable10))
        if any(it.tax_group == g for it in items)
    ] + [
        Adjustment("svc", "service_charge", 12_345, "サービス料", AdjustmentRule(type="equal")),
        Adjustment("disc", "discount", -5_000, "割引", AdjustmentRule(type="members", member_ids=tuple(members[:5]))),
        Adjustment("coupon", "coupon", -777, "クーポン",
                   AdjustmentRule(type="exact", amounts={members[0]: -700, members[1]: -77})),
    ]
    total = sum(it.line_minor for it in items) + sum(a.amount_minor for a in adjustments)
    return ReceiptSpec(items=items, adjustments=adjustments, receipt_total_minor=total)


def test_receipt_5000_items():
    rng = random.Random(SEED + 1)
    members = _members(30)
    spec = _big_receipt(rng, 5_000, members)
    timer = Timer()
    with timer.measure():
        result = compute_receipt(spec, "big")
    assert result.ok, result.issues[:3]
    verify_receipt(spec, result)  # 商品ごと・調整ごと・総額の一致
    assert sum(result.member_totals.values()) == spec.receipt_total_minor
    # 同じ入力・同じ種なら同じ結果
    assert compute_receipt(spec, "big").member_totals == result.member_totals
    s = timer.summary()
    assert s["max_ms"] < 10_000, s  # ゆるい上限(明らかな性能劣化だけを検出する)
    record("receipt_5000_items", items=5_000, members=30, adjustments=len(spec.adjustments),
           receipt_total_minor=spec.receipt_total_minor, elapsed_ms=s["max_ms"])


def test_one_yen_mismatch_10k():
    """合計が 1 円ずれたレシートは必ず不一致として報告し、自動で直さない。差額行の明示追加で一致する。"""
    rng = random.Random(SEED + 2)
    timer = Timer()
    needs_rule = 0
    for case in range(10_000):
        members = _members(rng.randint(2, 8))
        spec = _big_receipt(rng, rng.randint(1, 12), members)
        delta = rng.choice([-1, 1])
        bad = ReceiptSpec(spec.items, spec.adjustments, spec.receipt_total_minor + delta)
        with timer.measure():
            res = compute_receipt(bad, f"c{case}")
        codes = [i.code for i in res.issues]
        assert "RECEIPT_TOTAL_MISMATCH" in codes, case
        mismatch = next(i for i in res.issues if i.code == "RECEIPT_TOTAL_MISMATCH")
        assert mismatch.detail["difference"] == delta
        assert res.breakdown_total_minor == spec.receipt_total_minor  # 内訳は変えていない(補正しない)

        if not compute_receipt(spec, f"c{case}").ok:
            # 返品で比例配分できない等、ずらす前から確認が必要なレシート。不一致の検出だけを確かめる
            needs_rule += 1
            continue
        fixed = ReceiptSpec(spec.items, [*spec.adjustments, Adjustment(
            "diff", "rounding", delta, "差額", AdjustmentRule(type="members", member_ids=(members[0],)))],
            bad.receipt_total_minor)
        ok = compute_receipt(fixed, f"c{case}")
        assert ok.ok, (case, ok.issues[:2])
        assert sum(ok.member_totals.values()) == bad.receipt_total_minor
    assert needs_rule < 1_000  # 大半は差額行の追加まで確かめる
    record("one_yen_mismatch", cases=10_000, detected=10_000, auto_corrected=0,
           resolved_by_explicit_difference_line=10_000 - needs_rule, needed_explicit_rule=needs_rule,
           **timer.summary())
