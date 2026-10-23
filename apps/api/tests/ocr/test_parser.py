from app.ocr.parser import PARSER_VERSION, find_amount, parse_receipt
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


# ---------------------------------------------------------------- P10: 静かな誤り(商品の欠落・ポイント・途中の合計)
# 修正前のパーサー(rule-ja-0.1.0)で失敗することを確認してから修正した


def items3(d):
    return [(i.name, i.quantity, i.line_minor) for i in d.items]


def codes(d):
    return {w["code"] for w in d.warnings}


def test_intermediate_total_does_not_drop_later_items():
    # A300 → 途中の「合計 300」→ B200 → 最終の「合計 500」。以前は B を黙って捨て、差額 0 で警告なしだった
    d = parse_receipt(tokens_from_lines([
        "スーパー", ("商品A", "¥300"), ("合計", "¥300"), ("商品B", "¥200"), ("合計", "¥500"),
        ("お預り", "¥1,000"), ("お釣り", "¥500"),
    ]))
    assert items3(d) == [("商品A", 1, 300), ("商品B", 1, 200)]
    assert d.total_minor == 500 and d.difference_minor == 0
    assert "MULTIPLE_TOTALS" in codes(d)  # 候補が複数あったことは必ず知らせる
    assert [c["value"] for c in d.total_candidates] == [300, 500]


def test_total_candidates_after_payment_are_not_used():
    # 支払(お預り)より後の「合計」らしい行(ポイント合計など)は最終合計にしない
    d = parse_receipt(tokens_from_lines([
        "店", ("商品A", "¥1,000"), ("合計", "¥1,000"), ("お預り", "¥1,000"), ("お釣り", "¥0"),
        ("累計ポイント合計", "1,234"),
    ]))
    assert d.total_minor == 1000 and d.difference_minor == 0 and "MULTIPLE_TOTALS" not in codes(d)


def test_conflicting_totals_are_flagged_not_corrected():
    # 合計が 2 つあり、どちらとも明細が一致しない → 数値を補正せず、要確認
    d = parse_receipt(tokens_from_lines([
        "店", ("商品A", "¥300"), ("商品B", "¥200"), ("合計", "¥450"), ("ご請求額", "¥480"),
    ]))
    assert d.total_minor == 480 and d.difference_minor == -20
    assert {"MULTIPLE_TOTALS", "TOTAL_MISMATCH"} <= codes(d)
    assert [i.line_minor for i in d.items] == [300, 200]  # 商品の金額は書き換えない


def test_item_after_final_total_is_reported():
    d = parse_receipt(tokens_from_lines([
        "店", ("商品A", "¥500"), ("合計", "¥500"), ("お預り", "¥1,000"), ("商品Z", "¥300"),
    ]))
    assert items3(d) == [("商品A", 1, 500)]
    assert "ITEM_AFTER_TOTAL" in codes(d)


def test_printed_item_count_detects_missing_items_even_when_total_matches():
    # 合計が一致していても、印字された点数と読み取った数量が違えば知らせる(合計一致は十分条件ではない)
    d = parse_receipt(tokens_from_lines([
        "店", ("商品A", "¥300"), ("商品B", "¥200"), ("お買上点数", "3点"), ("合計", "¥500"),
    ]))
    assert d.difference_minor == 0
    assert d.item_count_printed == 3 and "ITEM_COUNT_MISMATCH" in codes(d)
    ok = parse_receipt(tokens_from_lines([
        "店", ("商品A 2個", "¥600"), ("お買上点数", "2点"), ("合計", "¥600"),
    ]))
    assert "ITEM_COUNT_MISMATCH" not in codes(ok)


def test_points_earned_and_balance_are_ignored():
    d = parse_receipt(tokens_from_lines([
        "店", ("商品A", "¥1,000"), ("合計", "¥1,000"), ("現金", "¥1,000"), ("今回付与ポイント", "10"),
        ("ポイント残高", "1,234"),
    ]))
    assert d.adjustments == [] and "POINT_USAGE_AMBIGUOUS" not in codes(d) and d.difference_minor == 0


def test_points_used_before_total_become_reviewable_discount():
    # 合計の前に引かれたポイント利用: 割引として下書きに入れるが、必ず要確認(誰の負担を減らすかは利用者が決める)
    d = parse_receipt(tokens_from_lines([
        "店", ("商品A", "¥1,000"), ("ポイント利用", "-100"), ("合計", "¥900"),
    ]))
    assert [(a.kind, a.amount_minor, a.needs_review) for a in d.adjustments] == [("discount", -100, True)]
    assert "POINT_USAGE" in d.adjustments[0].reasons
    assert d.difference_minor == 0 and "POINT_USAGE_AMBIGUOUS" in codes(d)


def test_points_used_as_payment_after_total_do_not_change_amounts():
    # 合計の後のポイント払い(支払手段): 割り勘の金額は変えず、立替額の扱いを確認してもらう
    d = parse_receipt(tokens_from_lines([
        "店", ("商品A", "¥1,000"), ("合計", "¥1,000"), ("ポイント充当", "¥300"), ("現金", "¥700"),
    ]))
    assert d.adjustments == [] and d.total_minor == 1000 and d.difference_minor == 0
    assert "POINT_USAGE_AMBIGUOUS" in codes(d)


def test_refund_line_is_reviewable_negative_adjustment():
    d = parse_receipt(tokens_from_lines([
        "店", ("商品A", "¥800"), ("返品 商品B", "-¥200"), ("合計", "¥600"),
    ]))
    assert items3(d) == [("商品A", 1, 800)]
    (a,) = d.adjustments
    assert a.kind == "discount" and a.amount_minor == -200 and a.needs_review and "REFUND" in a.reasons


def test_intermediate_subtotal_uses_last_subtotal_before_total():
    d = parse_receipt(tokens_from_lines([
        "店", ("商品A", "¥300"), ("小計", "¥300"), ("商品B", "¥200"), ("小計", "¥500"),
        ("消費税(10%)", "¥50"), ("合計", "¥550"),
    ]))
    assert items3(d) == [("商品A", 1, 300), ("商品B", 1, 200)]
    assert d.subtotal_minor == 500 and d.tax_mode == "exclusive" and d.difference_minor == 0
    assert "SUBTOTAL_MISMATCH" not in codes(d)


def test_parser_version_bumped_and_existing_keys_kept():
    d = parse_receipt(tokens_from_lines(["店", ("商品A", "¥100"), ("合計", "¥100")])).to_json()
    assert PARSER_VERSION != "rule-ja-0.1.0"
    for k in ("parser_version", "store_name", "purchased_on", "items", "adjustments", "subtotal_minor", "total_minor",
              "tax_mode", "difference_minor", "warnings", "lines"):
        assert k in d
    assert {"id", "name", "quantity", "line_minor", "tax_group", "confidence", "needs_review", "reasons", "bbox",
            "source_lines"} <= set(d["items"][0])
