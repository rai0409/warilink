"""画像の前処理と品質チェック(OpenCV)。OCR ワーカー側でのみ使う。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

MAX_SIDE = 2000  # これより大きい画像は縮小(認識精度と処理時間のバランス。要実測調整)
MIN_SHORT_SIDE = 300
# 傾き補正(任意・既定は無効)。推定は縮小画像で、角度は有界の範囲だけを探す(処理量の上限)
DESKEW_MIN_DEG = 0.5  # これ未満は補正しない(補間でぼかさない)
DESKEW_MAX_DEG = 10.0  # これを超える傾きは推定の対象外(撮り直しを促す)
DESKEW_ESTIMATE_SIDE = 600
# 推定の信頼度(投影の分散のピーク / 全角度の中央値)。合成画像で、実際の傾き ±7° 以内は約 1.9 以上、
# 範囲外(25°・40°)は 1.25 以下だった。これ未満なら補正しない(誤補正を避ける。暫定値)
DESKEW_MIN_CONFIDENCE = 1.6


@dataclass
class Prepared:
    image: np.ndarray  # BGR uint8
    scale: float  # 元画像 → 前処理後の倍率
    quality: dict[str, Any] = field(default_factory=dict)
    warnings: list[dict[str, str]] = field(default_factory=list)
    rejected: bool = False
    # 前処理後の画像座標 → 「縮小だけした元画像」の座標への 2x3 アフィン変換。None なら恒等(回転なし)
    inverse: np.ndarray | None = None


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


def _projection_score(binary: np.ndarray, angle: float) -> float:
    h, w = binary.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    rot = cv2.warpAffine(binary, m, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)
    return float(np.var(rot.sum(axis=1, dtype=np.float64)))


def estimate_skew_with_confidence(gray: np.ndarray) -> tuple[float, float]:
    """文字の行が水平になる回転角(度、反時計回りが正)と信頼度。横方向の投影の分散が最大になる角度。

    範囲は ±DESKEW_MAX_DEG。粗く 0.5° 刻み → 最良の周辺を 0.1° 刻み(計 52 回の回転、縮小画像で行う)。
    """
    h, w = gray.shape
    f = min(1.0, DESKEW_ESTIMATE_SIDE / max(h, w))
    small = cv2.resize(gray, (max(1, round(w * f)), max(1, round(h * f))), interpolation=cv2.INTER_AREA)
    _, binary = cv2.threshold(small, 0, 1, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    coarse = np.arange(-DESKEW_MAX_DEG, DESKEW_MAX_DEG + 1e-9, 0.5)
    scores = [_projection_score(binary, float(a)) for a in coarse]
    best = float(coarse[int(np.argmax(scores))])
    median = float(np.median(scores)) or 1.0
    fine = np.arange(best - 0.5, best + 0.5 + 1e-9, 0.1)
    angle = round(float(max(fine, key=lambda a: _projection_score(binary, float(a)))), 2)
    return angle, round(max(scores) / median, 3)


def estimate_skew(gray: np.ndarray) -> float:
    return estimate_skew_with_confidence(gray)[0]


def rotate_bounded(img: np.ndarray, angle: float) -> tuple[np.ndarray, np.ndarray]:
    """画像を回転し(はみ出さないよう広げる)、1 辺が MAX_SIDE を超えないよう縮める。

    戻り値: (回転後の画像, 回転後の座標 → 回転前の座標 の 2x3 逆変換)。
    """
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    cos, sin = abs(m[0, 0]), abs(m[0, 1])
    nw, nh = int(round(h * sin + w * cos)), int(round(h * cos + w * sin))
    k = min(1.0, MAX_SIDE / max(nw, nh))
    m[0, 2] += nw / 2 - w / 2
    m[1, 2] += nh / 2 - h / 2
    m *= k
    out = cv2.warpAffine(img, m, (max(1, round(nw * k)), max(1, round(nh * k))), flags=cv2.INTER_LINEAR,
                         borderValue=(255, 255, 255))
    return out, cv2.invertAffineTransform(m)


def map_bbox(bbox: list[int] | tuple[int, int, int, int], inverse: np.ndarray, width: int,
             height: int) -> list[int]:
    """前処理後の座標の矩形を、元の向きの画像座標へ戻す(4 隅を変換して外接矩形。画像の範囲に収める)。"""
    x0, y0, x1, y1 = bbox
    pts = np.array([[x0, y0, 1], [x1, y0, 1], [x0, y1, 1], [x1, y1, 1]], dtype=np.float64) @ inverse.T
    xs, ys = pts[:, 0], pts[:, 1]
    return [int(max(0, np.floor(xs.min()))), int(max(0, np.floor(ys.min()))),
            int(min(width, np.ceil(xs.max()))), int(min(height, np.ceil(ys.max())))]


def prepare(img: np.ndarray, *, deskew: bool = False) -> Prepared:
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
    inverse = None
    if deskew:
        angle, confidence = estimate_skew_with_confidence(gray)
        q["deskew_angle"], q["deskew_confidence"] = angle, confidence
        # 範囲の端に張り付いた推定(実際はもっと傾いている)と、信頼度の低い推定は補正しない
        applied = (DESKEW_MIN_DEG <= abs(angle) <= DESKEW_MAX_DEG - 0.5
                   and confidence >= DESKEW_MIN_CONFIDENCE)
        q["deskew_applied"] = applied
        if applied:
            q["deskew_size"] = [img.shape[1], img.shape[0]]  # 逆変換先(縮小後・回転前)の大きさ
            img, inverse = rotate_bounded(img, angle)
    return Prepared(img, scale, q, warnings, inverse=inverse)
