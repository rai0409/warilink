#!/usr/bin/env python3
"""本番の起動前検査(STEP 03)。読み取りだけで、何も変更しない。秘密の値は出力しない(項目名と状態だけ)。

  scripts/warilink_preflight.py --env-file /etc/warilink/production.env [--check-db] [--db-host-override HOST:PORT]

確かめること:
- env ファイル: 必須の値があること、形式(ドメイン・接続先のロール名・秘密の長さ)、開発用の値でないこと
- API の本番設定: compose.production.yaml が API に渡す値で、app/core/config.py の本番の検査を通ること
- 無料版の範囲: 対象外の機能を env ファイルで有効にしようとしていないこと(compose は false に固定)
- 構成: compose.production.yaml で外へ出るのが proxy の 80/443 だけ・内部ネットワーク・API の起動にマイグレーションを
  含まない・信頼する中継が Web の固定アドレスだけ、Caddyfile が X-Forwarded-For を上書きし、アクセスログを出さないこと
- バックアップと削除ジャーナル: STEP 02 の外部保管(s3://)・鍵(KMS)の設定の形式。実接続は確かめない(live は BLOCKED)
- --check-db: アプリのロールの実際の属性(rolsuper など)と権限(DDL なし・DML だけ・alembic_version は読むだけ)、
  スキーマが最新であること。ユーザー名ではなく pg_roles と has_*_privilege で判断する

終了コード: 0 = すべて PASS / 1 = FAIL あり / 3 = FAIL は無いが確かめられない項目(BLOCKED)あり。
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))

COMPOSE = ROOT / "infra" / "compose.production.yaml"
CADDYFILE = ROOT / "infra" / "proxy" / "Caddyfile"
REQUIRED = ("WARILINK_DOMAIN", "WARILINK_APP_DATABASE_URL", "WARILINK_MIGRATOR_DATABASE_URL",
            "WARILINK_RATE_LIMIT_SECRET", "WARILINK_DELETION_JOURNAL", "POSTGRES_BOOTSTRAP_USER",
            "POSTGRES_BOOTSTRAP_PASSWORD", "WARILINK_BACKUP_OFFSITE")
OUT_OF_SCOPE = ("PASSKEYS", "PAYMENT_DESTINATIONS", "OCR", "ADS", "BILLING", "BANK_PROVIDER")
ROLES = {"app": "warilink_app", "migrator": "warilink_migrator"}
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


class Report:
    def __init__(self) -> None:
        self.checks: list[dict[str, str]] = []

    def add(self, name: str, status: str, detail: str = "") -> None:
        assert status in ("PASS", "FAIL", "BLOCKED")
        self.checks.append({"name": name, "status": status, "detail": detail})

    def result(self) -> str:
        statuses = {c["status"] for c in self.checks}
        return "FAIL" if "FAIL" in statuses else "BLOCKED" if "BLOCKED" in statuses else "PASS"


def parse_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE の行だけ(シェルの展開はしない。compose の env ファイルと同じ扱い)。"""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k.strip()] = v
    return out


_VAR = re.compile(r"\$\{([A-Z0-9_]+)(?::([?-])([^}]*))?\}")


def interpolate(value: str, env: dict[str, str]) -> str:
    """compose の ${VAR} / ${VAR:-既定} / ${VAR:?説明} を展開する(必須が無ければ KeyError)。"""
    def sub(m: re.Match) -> str:
        name, op, arg = m.group(1), m.group(2), m.group(3) or ""
        v = env.get(name, "")
        if v:
            return v
        if op == "?":
            raise KeyError(name)
        return arg if op == "-" else ""
    return _VAR.sub(sub, value)


def load_compose() -> dict:
    import yaml  # noqa: PLC0415 - uvicorn[standard] に含まれる

    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def api_environment(compose: dict, env: dict[str, str], service: str = "api") -> dict[str, str]:
    raw = compose["services"][service]["environment"]
    return {k: interpolate(str(v), env) for k, v in raw.items()}


@contextmanager
def patched_environ(values: dict[str, str]) -> Iterator[None]:
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(values)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


# ---------------------------------------------------------------- 検査


def check_env(env: dict[str, str], r: Report) -> None:
    missing = [k for k in REQUIRED if not env.get(k)]
    r.add("env.required", "FAIL" if missing else "PASS", ("未設定: " + ", ".join(missing)) if missing else "")
    if not (env.get("WARILINK_BACKUP_KMS_KEY_ID") or env.get("WARILINK_BACKUP_KEYS")):
        r.add("env.backup_key", "FAIL", "WARILINK_BACKUP_KMS_KEY_ID(または WARILINK_BACKUP_KEYS)が未設定")
    else:
        r.add("env.backup_key", "PASS", "kms" if env.get("WARILINK_BACKUP_KMS_KEY_ID") else "env")
    domain = env.get("WARILINK_DOMAIN", "")
    ok = bool(DOMAIN_RE.fullmatch(domain)) and domain not in ("localhost",) and not domain.endswith(".localhost")
    r.add("env.domain", "PASS" if ok else "FAIL",
          "" if ok else "WARILINK_DOMAIN はスキーム・ポートの無いホスト名にしてください")
    enabled = [f for f in OUT_OF_SCOPE if env.get(f"WARILINK_FEATURE_{f}", "").lower() in ("1", "true", "yes", "on")]
    r.add("env.free_release_scope", "FAIL" if enabled else "PASS",
          ("無料版の対象外を有効にしようとしています: " + ", ".join(enabled)) if enabled else "compose で false に固定")
    from sqlalchemy.engine import make_url  # noqa: PLC0415

    users, passwords, problems = {}, set(), []
    for key, role in (("WARILINK_APP_DATABASE_URL", "app"), ("WARILINK_MIGRATOR_DATABASE_URL", "migrator")):
        try:
            u = make_url(env.get(key, ""))
        except Exception:  # noqa: BLE001 - 値はメッセージに出さない
            problems.append(f"{key} の形式が不正")
            continue
        users[role] = u.username
        passwords.add(u.password or "")
        if u.username != ROLES[role]:
            problems.append(f"{key} のユーザーは {ROLES[role]} にしてください")
        if u.host != "db":
            problems.append(f"{key} の接続先は compose の db にしてください")
        if not u.password or len(u.password) < 24 or "warilink-dev-only" in (u.password or ""):
            problems.append(f"{key} のパスワードは 24 文字以上の開発用でない値にしてください")
    if len(passwords) < 2:
        problems.append("アプリとマイグレーションのパスワードを別にしてください")
    if env.get("POSTGRES_BOOTSTRAP_USER") in users.values():
        problems.append("ブートストラップの管理者をアプリ・マイグレーションに使わないでください")
    if len(env.get("POSTGRES_BOOTSTRAP_PASSWORD", "")) < 24:
        problems.append("POSTGRES_BOOTSTRAP_PASSWORD は 24 文字以上にしてください")
    r.add("env.database_roles", "FAIL" if problems else "PASS", " / ".join(problems))


def check_api_settings(compose: dict, env: dict[str, str], r: Report) -> None:
    try:
        values = api_environment(compose, env)
    except KeyError as e:
        r.add("api.production_settings", "FAIL", f"compose の必須の値が未設定: {e.args[0]}")
        return
    from app.core import config as cfg  # noqa: PLC0415

    with patched_environ(values):
        try:
            s = cfg.Settings(_env_file=None)
            problems = cfg.production_problems(s)
        except Exception as e:  # noqa: BLE001 - pydantic の検証エラー(値は出さない)
            problems = [f"設定を読めません({type(e).__name__})"]
    r.add("api.production_settings", "FAIL" if problems else "PASS", " / ".join(problems))
    origins = json.loads(values["WARILINK_ALLOWED_ORIGINS"])
    ok = origins == [f"https://{env.get('WARILINK_DOMAIN', '')}"]
    r.add("api.origin_https", "PASS" if ok else "FAIL",
          "" if ok else "許可する Origin が https://WARILINK_DOMAIN だけではありません")


def check_compose(compose: dict, r: Report) -> None:
    svcs, nets = compose["services"], compose.get("networks", {})
    published = {name: s.get("ports", []) for name, s in svcs.items() if s.get("ports")}
    ok = set(published) == {"proxy"} and set(published["proxy"]) <= {"80:80", "443:443", "443:443/udp"}
    r.add("compose.published_ports", "PASS" if ok else "FAIL",
          "proxy の 80/443 だけ" if ok else "proxy の 80/443 以外を公開しています")
    internal = {n for n, v in nets.items() if (v or {}).get("internal")}
    bad = [name for name in ("web", "api", "db", "migrate")
           if not set(svcs[name].get("networks", {})) <= internal]
    r.add("compose.internal_networks", "FAIL" if bad else "PASS",
          ("内部でないネットワークにいます: " + ", ".join(bad)) if bad else "")
    api_cmd = " ".join(svcs["api"].get("command", []))
    problems = []
    if "upgrade" in api_cmd:
        problems.append("API の起動にマイグレーションが含まれています")
    if "(head)" not in api_cmd:
        problems.append("API がスキーマの最新を確かめずに起動します")
    for flag in ("--no-proxy-headers", "--no-access-log"):
        if flag not in api_cmd:
            problems.append(f"API の起動に {flag} がありません")
    if "migrate" not in svcs["migrate"].get("profiles", []) or "alembic upgrade head" not in \
            " ".join(svcs["migrate"].get("command", [])):
        problems.append("migrate が 1 回だけ実行する構成になっていません")
    r.add("compose.migration_separated", "FAIL" if problems else "PASS", " / ".join(problems))
    trusted = json.loads(compose["x-api-env"]["WARILINK_TRUSTED_PROXIES"])
    web_ip = svcs["web"]["networks"]["web_api"]["ipv4_address"]
    ok = trusted == [f"{web_ip}/32"] and ipaddress.ip_address(web_ip).is_private
    r.add("compose.trusted_proxy", "PASS" if ok else "FAIL",
          "Web の固定アドレスだけ" if ok else "信頼する中継が Web の固定アドレスだけではありません")
    users = []
    for svc, dockerfile in (("api", ROOT / "apps/api/Dockerfile"), ("web", ROOT / "apps/web/Dockerfile")):
        if not re.search(r"^USER (?!root\b)\S+$", dockerfile.read_text(), re.M):
            users.append(svc)
    r.add("compose.non_root", "FAIL" if users else "PASS", ("root で動きます: " + ", ".join(users)) if users else "")


def check_caddy(r: Report) -> None:
    text = CADDYFILE.read_text(encoding="utf-8")
    body = "\n".join(ln.split("#", 1)[0] for ln in text.splitlines())
    problems = []
    if "header_up X-Forwarded-For {remote_host}" not in body:
        problems.append("X-Forwarded-For を接続元で上書きしていません")
    if re.search(r"^\s*log\b", body, re.M):
        problems.append("アクセスログを出しています(招待トークンが残る)")
    if "trusted_proxies" in body:
        problems.append("入口の手前に信頼する中継を設定しています")
    if "reverse_proxy web:3000" not in body or "api:8000" in body:
        problems.append("Web 以外へ中継しています")
    if "{$WARILINK_DOMAIN}" not in body:
        problems.append("サイトのアドレスが WARILINK_DOMAIN ではありません")
    r.add("caddy.edge", "FAIL" if problems else "PASS", " / ".join(problems))


def check_backup_contract(env: dict[str, str], r: Report) -> None:
    from app.ops.deletion_journal import JournalError, journal_from_url  # noqa: PLC0415
    from app.ops.s3_storage import parse_s3_url  # noqa: PLC0415

    journal = env.get("WARILINK_DELETION_JOURNAL", "")
    try:
        journal_from_url(journal)
        if journal.startswith("s3://"):
            r.add("backup.deletion_journal", "PASS", "s3")
        else:
            r.add("backup.deletion_journal", "BLOCKED", "local: は同じ VM の全損に耐えない(s3:// を推奨)")
    except JournalError as e:
        r.add("backup.deletion_journal", "FAIL", str(e))
    offsite = env.get("WARILINK_BACKUP_OFFSITE", "")
    try:
        parse_s3_url(offsite)
        r.add("backup.offsite", "PASS", "s3")
    except Exception:  # noqa: BLE001
        r.add("backup.offsite", "FAIL", "WARILINK_BACKUP_OFFSITE は s3://バケット/接頭辞 にしてください")
    r.add("backup.live", "BLOCKED", "S3・KMS への実接続は確かめていない(STEP 02 の live 検証まで)")


def check_db(env: dict[str, str], r: Report, host_override: str | None) -> None:
    from sqlalchemy import create_engine, text  # noqa: PLC0415
    from sqlalchemy.engine import make_url  # noqa: PLC0415

    url = make_url(env.get("WARILINK_APP_DATABASE_URL", ""))
    if host_override:
        host, _, port = host_override.partition(":")
        url = url.set(host=host, port=int(port) if port else None)
    try:
        engine = create_engine(url, pool_pre_ping=False)
        with engine.connect() as c:
            role = c.execute(text(
                "SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls, rolreplication, rolinherit "
                "FROM pg_roles WHERE rolname = current_user")).one()
            schema_create = c.execute(text("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")).scalar()
            db_create = c.execute(text(
                "SELECT has_database_privilege(current_user, current_database(), 'CREATE')")).scalar()
            tables = c.execute(text(
                # 列名に t などの短い名前を使わない(SQLAlchemy の Row の属性 .t と衝突する)
                "SELECT c.relname, pg_get_userbyid(c.relowner) = current_user AS owned, "
                "has_table_privilege(c.oid, 'SELECT') AS can_select, "
                "has_table_privilege(c.oid, 'INSERT') AS can_insert, "
                "has_table_privilege(c.oid, 'UPDATE') AS can_update, "
                "has_table_privilege(c.oid, 'DELETE') AS can_delete, "
                "has_table_privilege(c.oid, 'TRUNCATE') AS can_truncate, "
                "has_table_privilege(c.oid, 'REFERENCES') AS can_reference, "
                "has_table_privilege(c.oid, 'TRIGGER') AS can_trigger "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'r'")).all()
            revision = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception as e:  # noqa: BLE001 - 接続文字列を含みうるので種類だけ
        r.add("db.app_role", "BLOCKED", f"DB に接続できません({type(e).__name__})")
        return
    attrs = [n for n, v in zip(("SUPERUSER", "CREATEDB", "CREATEROLE", "BYPASSRLS", "REPLICATION"), role[:5],
                               strict=True) if v]
    r.add("db.app_role_attributes", "FAIL" if attrs else "PASS",
          ("アプリのロールに不要な属性: " + ", ".join(attrs)) if attrs else "")
    problems = []
    if schema_create or db_create:
        problems.append("アプリのロールが DDL(スキーマ・DB の作成)をできます")
    for t in tables:
        if t.owned:
            problems.append(f"{t.relname} の所有者がアプリのロールです")
        if t.can_truncate or t.can_reference or t.can_trigger:
            problems.append(f"{t.relname} に TRUNCATE / REFERENCES / TRIGGER の権限があります")
        if t.relname == "alembic_version":
            if not t.can_select or t.can_insert or t.can_update or t.can_delete:
                problems.append("alembic_version は読むだけにしてください")
        elif not (t.can_select and t.can_insert and t.can_update and t.can_delete):
            problems.append(f"{t.relname} の DML の権限が足りません")
    r.add("db.app_privileges", "FAIL" if problems else "PASS", " / ".join(problems[:10]))
    from alembic.config import Config  # noqa: PLC0415
    from alembic.script import ScriptDirectory  # noqa: PLC0415

    cfg = Config(str(ROOT / "apps/api/alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "apps/api/alembic"))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    r.add("db.schema_head", "PASS" if revision == head else "FAIL",
          "" if revision == head else "スキーマが最新ではありません(migrate を実行してください)")


def run(env_file: Path | None, *, check_database: bool = False, db_host_override: str | None = None) -> dict:
    r = Report()
    if env_file is None or not env_file.is_file():
        r.add("env.file", "BLOCKED", "本番の env ファイルがありません(導入先が未決定)")
        env: dict[str, str] = {}
    else:
        mode = env_file.stat().st_mode & 0o077
        r.add("env.file", "FAIL" if mode else "PASS", "env ファイルの権限は 600 にしてください" if mode else "")
        env = parse_env_file(env_file)
    compose = load_compose()
    check_compose(compose, r)
    check_caddy(r)
    if env:
        check_env(env, r)
        check_api_settings(compose, env, r)
        check_backup_contract(env, r)
        if check_database:
            check_db(env, r, db_host_override)
        else:
            r.add("db.app_role", "BLOCKED", "--check-db を付けていない(実 DB の権限は未確認)")
    else:
        for name in ("env.required", "api.production_settings", "backup.offsite", "db.app_role"):
            r.add(name, "BLOCKED", "本番の env ファイルが無いため確かめられない")
    return {"preflight": r.result(), "checks": r.checks}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="warilink_preflight.py")
    p.add_argument("--env-file", help="本番の env ファイル(権限 600。リポジトリに置かない)")
    p.add_argument("--check-db", action="store_true",
                   help="アプリのロールで DB に接続し、属性と権限を読み取り専用で確かめる")
    p.add_argument("--db-host-override", help="DB に届くホスト:ポート(SSH の転送など。既定は env ファイルの db)")
    a = p.parse_args(argv)
    out = run(Path(a.env_file) if a.env_file else None, check_database=a.check_db, db_host_override=a.db_host_override)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return {"PASS": 0, "FAIL": 1, "BLOCKED": 3}[out["preflight"]]


if __name__ == "__main__":
    sys.exit(main())
