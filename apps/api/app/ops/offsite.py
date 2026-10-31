"""バックアップの外部保管(PR C)。保管先は未選定のため、インターフェースとローカルディレクトリの実装だけを持つ。

- クラウドへの書き込み・接続はしない(OFFSITE_STORAGE_LIVE=BLOCKED)。保管先が決まったら OffsiteStorage を実装する。
- 置くのは暗号化済みのファイルと manifest.json(秘密を含まない)だけ。鍵は置かない。
- 同じ名前・同じハッシュのものは置き直さない(再実行しても重複しない)。失敗は回数を決めて再試行する。
- 保管先の側で必要な設定(導入先の作業): 書き込み専用の資格情報(削除・上書きできない)、バージョン管理または
  オブジェクトロック(30 日)、保管先の暗号化、復元用の読み取り権限を別の担当者に分ける。
"""

from __future__ import annotations

import hashlib
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

    def put(self, key: str, src: Path) -> None: ...


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

    def put(self, key: str, src: Path) -> None:
        dst = self._path(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".part")
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)  # 途中で止まっても中途半端なファイルを正式な名前で残さない


def storage_from_env(env: dict[str, str] | None = None) -> OffsiteStorage | None:
    """`WARILINK_BACKUP_OFFSITE`。未設定なら None(本番ではバックアップを不完全として扱う)。"""
    e = os.environ if env is None else env
    spec = e.get("WARILINK_BACKUP_OFFSITE", "")
    if not spec:
        return None
    kind, _, rest = spec.partition(":")
    if kind == "local" and rest:
        return LocalDirStorage(Path(rest))
    raise OffsiteError(f"未対応の保管先です: {kind}(対応しているのは local:ディレクトリ だけです)")


def upload(storage: OffsiteStorage, backup_dir: Path, prefix: str, *, attempts: int = 3,
           sleep: Callable[[float], None] = time.sleep) -> dict[str, int]:
    """backup_dir のファイルを prefix/ の下へ置き、置いた後のハッシュで確かめる。manifest.json は最後に置く。"""
    files = sorted(p for p in backup_dir.rglob("*") if p.is_file())
    files.sort(key=lambda p: p.name == "manifest.json")  # manifest があれば中身はそろっている、とするため最後
    uploaded = skipped = 0
    for p in files:
        key = f"{prefix}/{p.relative_to(backup_dir).as_posix()}"
        digest = _sha256_file(p)
        if storage.sha256(key) == digest:
            skipped += 1
            continue
        for n in range(1, attempts + 1):
            try:
                storage.put(key, p)
                if storage.sha256(key) != digest:
                    raise OffsiteError("置いた後のハッシュが一致しません")
                uploaded += 1
                break
            except (OSError, OffsiteError):
                if n == attempts:
                    raise OffsiteError(f"外部保管に失敗しました({n} 回試行): {p.name}") from None
                sleep(2 ** n)
    return {"uploaded": uploaded, "skipped": skipped}
