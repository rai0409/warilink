#!/usr/bin/env python3
"""PostgreSQL とレシート画像を、整合したひとまとまりとしてバックアップ・復元する(運用手順の実装)。

  backup  --db NAME --receipts-dir DIR --out OUT_DIR       暗号化したダンプ(pg_dump -Fc)+画像+manifest(PR C)
  prune   --root DIR [--days 30] [--dry-run]                保持日数を過ぎたバックアップを削除
  restore --from OUT_DIR --target-db warilink_restore_* --receipts-target EMPTY_DIR --tombstones FILE
                                                          新しい使い捨て DB と空ディレクトリにだけ復元し、検証し、
                                                          DB の外の削除済み一覧で削除を再適用する
  drop    --target-db warilink_restore_*                  復元先の使い捨て DB を消す
  drill   --workdir DIR                                   使い捨て DB を作って seed → backup → restore → 検証 → 片付け

安全のための制約:
- 復元先 DB 名は `warilink_restore_` で始まり、まだ存在しないものだけ(既存・共有 DB には書き込まない)。
- 画像の復元先は空のディレクトリだけ。
- manifest には件数・ハッシュ・スキーマ版だけを書き、接続文字列・パスワード・画像の中身は書かない。
- 暗号化(既定、format warilink-backup-2): AES-256-GCM のエンベロープ暗号化(apps/api/app/ops/backup_crypto.py)。
  鍵は WARILINK_BACKUP_KEYS / WARILINK_BACKUP_ACTIVE_KEY(KMS は未接続)。ダンプは平文をディスクに書かずに暗号化する。
  復元は全ファイルの認証を先に済ませてから、復号した内容を pg_restore へ直接流す(平文の一時ファイルを作らない)。
- 平文のバックアップ(--insecure-plaintext、format warilink-backup-1)は開発用。
  本番(WARILINK_ENVIRONMENT=production)では拒否。
- 外部保管: WARILINK_BACKUP_OFFSITE(現状は local:ディレクトリ だけ)。本番で未設定なら終了コード 4(不完全)。
- 復元は DB の外の削除済み一覧(--tombstones)が必須。バックアップ時点の一覧より古ければ拒否し、復元後に必ず再適用する。

PostgreSQL のツールは DB コンテナ(既定 `warilink-db-1`)の中のものを `docker exec` で使う。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))

from app.ops.backup_crypto import (  # noqa: E402
    ALGORITHM,
    CHUNK_BYTES,
    FORMAT,
    BackupCryptoError,
    EncryptWriter,
    EnvKeyProvider,
    FakeKmsProvider,
    KeyConfigError,
    decrypt_chunks,
    dek_aad,
    new_dek,
)
from app.ops.offsite import LocalDirStorage, OffsiteError, storage_from_env, upload  # noqa: E402

LEGACY_FORMAT = "warilink-backup-1"
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


def is_production() -> bool:
    return os.environ.get("WARILINK_ENVIRONMENT", "development") == "production"


def dump_cmd(args: argparse.Namespace, db: str) -> list[str]:
    return ["docker", "exec", "-i", args.container, "pg_dump", "-U", args.user, "-d", db, "-Fc", "--no-owner",
            "--no-privileges", *(f"--exclude-table-data={t}" for t in EXCLUDED_TABLE_DATA)]


def record_run(args: argparse.Namespace, db: str, job: str) -> None:
    """成功の時刻だけを maintenance_runs に残す(監視でバックアップの新しさを見るため)。

    表が無い古い DB では何もしない。
    """
    try:
        psql(args, db, "DO $$ BEGIN IF to_regclass('public.maintenance_runs') IS NOT NULL THEN "
                       f"INSERT INTO maintenance_runs(job, last_success_at) VALUES ('{job}', now()) "
                       "ON CONFLICT (job) DO UPDATE SET last_success_at = now(); END IF; END $$;")
    except subprocess.CalledProcessError:
        print(json.dumps({"warning": "maintenance_runs に記録できませんでした"}, ensure_ascii=False), file=sys.stderr)


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


def cmd_backup_plaintext(args: argparse.Namespace) -> int:
    """開発用の平文バックアップ(format warilink-backup-1)。本番では使えない。"""
    if is_production():
        raise Refused("本番では平文のバックアップを作れません(暗号化の鍵を設定してください)")
    check_name(args.db)
    out = Path(args.out)
    if out.exists() and any(out.iterdir()):
        raise Refused(f"出力先が空ではありません: {out}")
    (out / "receipts").mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    before = snapshot(args, args.db)
    dump = out / "db.dump"
    with dump.open("wb") as f:
        subprocess.run(dump_cmd(args, args.db), stdout=f, stderr=subprocess.PIPE, check=True)
    # 画像はダンプの直後に複写する。ダンプ後に増えた画像は DB に無いので orphan、ダンプ後に消えた画像は missing として検出する
    src = Path(args.receipts_dir)
    files = {}
    for p in sorted(src.iterdir()) if src.exists() else []:
        if p.is_file() and not p.name.startswith("."):
            shutil.copy2(p, out / "receipts" / p.name)
            files[p.name] = sha256_file(out / "receipts" / p.name)
    missing = sorted(set(before["live_image_keys"]) - set(files))
    manifest = {
        "format": LEGACY_FORMAT,
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


def enc_path(name: str) -> str:
    return f"{name}.enc"


def key_provider(args: argparse.Namespace):
    """訓練・テストでは args.provider(FakeKmsProvider など)を使い、通常は環境変数の鍵を使う。"""
    provider = getattr(args, "provider", None)
    if provider is not None:
        return provider
    try:
        return EnvKeyProvider.from_env()
    except KeyConfigError as e:
        raise Refused(str(e)) from None


def tombstones_latest(args: argparse.Namespace, db: str) -> str | None:
    v = psql(args, db, "SELECT to_char(max(created_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US') "
                       "FROM deletion_tombstones")
    return f"{v}+00:00" if v else None


def _encrypt_file(src: Path, dst: Path, dek: bytes, backup_id: str, name: str):
    with src.open("rb") as fin, dst.open("wb") as fout:
        w = EncryptWriter(fout, dek, backup_id, name)
        for chunk in iter(lambda: fin.read(1 << 20), b""):
            w.write(chunk)
        return w.close()


def cmd_backup(args: argparse.Namespace) -> int:
    """暗号化したバックアップ(format warilink-backup-2)。

    終了コード: 0 成功 / 1 画像の欠け / 3 拒否 / 4 外部保管なし(本番)。
    """
    if getattr(args, "insecure_plaintext", False):
        return cmd_backup_plaintext(args)
    check_name(args.db)
    out = Path(args.out)
    if out.exists() and any(out.iterdir()):
        raise Refused(f"出力先が空ではありません: {out}")
    provider = key_provider(args)
    storage = getattr(args, "storage", None)
    if storage is None:
        try:
            storage = storage_from_env()
        except OffsiteError as e:
            raise Refused(str(e)) from None
    (out / "receipts").mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    backup_id, created_at = uuid.uuid4().hex, datetime.now(UTC).isoformat()
    dek = new_dek()
    key_id, wrapped = provider.wrap(dek, dek_aad(backup_id, created_at, provider.active_key_id()))
    before = snapshot(args, args.db)
    latest_tomb = tombstones_latest(args, args.db)
    files = {}
    # ダンプ: pg_dump の出力を平文のままディスクに書かず、塊ごとに暗号化する
    proc = subprocess.Popen(dump_cmd(args, args.db), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None and proc.stderr is not None
    with (out / enc_path("db.dump")).open("wb") as f:
        w = EncryptWriter(f, dek, backup_id, "db.dump")
        for chunk in iter(lambda: proc.stdout.read(1 << 20), b""):
            w.write(chunk)
        files["db.dump"] = w.close()
    err = proc.stderr.read().decode(errors="replace").strip().splitlines()
    if proc.wait() != 0:
        shutil.rmtree(out)
        raise Refused(f"pg_dump が失敗しました: {err[-1][:200] if err else ''}")
    src = Path(args.receipts_dir)
    for p in sorted(src.iterdir()) if src.exists() else []:
        if p.is_file() and not p.name.startswith("."):
            name = f"receipts/{p.name}"
            files[name] = _encrypt_file(p, out / enc_path(name), dek, backup_id, name)
    images = sorted(n.split("/", 1)[1] for n in files if n.startswith("receipts/"))
    missing = sorted(set(before["live_image_keys"]) - set(images))
    inner = {
        "backup_id": backup_id, "created_at": created_at, "source_db": args.db,
        "alembic_version": before["alembic_version"], "row_counts": before["row_counts"],
        "live_image_keys": before["live_image_keys"], "missing_images_at_backup": missing,
        "tombstones_latest": latest_tomb,
        "files": {n: {"ciphertext_sha256": e.ciphertext_sha256, "bytes": e.bytes,
                      "plaintext_sha256": e.plaintext_sha256} for n, e in files.items()},
    }
    with (out / enc_path("manifest")).open("wb") as f:
        w = EncryptWriter(f, dek, backup_id, "manifest")
        w.write(json.dumps(inner, ensure_ascii=False).encode())
        files["manifest"] = w.close()
    outer = {
        "format": FORMAT, "backup_id": backup_id, "created_at": created_at, "encrypted": True,
        "key": {"provider": provider.name, "key_id": key_id, "wrapped_dek": base64.b64encode(wrapped).decode()},
        "cipher": {"algorithm": ALGORITHM, "chunk_bytes": CHUNK_BYTES},
        "files": {n: {"nonce_prefix": e.nonce_prefix, "ciphertext_sha256": e.ciphertext_sha256, "bytes": e.bytes}
                  for n, e in files.items()},
        "note": "中身(件数・画像のキー)は manifest.enc に暗号化。鍵はこのバックアップに含まない",
    }
    (out / "manifest.json").write_text(json.dumps(outer, ensure_ascii=False, indent=1))
    del dek
    offsite = None
    if storage is not None:
        try:
            offsite = upload(storage, out, f"{created_at[:10]}-{backup_id}")
        except OffsiteError as e:
            print(json.dumps({"backup": str(out), "offsite_failed": str(e)}, ensure_ascii=False))
            return 4
    rc = 1 if missing else 0
    if rc == 0:
        record_run(args, args.db, "backup")
    print(json.dumps({"backup": str(out), "format": FORMAT, "key_provider": provider.name, "key_id": key_id,
                      "alembic_version": inner["alembic_version"], "rows": sum(inner["row_counts"].values()),
                      "images": len(images), "missing_images_at_backup": len(missing),
                      "offsite": offsite if offsite is not None else "not_configured"}, ensure_ascii=False))
    if storage is None and is_production():
        return 4  # 本番で外部保管が無いバックアップは不完全(PASS 扱いしない)
    return rc


# ---------------------------------------------------------------- restore


def guard_target(args: argparse.Namespace, name: str) -> None:
    check_name(name)
    if name in PROTECTED or not name.startswith(RESTORE_PREFIX):
        raise Refused(f"復元先は {RESTORE_PREFIX}* の新しい DB だけです(既存・共有 DB には復元しません): {name}")


def open_encrypted(args: argparse.Namespace, src: Path, outer: dict) -> tuple[dict, bytes]:
    """鍵をほどき、暗号化した中身の manifest を読み、外側の manifest と突き合わせる。失敗は BackupCryptoError。"""
    if outer.get("cipher", {}).get("algorithm") != ALGORITHM:
        raise BackupCryptoError("未対応の暗号方式です")
    key = outer["key"]
    provider = key_provider(args)
    dek = provider.unwrap(key["key_id"], base64.b64decode(key["wrapped_dek"]),
                          dek_aad(outer["backup_id"], outer["created_at"], key["key_id"]))
    m = outer["files"].get("manifest")
    mp = src / enc_path("manifest")
    if m is None or not mp.is_file() or sha256_file(mp) != m["ciphertext_sha256"]:
        raise BackupCryptoError("manifest.enc が欠けているか書き換えられています")
    with mp.open("rb") as f:
        inner = json.loads(b"".join(decrypt_chunks(f, dek, outer["backup_id"], "manifest", m["nonce_prefix"])))
    if inner.get("backup_id") != outer["backup_id"] or inner.get("created_at") != outer["created_at"]:
        raise BackupCryptoError("manifest の内容が一致しません")
    data = {n: v for n, v in outer["files"].items() if n != "manifest"}
    if set(data) != set(inner["files"]) or any(
            data[n]["ciphertext_sha256"] != inner["files"][n]["ciphertext_sha256"] for n in data):
        raise BackupCryptoError("ファイルの一覧が manifest と一致しません(追加・削除・差し替え)")
    return inner, dek


def verify_encrypted(src: Path, outer: dict, inner: dict, dek: bytes) -> list[str]:
    """全ファイルを復号して(結果は捨てる)、認証と平文のハッシュを確かめる。DB に触れる前に行う。"""
    problems = []
    for name, meta in inner["files"].items():
        p = src / enc_path(name)
        if not p.is_file():
            problems.append(f"{name} がありません")
            continue
        if sha256_file(p) != meta["ciphertext_sha256"]:
            problems.append(f"{name} のハッシュが manifest と一致しません")
            continue
        h = hashlib.sha256()
        try:
            with p.open("rb") as f:
                for chunk in decrypt_chunks(f, dek, outer["backup_id"], name, outer["files"][name]["nonce_prefix"]):
                    h.update(chunk)
        except BackupCryptoError as e:
            problems.append(str(e))
            continue
        if h.hexdigest() != meta["plaintext_sha256"]:
            problems.append(f"{name} の中身が manifest と一致しません")
    return problems


def read_tombstones(path: Path) -> tuple[list[dict], str | None]:
    rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    latest = max((r["created_at"] for r in rows if r.get("created_at")), default=None)
    return rows, latest


def target_url(args: argparse.Namespace, db: str) -> str:
    password = os.environ.get("WARILINK_RESTORE_DB_PASSWORD")
    if not password:
        if is_production():
            raise Refused("WARILINK_RESTORE_DB_PASSWORD を設定してください")
        password = os.environ.get("WARILINK_DRILL_DB_PASSWORD", "warilink-dev-only")
    return f"postgresql+psycopg://{args.user}:{password}@localhost:{args.port}/{db}"


def reapply_tombstones(args: argparse.Namespace, db: str, path: Path, rows: list[dict]) -> dict:
    """復元した DB に削除を再適用し、削除が保たれているかを読み戻して確かめる。"""
    env = {**os.environ, "WARILINK_DATABASE_URL": target_url(args, db), "PYTHONPATH": str(ROOT / "apps" / "api")}
    r = subprocess.run([str(ROOT / ".venv" / "bin" / "python"), "-m", "app.modules.identity.retention",
                        "apply-tombstones", "--in", str(path)], cwd=ROOT / "apps" / "api", env=env,
                       capture_output=True, text=True, check=False)
    applied = r.returncode == 0
    ids = {k: [x["target_id"] for x in rows if x["kind"] == k] for k in ("identity", "destination", "group")}
    def count(sql: str, values: list[str]) -> int:
        if not values:
            return 0
        lst = ",".join(f"'{uuid.UUID(v)}'" for v in values)  # UUID として検証してから埋め込む
        return int(psql(args, db, sql.format(ids=lst)))
    hold = (count("SELECT count(*) FROM identities WHERE id IN ({ids}) AND disabled_at IS NULL", ids["identity"]) == 0
            and count("SELECT count(*) FROM payment_destination_versions v JOIN payment_destinations d "
                      "ON d.id = v.destination_id WHERE d.id IN ({ids}) AND v.ciphertext IS NOT NULL",
                      ids["destination"]) == 0
            and count("SELECT count(*) FROM groups WHERE id IN ({ids})", ids["group"]) == 0)
    return {"tombstones_reapplied": applied, "deletions_hold": applied and hold,
            "reapplied_counts": (json.loads(r.stdout.strip().splitlines()[-1]).get("reapplied") if applied else None)}


def cmd_restore(args: argparse.Namespace) -> int:
    src = Path(getattr(args, "from"))
    outer = json.loads((src / "manifest.json").read_text())
    if outer.get("format") == LEGACY_FORMAT:
        if is_production():
            raise Refused("本番では平文のバックアップから復元できません")
        return cmd_restore_plaintext(args, src, outer)
    if outer.get("format") != FORMAT:
        raise Refused(f"未対応のバックアップ形式です: {outer.get('format')}")
    guard_target(args, args.target_db)
    if db_exists(args, args.target_db):
        raise Refused(f"復元先 DB が既に存在します: {args.target_db}")
    target_dir = Path(args.receipts_target)
    if target_dir.exists() and any(target_dir.iterdir()):
        raise Refused(f"画像の復元先が空ではありません: {target_dir}")
    tomb = Path(args.tombstones) if getattr(args, "tombstones", None) else None
    if tomb is None or not tomb.is_file():
        raise Refused("DB の外の削除済み一覧(--tombstones)を指定してください。復元で削除が戻るのを防ぐため必須です")
    # 1. 鍵・manifest・全ファイルの認証を、DB に触れる前に済ませる
    try:
        inner, dek = open_encrypted(args, src, outer)
        problems = verify_encrypted(src, outer, inner, dek)
    except BackupCryptoError as e:
        problems, inner, dek = [str(e)], {}, b""
    if problems:
        print(json.dumps({"restored": False, "problems": problems}, ensure_ascii=False))
        return 1
    rows, file_latest = read_tombstones(tomb)
    if inner.get("tombstones_latest") and (file_latest is None or file_latest < inner["tombstones_latest"]):
        raise Refused("削除済み一覧がバックアップの時点より古いため、復元すると削除が戻るおそれがあります。"
                      "最新の一覧を指定してください")
    if inner["missing_images_at_backup"] and not getattr(args, "accept_missing_images", False):
        raise Refused(f"バックアップの時点で画像が {len(inner['missing_images_at_backup'])} 枚欠けています。"
                      "確認のうえ --accept-missing-images を付けてください")
    # 2. 新しい DB へ。復号した内容を平文のファイルに書かず pg_restore へ流す
    psql(args, "postgres", f'CREATE DATABASE "{args.target_db}"')
    proc = subprocess.Popen(["docker", "exec", "-i", args.container, "pg_restore", "-U", args.user, "-d",
                             args.target_db, "--no-owner", "--no-privileges", "--exit-on-error"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    with (src / enc_path("db.dump")).open("rb") as f:
        for chunk in decrypt_chunks(f, dek, outer["backup_id"], "db.dump", outer["files"]["db.dump"]["nonce_prefix"]):
            proc.stdin.write(chunk)
    proc.stdin.close()
    if proc.wait() != 0:
        print(json.dumps({"restored": False, "problems": ["pg_restore が失敗しました"]}, ensure_ascii=False))
        return 1
    target_dir.mkdir(parents=True, exist_ok=True)
    for name in inner["files"]:
        if name.startswith("receipts/"):
            with (src / enc_path(name)).open("rb") as f, (target_dir / name.split("/", 1)[1]).open("wb") as o:
                for chunk in decrypt_chunks(f, dek, outer["backup_id"], name, outer["files"][name]["nonce_prefix"]):
                    o.write(chunk)
    del dek
    # 3. 読み戻して検証し、削除を再適用する
    after = snapshot(args, args.target_db)
    restored = {p.name: sha256_file(p) for p in target_dir.iterdir() if p.is_file()}
    expected = {n.split("/", 1)[1]: m["plaintext_sha256"] for n, m in inner["files"].items()
                if n.startswith("receipts/")}
    fk_invalid = psql(args, args.target_db,
                      "SELECT count(*) FROM pg_constraint WHERE contype = 'f' AND NOT convalidated")
    checks = {
        "alembic_version": after["alembic_version"] == inner["alembic_version"],
        "row_counts": after["row_counts"] == inner["row_counts"],
        "image_checksums": restored == expected,
        "live_images_present": set(after["live_image_keys"]) <= set(restored),
        "foreign_keys_validated": fk_invalid == "0",
    }
    tomb_result = reapply_tombstones(args, args.target_db, tomb, rows)
    checks["tombstones_reapplied"] = tomb_result["tombstones_reapplied"]
    checks["deletions_hold"] = tomb_result["deletions_hold"]
    ok = all(checks.values())
    print(json.dumps({"restored": ok, "target_db": args.target_db, "checks": checks,
                      "reapplied": tomb_result["reapplied_counts"], "rows": sum(after["row_counts"].values()),
                      "images": len(restored)}, ensure_ascii=False))
    return 0 if ok else 1


def cmd_restore_plaintext(args: argparse.Namespace, src: Path, manifest: dict) -> int:
    """開発用の平文バックアップ(format warilink-backup-1)からの復元。削除済み一覧の再適用は暗号化版と同じく必須。"""
    guard_target(args, args.target_db)
    if db_exists(args, args.target_db):
        raise Refused(f"復元先 DB が既に存在します: {args.target_db}")
    target_dir = Path(args.receipts_target)
    if target_dir.exists() and any(target_dir.iterdir()):
        raise Refused(f"画像の復元先が空ではありません: {target_dir}")
    tomb = Path(args.tombstones) if getattr(args, "tombstones", None) else None
    if tomb is None or not tomb.is_file():
        raise Refused("DB の外の削除済み一覧(--tombstones)を指定してください。復元で削除が戻るのを防ぐため必須です")
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
    rows, _ = read_tombstones(tomb)
    tomb_result = reapply_tombstones(args, args.target_db, tomb, rows)
    checks["tombstones_reapplied"] = tomb_result["tombstones_reapplied"]
    checks["deletions_hold"] = tomb_result["deletions_hold"]
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
    """使い捨ての元 DB に API 経由でデータを入れ、暗号化 backup → 退会(削除済み一覧に載る)→ 一覧の書き出し →
    restore(削除の再適用)→ 検証 → アプリからの読み戻し → 片付け、まで行う。

    鍵は訓練のたびに乱数で作る使い捨て(FakeKmsProvider)。外部保管は作業ディレクトリ内のローカル保管先で模す。
    """
    stamp = time.strftime("%Y%m%d%H%M%S")
    src_db, dst_db = f"{RESTORE_PREFIX}src_{stamp}", f"{RESTORE_PREFIX}dst_{stamp}"
    work = Path(args.workdir)
    if work.exists() and any(work.iterdir()):
        raise Refused(f"作業ディレクトリが空ではありません: {work}")
    src_images, backup_dir, dst_images = work / "src_receipts", work / "backup", work / "dst_receipts"
    state, tomb = work / "drill-state.json", work / "tombstones.jsonl"
    src_images.mkdir(parents=True)
    rc = 1
    provider = FakeKmsProvider()
    try:
        guard_target(args, src_db)
        psql(args, "postgres", f'CREATE DATABASE "{src_db}"')
        url = f"postgresql+psycopg://{args.user}:{os.environ.get('WARILINK_DRILL_DB_PASSWORD', 'warilink-dev-only')}" \
              f"@localhost:{args.port}/{src_db}"
        # 種データにはレシート画像を含める(V0 の既定では OCR が無効なので、訓練の元 DB だけで有効にする)
        env = {**os.environ, "WARILINK_DATABASE_URL": url, "WARILINK_RECEIPT_STORAGE_DIR": str(src_images),
               "WARILINK_ENVIRONMENT": "test", "WARILINK_FEATURE_OCR": "true", "PYTHONPATH": str(ROOT / "apps" / "api"),
               "WARILINK_DRILL_STATE": str(state)}
        api = ROOT / "apps" / "api"
        py = str(ROOT / ".venv" / "bin" / "python")
        seed_py = str(ROOT / "scripts" / "warilink_backup_seed.py")

        def run(*extra: str, environ: dict | None = None) -> int:
            r = subprocess.run([py, seed_py, *extra], cwd=api, env=environ or env, capture_output=True, text=True,
                               check=False)
            print(r.stdout.strip() or r.stderr.strip()[-800:])
            return r.returncode

        subprocess.run([py, "-m", "alembic", "upgrade", "head"], cwd=api, env=env, check=True, capture_output=True)
        if run() != 0:
            return 1
        ns = argparse.Namespace(**{**vars(args), "db": src_db, "receipts_dir": str(src_images), "out": str(backup_dir),
                                   "provider": provider, "storage": LocalDirStorage(work / "offsite"),
                                   "insecure_plaintext": False})
        if cmd_backup(ns) != 0:
            return 1
        # バックアップの後に退会 → 削除済み一覧を DB の外へ書き出す(本番では定期ジョブが行う)
        if run("--delete-member") != 0:
            return 1
        exp = subprocess.run([py, "-m", "app.modules.identity.retention", "export-tombstones", "--out", str(tomb)],
                             cwd=api, env=env, capture_output=True, text=True, check=False)
        if exp.returncode != 0:
            print(exp.stderr.strip()[-400:])
            return 1
        ns = argparse.Namespace(**{**vars(args), "from": str(backup_dir), "target_db": dst_db,
                                   "receipts_target": str(dst_images), "provider": provider,
                                   "tombstones": str(tomb), "accept_missing_images": False})
        rc = cmd_restore(ns)
        if rc == 0:
            # 復元した DB をアプリから読めること(合計金額・残高の和)と、退会した人が戻っていないことを確かめる
            env2 = {**env, "WARILINK_DATABASE_URL": url.rsplit("/", 1)[0] + f"/{dst_db}",
                    "WARILINK_RECEIPT_STORAGE_DIR": str(dst_images)}
            rc = run("--verify", environ=env2)
    finally:
        for name in (src_db, dst_db):
            try:
                psql(args, "postgres", f'DROP DATABASE IF EXISTS "{name}"')
            except subprocess.CalledProcessError:
                print(json.dumps({"cleanup_failed": name}))
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
    print(json.dumps({"drill": "PASS" if rc == 0 else "FAIL", "encrypted": True, "key_provider": provider.name,
                      "dropped": [src_db, dst_db]}))
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
        if manifest.get("format") not in (LEGACY_FORMAT, FORMAT):
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
    b.add_argument("--insecure-plaintext", action="store_true", help="開発用の平文バックアップ(本番では拒否)")
    r = sub.add_parser("restore")
    r.add_argument("--from", required=True)
    r.add_argument("--target-db", required=True)
    r.add_argument("--receipts-target", required=True)
    r.add_argument("--tombstones", help="DB の外の削除済み一覧(必須)")
    r.add_argument("--accept-missing-images", action="store_true",
                   help="バックアップ時点で欠けていた画像を承知で復元する")
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
