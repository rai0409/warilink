"""アップロード画像の検証と正規化(API 側、Pillow のみ)。

- 画像として読めないもの・対応外形式・大きすぎるものは受け付けない。
- EXIF(撮影位置などの個人情報を含みうる)を除去し、向きだけ反映して保存し直す。
"""

from __future__ import annotations

import hashlib
import io
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.config import get_settings
from app.core.errors import ApiError

ALLOWED = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


@dataclass
class NormalizedImage:
    data: bytes
    content_type: str
    width: int
    height: int
    original_sha256: str


def normalize_upload(raw: bytes) -> NormalizedImage:
    s = get_settings()
    if len(raw) == 0:
        raise ApiError(422, "IMAGE_EMPTY", "画像ファイルが空です")
    if len(raw) > s.receipt_max_bytes:
        raise ApiError(413, "IMAGE_TOO_LARGE", f"画像は{s.receipt_max_bytes // (1024 * 1024)}MB以下にしてください")
    Image.MAX_IMAGE_PIXELS = s.receipt_max_pixels
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            fmt = probe.format or ""
            probe.verify()
        if fmt not in ALLOWED:
            raise ApiError(415, "IMAGE_TYPE_UNSUPPORTED", "JPEG・PNG・WebP の画像を選んでください(HEIC は未対応です)")
        with Image.open(io.BytesIO(raw)) as img:
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGB")
            out = io.BytesIO()
            # EXIF・ICC などのメタデータは付けずに保存し直す
            img.save(out, format="JPEG", quality=92, optimize=True)
            w, h = img.size
    except ApiError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError, ValueError):
        raise ApiError(415, "IMAGE_UNREADABLE", "画像を読み込めませんでした。別の画像を選んでください") from None
    return NormalizedImage(out.getvalue(), "image/jpeg", w, h, hashlib.sha256(raw).hexdigest())


def storage_root() -> Path:
    return Path(get_settings().receipt_storage_dir)


def save_image(receipt_id: uuid.UUID, data: bytes) -> str:
    key = f"{receipt_id}.jpg"
    root = storage_root()
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f".{key}.tmp"
    tmp.write_bytes(data)
    tmp.replace(root / key)  # 途中で落ちても壊れたファイルを残さない
    return key


def image_path(key: str) -> Path:
    p = (storage_root() / key).resolve()
    if p.parent != storage_root().resolve():  # パス操作の防止
        raise ValueError("invalid storage key")
    return p


def delete_image(key: str) -> None:
    image_path(key).unlink(missing_ok=True)
