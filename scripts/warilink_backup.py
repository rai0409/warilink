#!/usr/bin/env python3
"""PostgreSQL とレシート画像を、整合したひとまとまりとしてバックアップ・復元する(運用手順の実装)。

  backup  --db NAME --receipts-dir DIR --out OUT_DIR       ダンプ(pg_dump -Fc)+画像+manifest.json
  prune   --root DIR [--days 30] [--dry-run]                保持日数を過ぎたバックアップを削除
  restore --from OUT_DIR --target-db warilink_restore_* --receipts-target EMPTY_DIR
                                                          新しい使い捨て DB と空ディレクトリにだけ復元し、検証する
  drop    --target-db warilink_restore_*                  復元先の使い捨て DB を消す
  drill   --workdir DIR                                   使い捨て DB を作って seed → backup → restore → 検証 → 片付け

安全のための制約:
- 復元先 DB 名は `warilink_restore_` で始まり、まだ存在しないものだけ(既存・共有 DB には書き込まない)。
- 画像の復元先は空のディレクトリだけ。
- manifest には件数・ハッシュ・スキーマ版だけを書き、接続文字列・パスワード・画像の中身は書かない。
- ダンプの暗号化と外部保管はここでは行わない(本番の保管先・鍵が決まってから。docs/SECURITY_PRIVACY.md 参照)。

PostgreSQL のツールは DB コンテナ(既定 `warilink-db-1`)の中のものを `docker exec` で使う。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROTECTED = {"postgres", "template0", "template1", "warilink", "warilink_test", "warilink_e2e"}
RESTORE_PREFIX = "warilink_restore_"
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
# 件数を比べる表(機密の中身は読まない)
TABLES = ["identities", "groups", "memberships", "expenses", "expense_shares", "settlement_batches",
          "settlement_transfers", "receipts", "receipt_jobs", "ocr_runs", "ocr_corrections", "ocr_quota_ledger",
          "audit_events"]
# 濫用対策の回数制限の記録は一時データ(IP などの鍵つきハッシュ)。保持する必要がなく、復元すると古い制限が
# 戻るため、表の定義だけをダンプし中身は含めない。件数の照合(TABLES)の対象外。
EXCLUDED_TABLE_DATA = ["rate_limit_events"]


class Refused(Exception):
    pass


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pg(args: argparse.Namespace, *cmd: str, stdin=None, stdout=None, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "exec", "-i", args.container, *cmd], stdin=stdin, stdout=stdout or subprocess.PIPE,
                          stderr=subprocess.PIPE, check=check)


def psql(args: argparse.Namespace, db: str, sql: str) -> str:
    out = pg(args, "psql", "-U", args.user, "-d", db, "-AtX", "-v", "ON_ERROR_STOP=1", "-c", sql)
    return out.stdout.decode().strip()


def db_exists(args: argparse.Namespace, name: str) -> bool:
    return psql(args, "postgres", f"SELECT 1 FROM pg_database WHERE datname = '{name}'") == "1"


def check_name(name: str) -> None:
    if not NAME_RE.match(name):
        raise Refused(f"DB 名が不正です: {name}")


def snapshot(args: argparse.Namespace, db: str) -> dict:
    version = psql(args, db, "SELECT version_num FROM alembic_version")
    counts = {t: int(psql(args, db, f"SELECT count(*) FROM {t}")) for t in TABLES}
    live = psql(args, db, "SELECT storage_key FROM receipts WHERE image_deleted_at IS NULL ORDER BY 1")
    return {"alembic_version": version, "row_counts": counts, "live_image_keys": [k for k in live.split("\n") if k]}


# ---------------------------------------------------------------- backup


def cmd_backup(args: argparse.Namespace) -> int:
    check_name(args.db)
    out = Path(args.out)
    if out.exists() and any(out.iterdir()):
        raise Refused(f"出力先が空ではありません: {out}")
    (out / "receipts").mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    before = snapshot(args, args.db)
    dump = out / "db.dump"
    with dump.open("wb") as f:
        pg(args, "pg_dump", "-U", args.user, "-d", args.db, "-Fc", "--no-owner", "--no-privileges",
           *(f"--exclude-table-data={t}" for t in EXCLUDED_TABLE_DATA), stdout=f)
    # 画像はダンプの直後に複写する。ダンプ後に増えた画像は DB に無いので orphan、ダンプ後に消えた画像は missing として検出する
    src = Path(args.receipts_dir)
    files = {}
    for p in sorted(src.iterdir()) if src.exists() else []:
        if p.is_file() and not p.name.startswith("."):
            shutil.copy2(p, out / "receipts" / p.name)
            files[p.name] = sha256_file(out / "receipts" / p.name)
    missing = sorted(set(before["live_image_keys"]) - set(files))
    manifest = {
        "format": "warilink-backup-1",
        "created_at": datetime.now(UTC).isoformat(),
        "source_db": args.db,
        "alembic_version": before["alembic_version"],
        "row_counts": before["row_counts"],
        "live_image_keys": before["live_image_keys"],
        "db_dump": {"file": "db.dump", "sha256": sha256_file(dump), "bytes": dump.stat().st_size},
        "images": files,
        "missing_images_at_backup": missing,
        "encrypted": False,
        "note": "暗号化・外部保管は未実施(本番の保管先・鍵が未定)。このディレクトリは個人情報を含むため権限 700",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    print(json.dumps({"backup": str(out), "alembic_version": manifest["alembic_version"],
                      "rows": sum(manifest["row_counts"].values()), "images": len(files),
                      "missing_images_at_backup": len(missing)}, ensure_ascii=False))
    return 0 if not missing else 1


# ---------------------------------------------------------------- restore


def guard_target(args: argparse.Namespace, name: str) -> None:
    check_name(name)
    if name in PROTECTED or not name.startswith(RESTORE_PREFIX):
        raise Refused(f"復元先は {RESTORE_PREFIX}* の新しい DB だけです(既存・共有 DB には復元しません): {name}")


def cmd_restore(args: argparse.Namespace) -> int:
    src = Path(getattr(args, "from"))
    manifest = json.loads((src / "manifest.json").read_text())
    guard_target(args, args.target_db)
    if db_exists(args, args.target_db):
        raise Refused(f"復元先 DB が既に存在します: {args.target_db}")
    target_dir = Path(args.receipts_target)
    if target_dir.exists() and any(target_dir.iterdir()):
        raise Refused(f"画像の復元先が空ではありません: {target_dir}")
    # 1. バックアップ自体の改ざん・破損を先に検出する
    problems = []
    if sha256_file(src / "db.dump") != manifest["db_dump"]["sha256"]:
        problems.append("db.dump のハッシュが manifest と一致しません")
    for name, digest in manifest["images"].items():
        p = src / "receipts" / name
        if not p.is_file() or sha256_file(p) != digest:
            problems.append(f"画像 {name} が欠けているか壊れています")
    if problems:
        print(json.dumps({"restored": False, "problems": problems}, ensure_ascii=False))
        return 1
    # 2. 新しい DB へ復元(外部キー制約も作り直されるため、参照切れがあれば失敗する)
    psql(args, "postgres", f'CREATE DATABASE "{args.target_db}"')
    with (src / "db.dump").open("rb") as f:
        pg(args, "pg_restore", "-U", args.user, "-d", args.target_db, "--no-owner", "--no-privileges",
           "--exit-on-error", stdin=f)
    target_dir.mkdir(parents=True, exist_ok=True)
    for name in manifest["images"]:
        shutil.copy2(src / "receipts" / name, target_dir / name)
    # 3. 読み戻して検証
    after = snapshot(args, args.target_db)
    restored_files = {p.name: sha256_file(p) for p in target_dir.iterdir() if p.is_file()}
    fk_invalid = psql(args, args.target_db, "SELECT count(*) FROM pg_constraint WHERE contype = 'f' AND NOT convalidated")
    checks = {
        "alembic_version": after["alembic_version"] == manifest["alembic_version"],
        "row_counts": after["row_counts"] == manifest["row_counts"],
        "image_checksums": restored_files == manifest["images"],
        "live_images_present": set(after["live_image_keys"]) <= set(restored_files),
        "foreign_keys_validated": fk_invalid == "0",
    }
    ok = all(checks.values())
    print(json.dumps({"restored": ok, "target_db": args.target_db, "checks": checks,
                      "rows": sum(after["row_counts"].values()), "images": len(restored_files)}, ensure_ascii=False))
    return 0 if ok else 1


def cmd_drop(args: argparse.Namespace) -> int:
    guard_target(args, args.target_db)
    psql(args, "postgres", f'DROP DATABASE IF EXISTS "{args.target_db}"')
    print(json.dumps({"dropped": args.target_db}))
    return 0


# ---------------------------------------------------------------- drill


def cmd_drill(args: argparse.Namespace) -> int:
    """使い捨ての元 DB を作り、API 経由でデータを入れてから backup → restore → 検証 → 片付けまで行う。"""
    stamp = time.strftime("%Y%m%d%H%M%S")
    src_db, dst_db = f"{RESTORE_PREFIX}src_{stamp}", f"{RESTORE_PREFIX}dst_{stamp}"
    work = Path(args.workdir)
    if work.exists() and any(work.iterdir()):
        raise Refused(f"作業ディレクトリが空ではありません: {work}")
    src_images, backup_dir, dst_images = work / "src_receipts", work / "backup", work / "dst_receipts"
    src_images.mkdir(parents=True)
    rc = 1
    try:
        guard_target(args, src_db)
        psql(args, "postgres", f'CREATE DATABASE "{src_db}"')
        url = f"postgresql+psycopg://{args.user}:{os.environ.get('WARILINK_DRILL_DB_PASSWORD', 'warilink-dev-only')}" \
              f"@localhost:{args.port}/{src_db}"
        # 種データにはレシート画像を含める(V0 の既定では OCR が無効なので、訓練の元 DB だけで有効にする)
        env = {**os.environ, "WARILINK_DATABASE_URL": url, "WARILINK_RECEIPT_STORAGE_DIR": str(src_images),
               "WARILINK_ENVIRONMENT": "test", "WARILINK_FEATURE_OCR": "true", "PYTHONPATH": str(ROOT / "apps" / "api")}
        api = ROOT / "apps" / "api"
        py = str(ROOT / ".venv" / "bin" / "python")
        subprocess.run([py, "-m", "alembic", "upgrade", "head"], cwd=api, env=env, check=True, capture_output=True)
        seed = subprocess.run([py, str(ROOT / "scripts" / "warilink_backup_seed.py")], cwd=api, env=env,
                              capture_output=True, text=True, check=False)
        print(seed.stdout.strip() or seed.stderr.strip()[-800:])
        if seed.returncode != 0:
            return 1
        ns = argparse.Namespace(**{**vars(args), "db": src_db, "receipts_dir": str(src_images), "out": str(backup_dir)})
        if cmd_backup(ns) != 0:
            return 1
        ns = argparse.Namespace(**{**vars(args), "from": str(backup_dir), "target_db": dst_db,
                                   "receipts_target": str(dst_images)})
        rc = cmd_restore(ns)
        if rc == 0:
            # 復元した DB をアプリから読めること(合計金額・残高の和)を確かめる
            env2 = {**env, "WARILINK_DATABASE_URL": url.rsplit("/", 1)[0] + f"/{dst_db}",
                    "WARILINK_RECEIPT_STORAGE_DIR": str(dst_images)}
            chk = subprocess.run([py, str(ROOT / "scripts" / "warilink_backup_seed.py"), "--verify"], cwd=api,
                                 env=env2, capture_output=True, text=True, check=False)
            print(chk.stdout.strip() or chk.stderr.strip()[-400:])
            rc = chk.returncode
    finally:
        for name in (src_db, dst_db):
            try:
                psql(args, "postgres", f'DROP DATABASE IF EXISTS "{name}"')
            except subprocess.CalledProcessError:
                print(json.dumps({"cleanup_failed": name}))
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
    print(json.dumps({"drill": "PASS" if rc == 0 else "FAIL", "dropped": [src_db, dst_db]}))
    return rc


# ---------------------------------------------------------------- prune(保持日数を過ぎたバックアップの削除)

BACKUP_RETENTION_DAYS = 30  # docs/DATA_MODEL.md §11。削除済み一覧(tombstone)の保持日数と同じ


def cmd_prune(args: argparse.Namespace) -> int:
    """--root の直下にある、このスクリプトが作ったバックアップ(manifest.json がある)のうち、保持日数を過ぎたものを消す。

    manifest の無いディレクトリ・ファイルには触れない。--dry-run は一覧を出すだけ。
    """
    root = Path(args.root)
    if not root.is_dir():
        raise Refused(f"バックアップの置き場所が見つかりません: {root}")
    cutoff = datetime.now(UTC) - timedelta(days=args.days)
    expired, kept = [], 0
    for d in sorted(root.iterdir()):
        m = d / "manifest.json"
        if not (d.is_dir() and m.is_file()):
            continue
        manifest = json.loads(m.read_text(encoding="utf-8"))
        if manifest.get("format") != "warilink-backup-1":
            continue
        if datetime.fromisoformat(manifest["created_at"]) < cutoff:
            expired.append(d)
        else:
            kept += 1
    if not args.dry_run:
        for d in expired:
            shutil.rmtree(d)
    print(json.dumps({"dry_run": args.dry_run, "days": args.days, "removed": [d.name for d in expired],
                      "kept": kept}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="warilink_backup.py")
    p.add_argument("--container", default=os.environ.get("WARILINK_DB_CONTAINER", "warilink-db-1"))
    p.add_argument("--user", default=os.environ.get("POSTGRES_USER", "warilink"))
    p.add_argument("--port", default="55432")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup")
    b.add_argument("--db", required=True)
    b.add_argument("--receipts-dir", required=True)
    b.add_argument("--out", required=True)
    r = sub.add_parser("restore")
    r.add_argument("--from", required=True)
    r.add_argument("--target-db", required=True)
    r.add_argument("--receipts-target", required=True)
    d = sub.add_parser("drop")
    d.add_argument("--target-db", required=True)
    dr = sub.add_parser("drill")
    dr.add_argument("--workdir", required=True)
    dr.add_argument("--keep", action="store_true", help="作業ディレクトリを残す(調査用)")
    pr = sub.add_parser("prune")
    pr.add_argument("--root", required=True, help="バックアップを置いているディレクトリ")
    pr.add_argument("--days", type=int, default=BACKUP_RETENTION_DAYS)
    pr.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    try:
        return {"backup": cmd_backup, "restore": cmd_restore, "drop": cmd_drop, "drill": cmd_drill,
                "prune": cmd_prune}[a.cmd](a)
    except Refused as e:
        print(json.dumps({"refused": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
