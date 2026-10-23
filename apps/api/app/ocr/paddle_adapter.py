"""PaddleOCR 3.x のアダプター。重い import はインスタンス生成時まで遅延させる。"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any

import numpy as np

from app.ocr.tokens import ModelInfo, OcrToken


def _default_threads() -> int:
    # PaddleX の既定(10)が CPU 数を超えると linux/aarch64 で推論中に SIGSEGV する(2026-10-19 実機確認)
    return max(1, min(4, os.cpu_count() or 1))


class PaddleOcrAdapter:
    def __init__(self, *, det_model: str = "PP-OCRv6_medium_det",
                 rec_model: str = "PP-OCRv6_medium_rec",
                 cpu_threads: int | None = None, enable_mkldnn: bool = False) -> None:
        from paddleocr import PaddleOCR  # noqa: PLC0415  (ワーカー専用の重い依存)

        self.params: dict[str, Any] = {
            "cpu_threads": cpu_threads or _default_threads(), "enable_mkldnn": enable_mkldnn,
            # レシートは撮影者が向きを合わせる前提。文書の傾き補正は Phase 3 では無効(要評価)
            "use_doc_orientation_classify": False, "use_doc_unwarping": False, "use_textline_orientation": False,
        }
        # モデルは名前で固定する(lang の既定が将来変わっても結果が変わらないように)
        self.det_model, self.rec_model = det_model, rec_model
        self.params["text_detection_model_name"] = det_model
        self.params["text_recognition_model_name"] = rec_model
        t = time.perf_counter()
        self._ocr = PaddleOCR(**self.params)
        self.init_ms = (time.perf_counter() - t) * 1000
        self._info = self._collect_info()

    def _collect_info(self) -> ModelInfo:
        import paddle  # noqa: PLC0415
        import paddleocr  # noqa: PLC0415

        # 実際に使ったモデル名と重みファイルのハッシュを記録する(同名モデルの差し替えを検出するため)
        cache = Path(os.environ.get("PADDLE_PDX_CACHE_HOME", Path.home() / ".paddlex")) / "official_models"
        hashes = {}
        for name in (self.det_model, self.rec_model):
            p = cache / name / "inference.pdiparams"
            hashes[name] = hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p.exists() else None
        return ModelInfo(
            adapter="paddleocr",
            model_name=f"{self.det_model}+{self.rec_model}",
            model_version=f"paddleocr={paddleocr.__version__};paddle={paddle.__version__}",
            params={**self.params, "weights_sha256_16": hashes},
        )

    def model_info(self) -> ModelInfo:
        return self._info

    def recognize(self, image: np.ndarray) -> list[OcrToken]:
        out: list[OcrToken] = []
        for res in self._ocr.predict(image):
            j = res.json.get("res", res.json)
            for text, score, box in zip(j["rec_texts"], j["rec_scores"], j["rec_boxes"], strict=True):
                if not str(text).strip():
                    continue
                x0, y0, x1, y1 = (int(v) for v in box)
                out.append(OcrToken(str(text), float(score), (x0, y0, x1, y1)))
        return out
