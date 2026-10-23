"""画像の前処理と品質チェック(OpenCV)。OCR ワーカー側でのみ使う。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

MAX_SIDE = 2000  # これより大きい画像は縮小(認識精度と処理時間のバランス。要実測調整)
MIN_SHORT_SIDE = 300


@dataclass
class Prepared:
    image: np.ndarray  # BGR uint8
    scale: float  # 元画像 → 前処理後の倍率
    quality: dict[str, Any] = field(default_factory=dict)
    warnings: list[dict[str, str]] = field(default_factory=list)
    rejected: bool = False


def decode(data: bytes) -> np.ndarray | None:
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return img if img is not None and img.size else None


def quality_metrics(gray: np.ndarray) -> dict[str, float]:
    return {
        "brightness": float(gray.mean()),
        "contrast": float(gray.std()),
        "sharpness": float(cv2.Laplacian(gray, cv2.CV_64F).var()),
    }


# しきい値は合成画像で決めた暫定値。実レシートでの調整はユーザーの評価で行う(docs/OCR_PIPELINE.md)
# 反射(白飛び)の検出は白い紙と区別できず誤検出するため未実装
THRESHOLDS = {"dark": 70.0, "low_contrast": 25.0, "blurry": 60.0}


def prepare(img: np.ndarray) -> Prepared:
    h, w = img.shape[:2]
    if min(h, w) < MIN_SHORT_SIDE:
        return Prepared(img, 1.0, {"width": w, "height": h},
                        [{"code": "TOO_SMALL", "message": "画像が小さすぎます。レシート全体を大きく写してください"}],
                        rejected=True)
    scale = min(1.0, MAX_SIDE / max(h, w))
    if scale < 1.0:
        img = cv2.resize(img, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    q: dict[str, Any] = {"width": w, "height": h, **quality_metrics(gray)}
    warnings: list[dict[str, str]] = []
    if q["brightness"] < THRESHOLDS["dark"]:
        warnings.append({"code": "DARK", "message": "画像が暗いため、読み取り精度が下がる可能性があります"})
    if q["contrast"] < THRESHOLDS["low_contrast"]:
        warnings.append({"code": "LOW_CONTRAST", "message": "文字と背景の差が小さく、読み取りにくい画像です"})
    if q["sharpness"] < THRESHOLDS["blurry"]:
        warnings.append({"code": "BLURRY",
                         "message": "画像がぼやけています。ピントを合わせて撮り直すと精度が上がります"})
    return Prepared(img, scale, q, warnings)
