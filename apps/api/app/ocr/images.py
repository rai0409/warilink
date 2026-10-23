"""アップロード画像の検証と正規化(API 側、Pillow のみ)。

- 画像として読めないもの・対応外形式・大きすぎるものは受け付けない。
- EXIF(撮影位置などの個人情報を含みうる)を除去し、向きだけ反映して保存し直す。
"""

from __future__ import annotations

import hashlib
import io
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ApiError
from app.models.tables import OcrRun, Receipt

ALLOWED = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


@dataclass
class NormalizedImage:
    data: bytes
    content_type: str
    width: int
    height: int
    original_sha256: str


@dataclass
class ProbedImage:
    format: str
    width: int
    height: int


def check_size(n: int) -> None:
    s = get_settings()
    if n == 0:
        raise ApiError(422, "IMAGE_EMPTY", "画像ファイルが空です")
    if n > s.receipt_max_bytes:
        raise ApiError(413, "IMAGE_TOO_LARGE", f"画像は{s.receipt_max_bytes // (1024 * 1024)}MB以下にしてください")


def probe(raw: bytes) -> ProbedImage:
    """ヘッダーだけを読み、形式と寸法を確かめる(画素のデコードはしない)。重い処理の前に呼ぶ。"""
    s = get_settings()
    check_size(len(raw))
    try:
        with Image.open(io.BytesIO(raw)) as im:  # 遅延読み込み: ここではヘッダーのみ
            fmt, (w, h) = im.format or "", im.size
    except Image.DecompressionBombError:
        # Pillow は寸法がその上限の 2 倍を超えるとヘッダーの時点で拒否する
        raise ApiError(413, "IMAGE_TOO_MANY_PIXELS", "画像の解像度が大きすぎます。縮小してから選んでください") from None
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        raise ApiError(415, "IMAGE_UNREADABLE", "画像を読み込めませんでした。別の画像を選んでください") from None
    if fmt not in ALLOWED:
        raise ApiError(415, "IMAGE_TYPE_UNSUPPORTED", "JPEG・PNG・WebP の画像を選んでください(HEIC は未対応です)")
    if w <= 0 or h <= 0:
        raise ApiError(415, "IMAGE_UNREADABLE", "画像を読み込めませんでした。別の画像を選んでください")
    # Pillow の MAX_IMAGE_PIXELS は上限の 2 倍まで警告だけなので、ここで明示的に拒否する
    if w * h > s.receipt_max_pixels or max(w, h) > s.receipt_max_side:
        raise ApiError(413, "IMAGE_TOO_MANY_PIXELS", "画像の解像度が大きすぎます。縮小してから選んでください")
    return ProbedImage(fmt, w, h)


def normalize_upload(raw: bytes) -> NormalizedImage:
    s = get_settings()
    probe(raw)
    Image.MAX_IMAGE_PIXELS = s.receipt_max_pixels
    try:
        with Image.open(io.BytesIO(raw)) as probe_img:
            probe_img.verify()
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


# ---------------------------------------------------------------- 保存期間を過ぎた画像の削除


@dataclass
class PurgeResult:
    dry_run: bool
    retention_days: int
    candidates: int = 0
    deleted: int = 0
    failed: list[str] = field(default_factory=list)  # 削除できなかったレシート ID(パス・内容は出さない)
    scrubbed_runs: int = 0

    def to_json(self) -> dict:
        return {"dry_run": self.dry_run, "retention_days": self.retention_days, "candidates": self.candidates,
                "deleted": self.deleted, "failed": self.failed, "scrubbed_ocr_runs": self.scrubbed_runs}


def _scrub_run(run: OcrRun) -> None:
    """画像と一緒に、読み取った生の文字(支払・会員番号などの行を含みうる)も消す。

    確定した支出・訂正記録に必要な構造(商品・調整・合計)は残す。
    """
    run.tokens = None
    if run.draft and "lines" in run.draft:
        run.draft = {k: v for k, v in run.draft.items() if k != "lines"}


def purge_expired(db: Session, *, dry_run: bool, now: datetime | None = None) -> PurgeResult:
    """保存期間を過ぎた画像を 1 枚ずつ削除し、成功したものだけ DB に削除済みと記録する。

    - 何度実行しても同じ結果になる(削除済みは対象外。ファイルが既に無い場合も成功として記録)。
    - ファイルを消せなかった行は記録しない(次回に再試行)。失敗があれば呼び出し側が異常終了にする。
    - 他のプロセスが処理中の行は飛ばす(SKIP LOCKED)。
    """
    s = get_settings()
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=s.receipt_image_retention_days)
    res = PurgeResult(dry_run=dry_run, retention_days=s.receipt_image_retention_days)
    rows = db.execute(select(Receipt).where(Receipt.created_at < cutoff, Receipt.image_deleted_at.is_(None))
                      .order_by(Receipt.created_at).with_for_update(skip_locked=True)).scalars().all()
    res.candidates = len(rows)
    if dry_run:
        db.rollback()
        return res
    for r in rows:
        try:
            delete_image(r.storage_key)
        except (OSError, ValueError):
            res.failed.append(str(r.id))
            continue
        r.image_deleted_at = now
        for run in db.execute(select(OcrRun).where(OcrRun.receipt_id == r.id)).scalars():
            _scrub_run(run)
            res.scrubbed_runs += 1
        res.deleted += 1
        db.flush()
    db.commit()
    return res


@dataclass
class StorageReport:
    live_rows: int = 0
    files: int = 0
    missing_files: list[str] = field(default_factory=list)  # DB は画像ありなのにファイルが無いレシート ID
    orphan_files: int = 0  # DB に対応する行が無い、または削除済みの行のファイル
    stale_tmp_files: int = 0

    @property
    def consistent(self) -> bool:
        return not self.missing_files and self.orphan_files == 0

    def to_json(self) -> dict:
        return {"live_rows": self.live_rows, "files": self.files, "missing_files": self.missing_files,
                "orphan_files": self.orphan_files, "stale_tmp_files": self.stale_tmp_files,
                "consistent": self.consistent}


def check_storage(db: Session) -> StorageReport:
    """DB の「画像あり」の行と、保存ディレクトリのファイルが一致しているかを調べる(変更はしない)。"""
    root = storage_root()
    names = {p.name for p in root.iterdir() if p.is_file()} if root.exists() else set()
    rep = StorageReport()
    rep.stale_tmp_files = sum(1 for n in names if n.startswith("."))
    files = {n for n in names if not n.startswith(".")}
    rep.files = len(files)
    live = dict(db.execute(select(Receipt.storage_key, Receipt.id).where(Receipt.image_deleted_at.is_(None))).all())
    rep.live_rows = len(live)
    rep.missing_files = sorted(str(rid) for key, rid in live.items() if key not in files)
    rep.orphan_files = len(files - set(live))
    return rep
