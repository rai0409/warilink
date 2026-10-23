"""前処理(P11): 傾きの推定・回転の上限・BBox の元の向きへの逆変換。OCR モデルは使わない。"""

import io
import random

import cv2
import numpy as np
import pytest
from PIL import Image

from app.ocr import preprocess as pp
from app.ocr.synth import find_font, make_sample, render
from app.ocr.tokens import ModelInfo, OcrToken
from app.ocr.worker import PREPROCESS_VERSION, run_pipeline


def receipt_image(seed: int = 3) -> Image.Image:
    rng = random.Random(seed)
    return render(make_sample(rng), find_font(), rng, "easy")


def bgr(img: Image.Image) -> np.ndarray:
    return cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2BGR)


@pytest.mark.parametrize("angle", [-7.0, -3.0, 2.0, 6.0])
def test_estimate_skew_recovers_known_rotation(angle):
    rotated = receipt_image().rotate(angle, expand=True, fillcolor=(255, 255, 255))  # PIL: 反時計回りが正
    est = pp.estimate_skew(cv2.cvtColor(bgr(rotated), cv2.COLOR_BGR2GRAY))
    # 推定値は「水平に戻すための回転角」= -angle
    assert abs(est + angle) <= 0.6, (angle, est)


def test_small_and_out_of_range_angles_are_not_applied():
    straight = pp.prepare(bgr(receipt_image()), deskew=True)
    assert straight.quality["deskew_applied"] is False and straight.inverse is None
    for a in (12, 25, 40):  # 範囲外・端に張り付いた推定・信頼度の低い推定は補正しない(誤補正を避ける)
        steep = pp.prepare(bgr(receipt_image().rotate(a, expand=True, fillcolor=(255, 255, 255))), deskew=True)
        assert steep.quality["deskew_applied"] is False and steep.inverse is None, (a, steep.quality)
    off = pp.prepare(bgr(receipt_image().rotate(5, expand=True, fillcolor=(255, 255, 255))))
    assert "deskew_angle" not in off.quality and off.inverse is None  # 既定は無効


def test_rotation_is_size_bounded_and_inverse_maps_back():
    big = np.full((3000, 1600, 3), 255, np.uint8)
    out, inv = pp.rotate_bounded(big, 8.0)
    assert max(out.shape[:2]) <= pp.MAX_SIDE
    # 元の点 → 回転後 → 逆変換 で元に戻る
    fwd = cv2.invertAffineTransform(inv)
    pts = np.array([[0, 0, 1], [1599, 0, 1], [800, 1500, 1], [1599, 2999, 1]], dtype=np.float64)
    back = (np.c_[pts @ fwd.T, np.ones(4)]) @ inv.T
    assert np.abs(back - pts[:, :2]).max() < 1e-6


def test_map_bbox_lands_on_the_same_feature_in_the_original():
    # 元の向きの画像に黒い矩形 → 6° 傾けたものを入力 → 補正後の画像で矩形を探し、逆変換すると入力画像上の矩形に重なる
    base = np.full((900, 600, 3), 255, np.uint8)
    for y in range(80, 820, 60):  # 文字の行の代わりの横線(傾きの推定用)
        cv2.rectangle(base, (60, y), (540, y + 14), (0, 0, 0), -1)
    pil = Image.fromarray(base).rotate(6, expand=True, fillcolor=(255, 255, 255))
    src = np.array(pil)
    prep = pp.prepare(src, deskew=True)
    assert prep.quality["deskew_applied"] and prep.inverse is not None
    gray = cv2.cvtColor(prep.image, cv2.COLOR_BGR2GRAY)
    ys, xs = np.where(gray < 128)
    box = [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]  # 補正後(水平)の全体の枠
    w, h = prep.quality["deskew_size"]
    mapped = pp.map_bbox(box, prep.inverse, w, h)
    gy, gx = np.where(cv2.cvtColor(src, cv2.COLOR_RGB2GRAY) < 128)
    truth = [int(gx.min()), int(gy.min()), int(gx.max()), int(gy.max())]
    assert all(abs(a - b) <= 12 for a, b in zip(mapped, truth, strict=True)), (mapped, truth)


class EchoAdapter:
    """前処理後の画像の大きさの中にトークンを置く(実エンジンの代わり)。"""

    def __init__(self):
        self.shape = None

    def recognize(self, image):
        self.shape = image.shape
        lines = [("店", None), ("商品A", "¥300"), ("商品B", "¥200"), ("合計", "¥500")]
        out = []
        for i, (left, right) in enumerate(lines):
            y = 40 + i * 40
            out.append(OcrToken(left, 0.99, (40, y, 200, y + 30)))
            if right:
                out.append(OcrToken(right, 0.99, (400, y, 520, y + 30)))
        return out

    def model_info(self):
        return ModelInfo("echo", "echo", "0")


def png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_pipeline_records_version_and_returns_unrotated_coordinates():
    data = png_bytes(receipt_image().rotate(5, expand=True, fillcolor=(255, 255, 255)))
    w, h = Image.open(io.BytesIO(data)).size
    plain = run_pipeline(EchoAdapter(), data, low_confidence=0.9, deskew=False)
    assert plain["quality"]["preprocess_version"] == PREPROCESS_VERSION == "cv-0.2.0"
    assert plain["draft"]["items"][0]["bbox"] == [40, 80, 520, 110]  # 補正なしは従来どおり
    rot = run_pipeline(EchoAdapter(), data, low_confidence=0.9, deskew=True)
    assert rot["quality"]["deskew_applied"]
    assert [i["line_minor"] for i in rot["draft"]["items"]] == [300, 200]  # 解析結果は同じ
    for row in rot["draft"]["items"] + rot["draft"]["lines"]:
        x0, y0, x1, y1 = row["bbox"]
        assert 0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h  # 保存した画像(回転していない)の範囲に収まる
    assert rot["draft"]["items"][0]["bbox"] != [40, 80, 520, 110]  # 補正後の座標をそのまま出していない
    assert rot["draft"]["image_scale"] == plain["draft"]["image_scale"] == 1.0
