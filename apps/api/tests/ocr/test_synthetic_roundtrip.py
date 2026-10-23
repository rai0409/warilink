"""合成レシートの文字列をそのまま(誤認識なしで)トークンにしたとき、パーサーが正解ラベルを復元できること。

認識誤りを含まない上限の確認であり、実レシートの精度ではない。
"""

import random

from hypothesis import given, settings
from hypothesis import strategies as st

from app.ocr.evaluate import score_sample
from app.ocr.parser import parse_receipt
from app.ocr.synth import make_sample

from .helpers import tokens_from_lines


@settings(max_examples=300, deadline=None)
@given(st.integers(min_value=0, max_value=10**9))
def test_parser_recovers_synthetic_labels(seed):
    sample = make_sample(random.Random(seed))
    draft = parse_receipt(tokens_from_lines(sample.lines)).to_json()
    r = score_sample(draft, sample.label)
    assert r["exact"], (sample.lines, draft["items"], draft["adjustments"], draft["total_minor"])
    assert draft["difference_minor"] == 0


@settings(max_examples=200, deadline=None)
@given(st.lists(st.integers(min_value=1, max_value=9_999), min_size=2, max_size=8), st.data())
def test_intermediate_total_never_drops_items(prices, data):
    """途中に「合計」(その時点までの和)が入っても、すべての商品を読み、最終合計を使う(P10 の静かな誤りの性質テスト)。"""
    cut = data.draw(st.integers(min_value=1, max_value=len(prices) - 1))
    lines: list = ["店"]
    for i, p in enumerate(prices):
        lines.append((f"商品{chr(0x30A2 + i * 2)}", f"¥{p:,}"))
        if i + 1 == cut:
            lines.append(("合計", f"¥{sum(prices[:cut]):,}"))
    lines += [("合計", f"¥{sum(prices):,}"), ("お預り", f"¥{sum(prices) + 1000:,}")]
    d = parse_receipt(tokens_from_lines(lines))
    assert [i.line_minor for i in d.items] == prices
    assert d.total_minor == sum(prices) and d.difference_minor == 0
    assert any(w["code"] == "MULTIPLE_TOTALS" for w in d.warnings)


@settings(max_examples=200, deadline=None)
@given(st.lists(st.integers(min_value=1, max_value=9_999), min_size=2, max_size=8), st.data())
def test_dropping_an_item_line_is_never_silent(prices, data):
    """1 行を読み落とした(OCR が検出しなかった)場合、合計の差額か点数の不一致で必ず知らせる。"""
    miss = data.draw(st.integers(min_value=0, max_value=len(prices) - 1))
    lines: list = ["店"] + [(f"商品{chr(0x30A2 + i * 2)}", f"¥{p:,}") for i, p in enumerate(prices) if i != miss]
    lines += [("お買上点数", f"{len(prices)}点"), ("合計", f"¥{sum(prices):,}")]
    d = parse_receipt(tokens_from_lines(lines))
    codes = {w["code"] for w in d.warnings}
    assert {"TOTAL_MISMATCH", "ITEM_COUNT_MISMATCH"} <= codes
