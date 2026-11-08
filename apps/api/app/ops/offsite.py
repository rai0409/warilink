"""バックアップの外部保管(PR C、S3 は STEP 02 の app/ops/s3_storage.py)。

- `local:ディレクトリ` と `s3://バケット/接頭辞`。S3 への接続は WARILINK_AWS_LIVE=1 のときだけ(実接続の検証は
  OFFSITE_STORAGE_LIVE=BLOCKED)。
- 置いたファイルの SHA-256 と世代(VersionId。ローカルは無し)を受領記録(upload-receipt.json)にまとめ、
  manifest の後に置く。
  読み戻し(fetch)は受領記録の世代を指定して取得し、実バイトの SHA-256 を照合する。
- 置くのは暗号化済みのファイルと manifest.json(秘密を含まない)だけ。鍵は置かない。
- 同じ名前・同じハッシュのものは置き直さない(再実行しても重複しない)。失敗は回数を決めて再試行する。
- 保管先の側で必要な設定(導入先の作業): 書き込み専用の資格情報(削除・上書きできない)、バージョン管理または
  オブジェクトロック(30 日)、保管先の暗号化、復元用の読み取り権限を別の担当者に分ける。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class OffsiteError(Exception):
    pass


class OffsiteStorage(Protocol):
    name: str

    def sha256(self, key: str) -> str | None: ...  # 無ければ None

    def stat(self, key: str) -> tuple[str, str | None] | None: ...  # (SHA-256, 世代)。無ければ None

    def put(self, key: str, src: Path) -> str | None: ...  # 置いた世代(ローカルは None)

    def get(self, key: str, version_id: str | None, dst: Path) -> str: ...  # 実バイトの SHA-256


RECEIPT = "upload-receipt.json"
RECEIPT_FORMAT = "warilink-offsite-receipt-1"


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class LocalDirStorage:
    """ローカルのディレクトリを保管先に見立てる(開発・テスト・訓練用。別のディスクへの複製にも使える)。"""

    root: Path
    name: str = "local-dir"

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents:
            raise OffsiteError("保管先の外へは書き込めません")
        return p

    def sha256(self, key: str) -> str | None:
        p = self._path(key)
        return _sha256_file(p) if p.is_file() else None

    def stat(self, key: str) -> tuple[str, str | None] | None:
        d = self.sha256(key)
        return (d, None) if d is not None else None

    def put(self, key: str, src: Path) -> None:
        dst = self._path(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".part")
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)  # 途中で止まっても中途半端なファイルを正式な名前で残さない

    def get(self, key: str, version_id: str | None, dst: Path) -> str:
        src = self._path(key)
        if not src.is_file():
            raise OffsiteError("保管先にファイルがありません")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        return _sha256_file(dst)


def storage_from_env(env: dict[str, str] | None = None) -> OffsiteStorage | None:
    """`WARILINK_BACKUP_OFFSITE`。未設定なら None(本番ではバックアップを不完全として扱う)。"""
    e = os.environ if env is None else env
    spec = e.get("WARILINK_BACKUP_OFFSITE", "")
    if not spec:
        return None
    kind, _, rest = spec.partition(":")
    if kind == "local" and rest:
        return LocalDirStorage(Path(rest))
    if kind == "s3":
        from app.ops.s3_storage import storage_from_url  # noqa: PLC0415

        return storage_from_url(spec, env=dict(e))
    raise OffsiteError(f"未対応の保管先です: {kind}(対応しているのは local:ディレクトリ と s3://バケット/接頭辞 です)")


def upload(storage: OffsiteStorage, backup_dir: Path, prefix: str, *, attempts: int = 3,
           sleep: Callable[[float], None] = time.sleep, versions: dict[str, dict] | None = None) -> dict[str, int]:
    """backup_dir のファイルを prefix/ の下へ置き、置いた後のハッシュで確かめる。manifest.json は最後に置く。

    versions を渡すと、ファイルごとの {sha256, version_id, bytes} を入れる(受領記録の材料)。"""
    files = sorted(p for p in backup_dir.rglob("*") if p.is_file())
    files.sort(key=lambda p: p.name == "manifest.json")  # manifest があれば中身はそろっている、とするため最後
    uploaded = skipped = 0
    for p in files:
        rel = p.relative_to(backup_dir).as_posix()
        key = f"{prefix}/{rel}"
        digest = _sha256_file(p)
        st = storage.stat(key)
        if st is not None and st[0] == digest:
            skipped += 1
            if versions is not None:
                versions[rel] = {"sha256": digest, "version_id": st[1], "bytes": p.stat().st_size}
            continue
        for n in range(1, attempts + 1):
            try:
                vid = storage.put(key, p)
                after = storage.stat(key)
                if after is None or after[0] != digest or (vid is not None and after[1] != vid):
                    raise OffsiteError("置いた後のハッシュ・世代が一致しません")
                uploaded += 1
                if versions is not None:
                    versions[rel] = {"sha256": digest, "version_id": vid, "bytes": p.stat().st_size}
                break
            except (OSError, OffsiteError):
                if n == attempts:
                    raise OffsiteError(f"外部保管に失敗しました({n} 回試行): {p.name}") from None
                sleep(2 ** n)
    return {"uploaded": uploaded, "skipped": skipped}


def put_receipt(storage: OffsiteStorage, prefix: str, versions: dict[str, dict], work: Path) -> dict:
    """受領記録を最後に置き、その世代と SHA-256 を返す(読み戻しの起点。運用記録に残す)。"""
    body = json.dumps({"format": RECEIPT_FORMAT, "prefix": prefix, "objects": versions}, sort_keys=True,
                      ensure_ascii=False, indent=1).encode()
    p = work / RECEIPT
    p.write_bytes(body)
    vid = storage.put(f"{prefix}/{RECEIPT}", p)
    st = storage.stat(f"{prefix}/{RECEIPT}")
    digest = hashlib.sha256(body).hexdigest()
    if st is None or st[0] != digest or (vid is not None and st[1] != vid):
        raise OffsiteError("受領記録を置いた後のハッシュ・世代が一致しません")
    return {"key": f"{prefix}/{RECEIPT}", "version_id": vid, "sha256": digest}


def _safe_rel(rel: str) -> str:
    parts = rel.split("/")
    if not rel or rel.startswith("/") or any(p in ("", ".", "..") for p in parts) or rel == RECEIPT:
        raise OffsiteError("受領記録のファイル名が不正です")
    return rel


def fetch(storage: OffsiteStorage, prefix: str, receipt: dict, dest: Path) -> list[str]:
    """受領記録の世代を指定して dest へ取得し、実バイトの SHA-256 を照合する。問題の一覧を返す(空なら一致)。"""
    if receipt.get("format") != RECEIPT_FORMAT or receipt.get("prefix") != prefix:
        return ["受領記録の形式か接頭辞が違います"]
    objects = receipt.get("objects")
    if not isinstance(objects, dict) or "manifest.json" not in objects:
        return ["受領記録に manifest.json がありません(不完全なアップロード)"]
    problems = []
    for rel, meta in sorted(objects.items()):
        try:
            got = storage.get(f"{prefix}/{_safe_rel(rel)}", meta.get("version_id"), dest / rel)
        except OffsiteError as e:
            problems.append(f"{rel}: {e}")
            continue
        if got != meta.get("sha256"):
            problems.append(f"{rel}: 読み戻した内容の SHA-256 が受領記録と一致しません")
    return problems


def read_receipt(storage: OffsiteStorage, prefix: str, version_id: str | None, work: Path) -> tuple[dict, str]:
    """受領記録を取得する。戻り値は (内容, 実バイトの SHA-256)。"""
    p = work / RECEIPT
    digest = storage.get(f"{prefix}/{RECEIPT}", version_id, p)
    try:
        return json.loads(p.read_text(encoding="utf-8")), digest
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise OffsiteError("受領記録の形式が壊れています") from None
