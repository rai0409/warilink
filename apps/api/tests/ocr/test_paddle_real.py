"""本物の PaddleOCR で合成レシートを読み取る結合テスト(`make test-ocr`)。

合成画像での結果は実レシートの精度を示さない。モデルの読み込みとパイプライン全体が動くことの確認。
"""

import json
import random

import pytest

from app.ocr.evaluate import score_sample
from app.ocr.synth import find_font, make_sample, render
from app.ocr.worker import run_pipeline

pytestmark = pytest.mark.ocr


@pytest.fixture(scope="module")
def adapter():
    from app.ocr.paddle_adapter import PaddleOcrAdapter

    return PaddleOcrAdapter()


def test_recognizes_synthetic_receipts_to_json(adapter, tmp_path):
    font = find_font()
    rng = random.Random(2026)
    exact = 0
    for n in range(5):
        sample = make_sample(rng)
        path = tmp_path / f"{n}.jpg"
        render(sample, font, rng, "easy").save(path)
        out = run_pipeline(adapter, path.read_bytes(), low_confidence=0.9)
        json.dumps(out, ensure_ascii=False)  # JSON として出力できる
        assert out["tokens"] and all(0 <= t["score"] <= 1 for t in out["tokens"])
        exact += score_sample(out["draft"], sample.label)["exact"]
    info = adapter.model_info()
    assert info.model_name == "PP-OCRv6_medium_det+PP-OCRv6_medium_rec"
    assert all(info.params["weights_sha256_16"].values())
    assert exact >= 4, f"合成の簡単な画像 5 枚中 {exact} 枚しか完全一致しなかった"
