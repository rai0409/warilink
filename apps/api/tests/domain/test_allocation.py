import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from app.domain.allocation import (
    Adjustment,
    AdjustmentRule,
    ExpenseSpec,
    Item,
    ItemAllocation,
    ReceiptSpec,
    compute_expense,
    compute_receipt,
    split_simple,
)
from app.domain.money import MoneyError

from ..stress.support import hamilton_violations, oracle_apportion, oracle_receipt

MEMBERS = ["A", "B", "C", "D", "E", "F"]


# ---------------------------------------------------------------- 単純分割


@settings(max_examples=1000)
@given(
    total=st.integers(-10**10, 10**10),
    members=st.lists(st.sampled_from(MEMBERS), min_size=1, unique=True),
    mode=st.sampled_from(["equal", "weight", "units"]),
    data=st.data(),
)
def test_split_simple_sum(total, members, mode, data):
    shares = {m: data.draw(st.integers(1, 1000)) for m in members}
    res = split_simple(total, mode, shares, "seed")
    assert sum(res.values()) == total


@given(total=st.integers(0, 10**9), data=st.data())
def test_percentage_split(total, data):
    n = data.draw(st.integers(1, 6))
    cuts = sorted(data.draw(st.lists(st.integers(0, 10000), min_size=n - 1, max_size=n - 1)))
    bps = [b - a for a, b in zip([0, *cuts], [*cuts, 10000], strict=True)]
    res = split_simple(total, "percentage", dict(zip(MEMBERS, bps, strict=False)), "s")
    assert sum(res.values()) == total


def test_percentage_must_sum_100():
    with pytest.raises(MoneyError) as e:
        split_simple(1000, "percentage", {"A": 5000, "B": 4000}, "s")
    assert e.value.code == "PERCENTAGE_SUM_MISMATCH"


def test_exact_must_match():
    assert split_simple(1000, "exact", {"A": 300, "B": 700}, "s") == {"A": 300, "B": 700}
    with pytest.raises(MoneyError) as e:
        split_simple(1000, "exact", {"A": 300, "B": 600}, "s")
    assert e.value.code == "SHARE_SUM_MISMATCH"


# ---------------------------------------------------------------- 商品別


def test_beer_three_glasses_two_and_one():
    # ビール3杯(1,500円)を A 2杯・B 1杯
    spec = ReceiptSpec(
        items=[Item("beer", "生ビール", 1500, 3, None, ItemAllocation("units", {"A": 2, "B": 1}))],
        adjustments=[],
        receipt_total_minor=1500,
    )
    r = compute_receipt(spec, "e1")
    assert r.ok
    assert r.item_allocations["beer"] == {"A": 1000, "B": 500}


def test_units_must_match_quantity():
    spec = ReceiptSpec(
        [Item("beer", "生ビール", 1500, 3, None, ItemAllocation("units", {"A": 1, "B": 1}))], [], 1500
    )
    r = compute_receipt(spec, "e1")
    assert [i.code for i in r.issues] == ["UNITS_MISMATCH"]
    with pytest.raises(MoneyError):
        r.require_valid()


def test_tax_example_2970():
    # 2,970円 = 商品2,700 + 外税10% 270
    spec = ReceiptSpec(
        items=[
            Item("a", "唐揚げ", 1000, 1, "10", ItemAllocation("equal", {"A": 1, "B": 1, "C": 1})),
            Item("b", "刺身", 1700, 1, "10", ItemAllocation("equal", {"A": 1, "C": 1})),
        ],
        adjustments=[Adjustment("t", "tax", 270, "消費税10%", AdjustmentRule("proportional", scope_tax_group="10"))],
        receipt_total_minor=2970,
    )
    r = compute_receipt(spec, "e2")
    assert r.ok
    assert sum(r.member_totals.values()) == 2970
    assert sum(r.adjustment_allocations["t"].values()) == 270


def test_mixed_tax_rates_apply_only_to_matching_items():
    spec = ReceiptSpec(
        items=[
            Item("food", "弁当", 1000, 1, "8", ItemAllocation("equal", {"A": 1})),
            Item("drink", "酒", 1000, 1, "10", ItemAllocation("equal", {"B": 1})),
        ],
        adjustments=[
            Adjustment("t8", "tax", 80, "8%", AdjustmentRule("proportional", scope_tax_group="8")),
            Adjustment("t10", "tax", 100, "10%", AdjustmentRule("proportional", scope_tax_group="10")),
        ],
        receipt_total_minor=2180,
    )
    r = compute_receipt(spec, "e")
    assert r.member_totals == {"A": 1080, "B": 1100}


def test_mismatch_is_reported_not_corrected():
    spec = ReceiptSpec(
        [Item("a", "x", 1000, 1, None, ItemAllocation("equal", {"A": 1}))], [], receipt_total_minor=1100
    )
    r = compute_receipt(spec, "e")
    assert not r.ok
    issue = r.issues[0]
    assert issue.code == "RECEIPT_TOTAL_MISMATCH"
    assert issue.detail["difference"] == 100
    # 金額は勝手に補正されない
    assert r.member_totals == {"A": 1000}


def test_explicit_difference_adjustment_resolves_mismatch():
    spec = ReceiptSpec(
        [Item("a", "x", 1000, 1, None, ItemAllocation("equal", {"A": 1, "B": 1}))],
        [Adjustment("diff", "other", 100, "差額(ユーザー確認済み)", AdjustmentRule("equal"))],
        receipt_total_minor=1100,
    )
    assert compute_receipt(spec, "e").ok


def test_unallocated_item_flagged():
    items = [Item("a", "x", 1000), Item("b", "y", 500, allocation=ItemAllocation("equal", {"A": 1}))]
    spec = ReceiptSpec(items, [], 1500)
    r = compute_receipt(spec, "e")
    assert [(i.code, i.target) for i in r.issues] == [("ITEM_UNALLOCATED", "a")]


def test_discount_sign_enforced():
    spec = ReceiptSpec(
        [Item("a", "x", 1000, 1, None, ItemAllocation("equal", {"A": 1}))],
        [Adjustment("d", "discount", 100)],
        1100,
    )
    assert "ADJUSTMENT_SIGN_INVALID" in [i.code for i in compute_receipt(spec, "e").issues]


def test_return_and_negative_adjustments():
    spec = ReceiptSpec(
        items=[
            Item("a", "ピザ", 2000, 1, None, ItemAllocation("equal", {"A": 1, "B": 1, "C": 1})),
            Item("r", "返品 ピザ", -500, 1, None, ItemAllocation("equal", {"A": 1, "B": 1})),
        ],
        adjustments=[
            Adjustment("c", "coupon", -301, "クーポン", AdjustmentRule("equal")),
            Adjustment("s", "service_charge", 151, "サービス料", AdjustmentRule("members", member_ids=("A", "C"))),
        ],
        receipt_total_minor=1350,
    )
    r = compute_receipt(spec, "e")
    assert r.ok, r.issues
    assert sum(r.member_totals.values()) == 1350
    assert sum(r.item_allocations["r"].values()) == -500


def test_proportional_with_negative_base_requires_explicit_rule():
    spec = ReceiptSpec(
        items=[
            Item("a", "x", 100, 1, None, ItemAllocation("equal", {"A": 1})),
            Item("r", "返品", -500, 1, None, ItemAllocation("equal", {"B": 1})),
        ],
        adjustments=[Adjustment("t", "tax", 10, rule=AdjustmentRule("proportional"))],
        receipt_total_minor=-390,
    )
    assert "ADJUSTMENT_RULE_INVALID" in [i.code for i in compute_receipt(spec, "e").issues]


# ---- ランダム生成の大量ケースで不変条件を検証


@st.composite
def receipts(draw):
    members = draw(st.lists(st.sampled_from(MEMBERS), min_size=1, max_size=6, unique=True))
    n_items = draw(st.integers(1, 25))
    items = []
    for i in range(n_items):
        qty = draw(st.integers(1, 12))
        line = draw(st.integers(-20_000, 200_000))
        mode = draw(st.sampled_from(["equal", "weight", "units", "exact"]))
        who = draw(st.lists(st.sampled_from(members), min_size=1, unique=True))
        if mode == "equal":
            shares = {m: 1 for m in who}
        elif mode == "weight":
            shares = {m: draw(st.integers(1, 9)) for m in who}
        elif mode == "units":
            # qty を who に分割(0 個の人がいてもよい)
            cuts = sorted(draw(st.lists(st.integers(0, qty), min_size=len(who) - 1, max_size=len(who) - 1)))
            parts = [b - a for a, b in zip([0, *cuts], [*cuts, qty], strict=True)]
            shares = dict(zip(who, parts, strict=True))
        else:
            cuts = sorted(draw(st.lists(st.integers(min(0, line), max(0, line)), min_size=len(who) - 1,
                                        max_size=len(who) - 1)))
            lo = min(0, line)
            hi = max(0, line)
            pts = [lo, *cuts, hi]
            parts = [b - a for a, b in zip(pts, pts[1:], strict=False)]
            if line < 0:
                parts = [-p for p in parts]
            shares = dict(zip(who, parts, strict=True))
        tax_group = draw(st.sampled_from([None, "8", "10"]))
        items.append(Item(f"i{i}", f"item{i}", line, qty, tax_group, ItemAllocation(mode, shares)))
    adjustments = []
    for j in range(draw(st.integers(0, 4))):
        kind = draw(st.sampled_from(["tax", "discount", "coupon", "service_charge", "other"]))
        amt = draw(st.integers(0, 30_000))
        if kind in ("discount", "coupon"):
            amt = -amt
        elif kind == "other":
            amt = draw(st.integers(-30_000, 30_000))
        rule_type = draw(st.sampled_from(["equal", "members", "exact"]))
        if rule_type == "members":
            who = draw(st.lists(st.sampled_from(members), min_size=1, unique=True))
            rule = AdjustmentRule("members", member_ids=tuple(who))
        elif rule_type == "exact":
            first = draw(st.integers(-abs(amt) - 1, abs(amt) + 1))
            rule = AdjustmentRule("exact", amounts={members[0]: first, members[-1]: amt - first}
                                  if len(members) > 1 else {members[0]: amt})
        else:
            rule = AdjustmentRule("equal")
        adjustments.append(Adjustment(f"a{j}", kind, amt, rule=rule))
    total = sum(i.line_minor for i in items) + sum(a.amount_minor for a in adjustments)
    return ReceiptSpec(items, adjustments, total), members


@settings(max_examples=1500, deadline=None)
@given(rm=receipts(), seed=st.text(max_size=6))
def test_receipt_invariants_random(rm, seed):
    spec, members = rm
    r = compute_receipt(spec, seed)
    assert r.ok, r.issues
    for it in spec.items:
        assert sum(r.item_allocations[it.id].values()) == it.line_minor
    for a in spec.adjustments:
        assert sum(r.adjustment_allocations[a.id].values()) == a.amount_minor
    assert sum(r.member_totals.values()) == spec.receipt_total_minor


@settings(max_examples=300, deadline=None)
@given(rm=receipts(), delta=st.integers(-1000, 1000))
def test_receipt_any_mismatch_detected(rm, delta):
    assume(delta != 0)
    spec, _ = rm
    bad = ReceiptSpec(spec.items, spec.adjustments, spec.receipt_total_minor + delta)
    r = compute_receipt(bad, "s")
    assert "RECEIPT_TOTAL_MISMATCH" in [i.code for i in r.issues]
    with pytest.raises(MoneyError):
        r.require_valid()


# ---------------------------------------------------------------- 支出全体


def test_expense_multi_payer_itemized():
    receipt = ReceiptSpec([Item("a", "x", 3000, 1, None, ItemAllocation("equal", {"A": 1, "B": 1, "C": 1}))], [], 3000)
    r = compute_expense(ExpenseSpec(3000, {"A": 2000, "B": 1000}, "itemized", receipt=receipt), "e", {"A", "B", "C"})
    assert r.ok and r.shares == {"A": 1000, "B": 1000, "C": 1000}


def test_expense_rejects_unknown_member():
    with pytest.raises(MoneyError) as e:
        compute_expense(ExpenseSpec(100, {"Z": 100}, "equal", {"A": 1}), "e", {"A"})
    assert e.value.code == "UNKNOWN_MEMBER"


def test_expense_payer_mismatch_blocks():
    r = compute_expense(ExpenseSpec(100, {"A": 90}, "equal", {"A": 1}), "e", {"A"})
    assert not r.ok
    with pytest.raises(MoneyError) as e:
        r.require_valid()
    assert e.value.code == "PAYER_SUM_MISMATCH"


# ---- 独立オラクル(tests/stress/support.py)との一致: 合計だけでなく「誰が何円か」まで照合する


def test_hand_computed_proportional_tax_with_one_yen_remainder():
    """手計算の例。税 291 円を負担額 1400:400:1100 で比例配分すると理想値は 140.48 / 40.14 / 110.38。
    整数部 290 円、残り 1 円は小数部の最も大きい A へ。"""
    spec = ReceiptSpec(
        items=[
            Item("a", "唐揚げ", 1200, 1, "10", ItemAllocation("equal", {"A": 1, "B": 1, "C": 1})),
            Item("b", "刺身", 1700, 1, "10", ItemAllocation("exact", {"A": 1000, "C": 700})),
        ],
        adjustments=[Adjustment("t", "tax", 291, "消費税10%", AdjustmentRule("proportional", scope_tax_group="10"))],
        receipt_total_minor=3191,
    )
    r = compute_receipt(spec, "hand")
    assert r.ok, r.issues
    assert r.adjustment_allocations["t"] == {"A": 141, "B": 40, "C": 110}
    assert r.member_totals == {"A": 1541, "B": 440, "C": 1210}


@settings(max_examples=2000, deadline=None)
@given(
    total=st.integers(-50_000, 50_000),
    members=st.lists(st.sampled_from(MEMBERS), min_size=1, unique=True),
    mode=st.sampled_from(["equal", "weight", "units", "percentage"]),
    data=st.data(),
    seed=st.text(max_size=4),
)
def test_split_simple_matches_fraction_oracle(total, members, mode, data, seed):
    if mode == "equal":
        shares = dict.fromkeys(members, 1)
        weights = shares
    elif mode == "percentage":
        cuts = sorted(data.draw(st.lists(st.integers(0, 10_000), min_size=len(members) - 1,
                                         max_size=len(members) - 1)))
        shares = dict(zip(members, [b - a for a, b in zip([0, *cuts], [*cuts, 10_000], strict=True)], strict=True))
        weights = shares
    else:
        shares = {m: data.draw(st.integers(0, 7)) for m in members}
        assume(sum(shares.values()) > 0)
        weights = shares
    got = split_simple(total, mode, shares, seed)
    assert got == oracle_apportion(total, weights, seed)
    assert hamilton_violations(got, total, weights) == []


@st.composite
def scoped_receipts(draw):
    """比例配分(全商品・税率グループ・商品指定)を含む、正しいレシート。基準額が負にならないよう商品は正の金額。"""
    members = draw(st.lists(st.sampled_from(MEMBERS), min_size=1, max_size=6, unique=True))
    items = []
    for i in range(draw(st.integers(1, 12))):
        qty = draw(st.integers(1, 6))
        line = draw(st.integers(1, 30_000))
        who = draw(st.lists(st.sampled_from(members), min_size=1, unique=True))
        mode = draw(st.sampled_from(["equal", "weight", "units"]))
        if mode == "equal":
            shares = dict.fromkeys(who, 1)
        elif mode == "weight":
            shares = {m: draw(st.integers(1, 5)) for m in who}
        else:
            shares = dict.fromkeys(who, 0)
            for k in range(qty):
                shares[who[k % len(who)]] += 1
        items.append(Item(f"i{i}", f"item{i}", line, qty, draw(st.sampled_from(["8", "10"])),
                          ItemAllocation(mode, shares)))
    adjustments = []
    for j in range(draw(st.integers(0, 5))):
        kind = draw(st.sampled_from(["tax", "service_charge", "discount", "coupon", "rounding"]))
        amt = draw(st.integers(0, 5_000))
        if kind in ("discount", "coupon"):
            amt = -amt
        elif kind == "rounding":
            amt = draw(st.integers(-3, 3))
        scope = draw(st.sampled_from(["all", "group", "items", "equal", "members"]))
        if scope == "group":
            g = draw(st.sampled_from(["8", "10"]))
            assume(any(it.tax_group == g for it in items))
            rule = AdjustmentRule("proportional", scope_tax_group=g)
        elif scope == "items":
            ids = draw(st.lists(st.sampled_from([it.id for it in items]), min_size=1, unique=True))
            rule = AdjustmentRule("proportional", scope_item_ids=tuple(ids))
        elif scope == "equal":
            rule = AdjustmentRule("equal")
        elif scope == "members":
            rule = AdjustmentRule("members", member_ids=tuple(draw(
                st.lists(st.sampled_from(members), min_size=1, unique=True))))
        else:
            rule = AdjustmentRule("proportional")
        adjustments.append(Adjustment(f"a{j}", kind, amt, rule=rule))
    total = sum(i.line_minor for i in items) + sum(a.amount_minor for a in adjustments)
    return ReceiptSpec(items, adjustments, total), members


@settings(max_examples=1500, deadline=None)
@given(rm=scoped_receipts(), seed=st.text(max_size=4))
def test_itemized_matches_independent_oracle(rm, seed):
    spec, members = rm
    r = compute_receipt(spec, seed)
    assert r.ok, r.issues
    want = oracle_receipt(spec, seed)
    assert {**r.item_allocations, **r.adjustment_allocations} == want["allocations"]
    assert r.member_totals == want["member_totals"]
    # 支出として登録したときの負担額は、0 円の人を除いた同じ値(プレビューと登録は同じ関数・同じ種)
    exp = compute_expense(
        ExpenseSpec(spec.receipt_total_minor, {members[0]: spec.receipt_total_minor}, "itemized", receipt=spec),
        seed, set(members))
    assert exp.ok and exp.receipt is not None and exp.receipt.ok
    assert exp.shares == {m: v for m, v in want["member_totals"].items() if v != 0}


@settings(max_examples=300, deadline=None)
@given(rm=scoped_receipts(), data=st.data())
def test_itemized_result_does_not_depend_on_line_order(rm, data):
    spec, _ = rm
    items = data.draw(st.permutations(list(spec.items)))
    adjs = data.draw(st.permutations(list(spec.adjustments)))
    a = compute_receipt(spec, "o")
    b = compute_receipt(ReceiptSpec(items, adjs, spec.receipt_total_minor), "o")
    assert a.member_totals == b.member_totals
    assert a.adjustment_allocations == b.adjustment_allocations


def test_adjustment_scoped_to_missing_item_is_reported_not_redistributed():
    """割引の対象に指定した商品が明細に無い(読み取り後に商品行を消した等)。黙って残りの商品の負担者へ
    配り直さず、問題として返す。全部が無い場合はすでに ADJUSTMENT_RULE_INVALID になる。"""
    spec = ReceiptSpec(
        items=[
            Item("ramen", "ラーメン", 900, 1, None, ItemAllocation("equal", {"A": 1})),
            Item("gyoza", "餃子", 400, 1, None, ItemAllocation("equal", {"B": 1})),
        ],
        adjustments=[Adjustment("d", "discount", -100, "餃子100円引き",
                                AdjustmentRule("proportional", scope_item_ids=("gyoza_deleted", "ramen")))],
        receipt_total_minor=1200,
    )
    r = compute_receipt(spec, "s")
    issue = next((i for i in r.issues if i.code == "ADJUSTMENT_RULE_INVALID"), None)
    assert issue is not None, r.adjustment_allocations
    assert issue.target == "d" and issue.detail["items"] == ["gyoza_deleted"]
    assert "d" not in r.adjustment_allocations  # 配り直した結果を返さない


def test_equal_adjustment_skips_members_with_zero_base_by_hand():
    """手計算の例。サービス料 100 円を「対象商品の負担者で均等」に配る。数量 0 個の C は負担者ではないので除き、
    A・B で 50 円ずつ。"""
    spec = ReceiptSpec(
        items=[Item("beer", "ビール", 1000, 2, None, ItemAllocation("units", {"A": 1, "B": 1, "C": 0}))],
        adjustments=[Adjustment("s", "service_charge", 100, "サービス料", AdjustmentRule("equal"))],
        receipt_total_minor=1100,
    )
    r = compute_receipt(spec, "hand")
    assert r.ok, r.issues
    assert r.adjustment_allocations["s"] == {"A": 50, "B": 50}
