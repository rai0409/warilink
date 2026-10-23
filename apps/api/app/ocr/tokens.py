"""OCR エンジンに依存しない入出力の型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:  # API コンテナには numpy/OpenCV を入れない
    import numpy as np


@dataclass(frozen=True)
class OcrToken:
    text: str
    score: float  # 0..1 認識の確信度(エンジンの値。正しさの保証ではない)
    bbox: tuple[int, int, int, int]  # x0, y0, x1, y1(前処理後の画像座標)

    def to_json(self) -> dict[str, Any]:
        return {"text": self.text, "score": round(self.score, 4), "bbox": list(self.bbox)}

    @staticmethod
    def from_json(d: dict[str, Any]) -> OcrToken:
        x0, y0, x1, y1 = (int(v) for v in d["bbox"])
        return OcrToken(str(d["text"]), float(d["score"]), (x0, y0, x1, y1))


@dataclass
class ModelInfo:
    adapter: str
    model_name: str
    model_version: str
    params: dict[str, Any] = field(default_factory=dict)


class OcrAdapter(Protocol):
    """OCR エンジンの交換点。PaddleOCR 以外(将来の別モデル)も同じ形で差し替える。"""

    def recognize(self, image: np.ndarray) -> list[OcrToken]:  # BGR uint8
        ...

    def model_info(self) -> ModelInfo:
        ...
