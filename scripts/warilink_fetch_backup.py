#!/usr/bin/env python3
"""外部保管のバックアップを、受領記録の世代を指定して空のディレクトリへ取得し、検証する(STEP 02)。復元はしない。

  warilink_fetch_backup.py --from s3://バケット/接頭辞 --backup 2026-11-04-<backup_id> \\
      --receipt-version <受領記録の VersionId> [--receipt-sha256 <hex>] --out 空のディレクトリ [--journal ...]

- 受領記録の世代(と SHA-256)は、バックアップの出力 `offsite.receipt` を運用記録から写す。S3 では世代の指定が必須
  (最新を読むと、差し替えられた受領記録を読むおそれがある)。
- 確かめること: 各ファイルの世代と実バイトの SHA-256、ファイルの一覧、暗号の認証(鍵は manifest の提供元で選ぶ)、
  撮影時点の画像、削除ジャーナルのチェックポイント。取得したファイルは暗号化されたまま。
- 復元は検証の後に `warilink_backup.py restore --from <取得先> ...`(warilink_restore_* の新しい DB だけ)。
- 出力は件数・世代・ハッシュだけ(鍵・中身・バケット名は出さない)。S3 への接続は WARILINK_AWS_LIVE=1 のときだけ。

終了コード: 0 = 検証済み / 1 = 問題あり / 3 = 拒否(引数・設定・取得先が空でない)。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import warilink_backup as wb  # noqa: E402

from app.ops.offsite import LocalDirStorage, OffsiteError, storage_from_env  # noqa: E402


def main(argv: list[str] | None = None, *, storage=None, provider=None) -> int:
    p = argparse.ArgumentParser(prog="warilink_fetch_backup.py")
    p.add_argument("--from", dest="src", required=True, help="local:ディレクトリ か s3://バケット/接頭辞")
    p.add_argument("--backup", required=True, help="保管先でのバックアップの接頭辞(例: 2026-11-04-<backup_id>)")
    p.add_argument("--receipt-version", help="受領記録の世代(S3 では必須)")
    p.add_argument("--receipt-sha256", help="受領記録の SHA-256(バックアップの出力にある値)")
    p.add_argument("--out", required=True, help="取得先(空のディレクトリ)")
    p.add_argument("--journal", help="削除ジャーナル(既定は WARILINK_DELETION_JOURNAL)")
    a = p.parse_args(argv)
    try:
        out = Path(a.out)
        if out.exists() and any(out.iterdir()):
            raise wb.Refused("取得先が空ではありません")
        if storage is None:
            try:
                storage = storage_from_env({"WARILINK_BACKUP_OFFSITE": a.src})
            except OffsiteError as e:
                raise wb.Refused(str(e)) from None
        if storage is None:
            raise wb.Refused("--from を指定してください")
        if not isinstance(storage, LocalDirStorage) and not a.receipt_version:
            raise wb.Refused("S3 から取得するときは --receipt-version(受領記録の世代)を指定してください")
        out.mkdir(parents=True, exist_ok=True)
        ns = argparse.Namespace(provider=provider, journal=a.journal)
        problems, summary = wb.verify_offsite_copy(ns, storage, a.backup, a.receipt_version, out,
                                                   expected_receipt_sha256=a.receipt_sha256)
    except wb.Refused as e:
        print(json.dumps({"refused": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 3
    print(json.dumps({"verified": not problems, "problems": problems[:20], **summary, "out": str(out)},
                     ensure_ascii=False))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
