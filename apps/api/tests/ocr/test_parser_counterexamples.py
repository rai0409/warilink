"""PR D: 合計が合っていても正答とみなさない・確信できない税区分を黙って確定しない、ための反例。"""

from fractions import Fraction

from hypothesis import given, settings
from hypothesis import strategies as st

from app.ocr.evaluate import score_sample
from app.ocr.parser import parse_receipt

from .helpers import tokens_from_lines


def groups(d):
    return [(i.name, i.tax_group) for i in d.items]


def codes(d):
    return {w["code"] for w in d.warnings}


def test_one_missing_mark_is_recovered_from_printed_taxes():
    # 凡例あり・8% の 2 商品のうち 1 つだけ「※」を読み落とした(旧: 牛乳を黙って 10% にしていた)
    d = parse_receipt(tokens_from_lines([
        "店", ("パン※", "¥200"), ("牛乳", "¥100"), ("電池", "¥500"), ("小計", "¥800"),
        ("消費税(8%)", "¥24"), ("消費税(10%)", "¥50"), ("合計", "¥874"), "※は軽減税率対象商品",
    ]))
    assert groups(d) == [("パン", "8"), ("牛乳", "8"), ("電池", "10")]
    assert "TAX_GROUP_INFERRED" in codes(d) and all(i.needs_review for i in d.items)


def test_ambiguous_missing_marks_are_left_for_the_user():
    # 同じ金額の商品があり、どれが 8% か 1 通りに決まらない → 記号の無い商品の税率は空にして要確認
    d = parse_receipt(tokens_from_lines([
        "店", ("パン※", "¥100"), ("牛乳", "¥100"), ("卵", "¥100"), ("小計", "¥300"),
        ("消費税(8%)", "¥16"), ("消費税(10%)", "¥10"), ("合計", "¥326"), "※は軽減税率対象商品",
    ]))
    assert groups(d) == [("パン", "8"), ("牛乳", None), ("卵", None)]
    assert "TAX_GROUP_MISMATCH" in codes(d)
    assert all("TAX_GROUP_UNVERIFIED" in i.reasons for i in d.items[1:])


def test_legend_without_tax_breakdown_is_an_assumption_and_says_so():
    d = parse_receipt(tokens_from_lines([
        "店", ("パン※", "¥108"), ("電池", "¥330"), ("合計", "¥438"), "(内消費税等 ¥38)", "※は軽減税率対象商品",
    ]))
    assert groups(d) == [("パン", "8"), ("電池", "10")]
    assert "TAX_GROUP_ASSUMED" in codes(d)


def test_consistent_marks_need_no_warning():
    d = parse_receipt(tokens_from_lines([
        "店", ("パン※", "¥200"), ("電池", "¥500"), ("小計", "¥700"), ("消費税(8%)", "¥16"),
        ("消費税(10%)", "¥50"), ("合計", "¥766"), "※は軽減税率対象商品",
    ]))
    assert groups(d) == [("パン", "8"), ("電池", "10")] and d.warnings == []


def test_bare_number_in_a_yen_receipt_is_not_silently_dropped():
    # ¥ 付きのレシートで 1 行だけ ¥ が読めなかった → 商品にはしないが、合計の不一致として必ず知らせる
    d = parse_receipt(tokens_from_lines([
        "居酒屋", "2026/10/25 22:07", "TEL 03-0000-0000", ("お通し", "300"), ("生ビール", "¥550"), ("合計", "¥850"),
    ]))
    assert [i.name for i in d.items] == ["生ビール"]
    assert "TOTAL_MISMATCH" in codes(d) and d.difference_minor == 300


def test_matching_total_with_a_missing_item_is_not_exact():
    label = {"items": [{"name": "A", "quantity": 1, "line_minor": 500},
                       {"name": "B", "quantity": 1, "line_minor": 300}], "total_minor": 800, "adjustments": []}
    draft = {"items": [{"name": "A", "quantity": 1, "line_minor": 800}], "total_minor": 800, "adjustments": []}
    r = score_sample(draft, label)
    assert r["total_ok"] and not r["exact"] and not r["structure_exact"] and r["matched_lines"] == 0


@settings(max_examples=3000, deadline=None)
@given(s8=st.integers(0, 10**9), tax=st.integers(0, 10**8), rate=st.sampled_from([8, 10]))
def test_integer_tax_comparison_matches_exact_arithmetic(s8, tax, rate):
    """整数の比較(|s8 × rate − tax × 100| < 100)は、有理数で計算した |s8 × rate / 100 − tax| < 1 と常に同じ。"""
    exact = abs(Fraction(s8 * rate, 100) - tax) < 1
    assert (abs(s8 * rate - tax * 100) < 100) == exact
