from app.ocr.parser import find_amount, parse_receipt
from app.ocr.tokens import OcrToken

from .helpers import tokens_from_lines


def items_of(d):
    return [(i.name, i.quantity, i.line_minor, i.tax_group) for i in d.items]


def test_exclusive_tax_with_qty_merged_into_amount():
    # PaddleOCR が「2」と「¥1,300」を 1 トークン「2¥1,300」に連結した実例(合成画像での観測)
    d = parse_receipt(tokens_from_lines([
        "居酒屋 WARI", "2026年10月16日19:42", ("生ビール", "2¥1,300"), ("唐揚げ", "¥800"), ("サラダ", "¥600"),
        ("小計", "¥2,700"), ("消費税(10%)", "¥270"), ("合計", "¥2,970"), ("お預り", "¥3,000"), ("お釣り", "¥30"),
    ]))
    assert d.store_name == "居酒屋 WARI" and d.purchased_on == "2026-10-16"
    assert items_of(d) == [("生ビール", 2, 1300, None), ("唐揚げ", 1, 800, None), ("サラダ", 1, 600, None)]
    assert [(a.kind, a.amount_minor, a.tax_group) for a in d.adjustments] == [("tax", 270, "10")]
    assert d.total_minor == 2970 and d.tax_mode == "exclusive" and d.difference_minor == 0
    assert d.warnings == []


def test_two_line_quantity_and_unit_price():
    d = parse_receipt(tokens_from_lines([
        "カフェ", ("ブレンドコーヒー", ""), ("@450 x2", "¥900"), ("チーズケーキ", "¥500"), ("合計", "¥1,400"),
    ]))
    assert items_of(d) == [("ブレンドコーヒー", 2, 900, None), ("チーズケーキ", 1, 500, None)]
    assert d.items[0].unit_price_minor == 450 and not d.items[0].needs_review


def test_unit_price_inconsistency_needs_review():
    d = parse_receipt(tokens_from_lines(["店", ("焼き鳥", ""), ("@200 x3", "¥500"), ("合計", "¥500")]))
    assert d.items[0].needs_review and "QTY_PRICE_INCONSISTENT" in d.items[0].reasons


def test_reduced_tax_marks_split_8_and_10():
    d = parse_receipt(tokens_from_lines([
        "スーパー", ("牛乳※", "¥200"), ("洗剤", "¥400"), ("小計", "¥600"),
        ("消費税(8%)", "¥16"), ("消費税(10%)", "¥40"), ("合計", "¥656"), "※は軽減税率対象商品",
    ]))
    assert items_of(d) == [("牛乳", 1, 200, "8"), ("洗剤", 1, 400, "10")]
    assert [(a.amount_minor, a.tax_group) for a in d.adjustments] == [(16, "8"), (40, "10")]
    assert d.difference_minor == 0


def test_inclusive_tax_is_not_added_twice():
    d = parse_receipt(tokens_from_lines([
        "ベーカリー", ("クロワッサン 2個", "¥560"), ("食パン", "¥300"), ("合計", "¥860"), "(内消費税等 ¥78)",
    ]))
    assert d.tax_mode == "inclusive" and d.adjustments == [] and d.difference_minor == 0
    assert items_of(d)[0] == ("クロワッサン", 2, 560, None)


def test_discount_after_subtotal_is_global_and_right_after_item_is_scoped():
    d = parse_receipt(tokens_from_lines([
        "店", ("おにぎり", "¥150"), ("値引", "-¥30"), ("緑茶", "¥140"), ("小計", "¥260"), ("クーポン", "¥50"),
        ("合計", "¥210"),
    ]))
    by_kind = {a.kind: a for a in d.adjustments}
    assert by_kind["discount"].amount_minor == -30 and by_kind["discount"].scope_item_ids == ["ocr-i1"]
    assert by_kind["coupon"].amount_minor == -50 and by_kind["coupon"].scope_item_ids is None
    assert d.difference_minor == 0


def test_mismatch_is_reported_not_corrected():
    d = parse_receipt(tokens_from_lines(["店", ("A", "¥1,000"), ("B", "¥500"), ("合計", "¥1,600")]))
    assert d.difference_minor == 100
    assert [w["code"] for w in d.warnings] == ["TOTAL_MISMATCH"]
    assert sum(i.line_minor for i in d.items) == 1500  # 勝手に補正しない


def test_total_not_found():
    d = parse_receipt(tokens_from_lines(["店", ("A", "¥1,000")]))
    assert d.total_minor is None and any(w["code"] == "TOTAL_NOT_FOUND" for w in d.warnings)


def test_long_vowel_is_not_a_minus_sign_and_time_is_not_an_item():
    d = parse_receipt(tokens_from_lines(["カフェ", "14:05", ("コーヒー", "400"), ("合計", "¥400")]))
    assert items_of(d) == [("コーヒー", 1, 400, None)]


def test_split_keyword_and_amount_lines_in_either_order():
    toks = tokens_from_lines(["店", ("A", "¥1,000"), ("B", "¥780")])
    # 傾きで「¥1,780」が「小計」より上の行になったケース
    toks += [OcrToken("¥1,780", 0.99, (400, 130, 480, 160)), OcrToken("小計", 0.99, (20, 165, 60, 195)),
             OcrToken("消費税(10%)", 0.99, (20, 210, 160, 240)), OcrToken("¥178", 0.99, (400, 210, 460, 240)),
             OcrToken("合計", 0.99, (20, 250, 60, 280)), OcrToken("¥1,958", 0.99, (400, 285, 480, 315))]
    d = parse_receipt(toks)
    assert items_of(d) == [("A", 1, 1000, None), ("B", 1, 780, None)]
    assert d.subtotal_minor == 1780 and d.total_minor == 1958 and d.difference_minor == 0


def test_low_confidence_flags_review():
    d = parse_receipt(tokens_from_lines(["店", ("唐揚げ", "¥800"), ("合計", "¥800")], score=0.6))
    assert d.items[0].needs_review and "LOW_CONFIDENCE" in d.items[0].reasons
    assert any(w["code"] == "ITEMS_NEED_REVIEW" for w in d.warnings)


def test_lines_after_total_are_ignored():
    d = parse_receipt(tokens_from_lines(["店", ("A", "¥500"), ("合計", "¥500"), ("ポイント残高", "1,234"),
                                         ("PayPay", "¥500")]))
    assert items_of(d) == [("A", 1, 500, None)]


def test_find_amount_ignores_long_bare_numbers():
    assert find_amount("TEL 0312345678") is None
    assert find_amount("¥1,300").value == 1300
    assert find_amount("▲100").value == -100
