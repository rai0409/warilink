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
