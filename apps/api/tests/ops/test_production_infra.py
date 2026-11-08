"""STEP 03: 本番の構成(compose.production.yaml・Caddyfile・DB のロール)と起動前検査(scripts/warilink_preflight.py)。

静的な検査と、送信元 IP の受け渡し(Caddy → Next.js → FastAPI)の再現は DB も Docker も使わない。
実 DB のロールの検査(最後のテスト)は、専用の使い捨てコンテナ(名前が warilink-roles-test- で始まるもの)を
WARILINK_ROLE_TEST_CONTAINER で指定したときだけ動く。既存の DB では動かない。
"""

import importlib.util
import json
import os
import re
import secrets
import subprocess
from pathlib import Path

import pytest
import yaml
from starlette.requests import Request

from app.core import security
from app.core.config import get_settings

ROOT = Path(__file__).resolve().parents[4]
COMPOSE = ROOT / "infra/compose.production.yaml"
PREFLIGHT = ROOT / "scripts/warilink_preflight.py"


@pytest.fixture(scope="module")
def prod() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


@pytest.fixture
def pf():
    spec = importlib.util.spec_from_file_location("warilink_preflight_under_test", PREFLIGHT)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ---------------------------------------------------------------- 構成(静的)


def test_only_the_proxy_is_published_and_everything_else_is_internal(prod):
    svcs, nets = prod["services"], prod["networks"]
    assert {n for n, s in svcs.items() if "ports" in s} == {"proxy"}
    assert set(svcs["proxy"]["ports"]) == {"80:80", "443:443", "443:443/udp"}
    assert nets["public"] == {} and all(nets[n]["internal"] for n in ("proxy_web", "web_api", "api_db"))
    assert set(svcs["proxy"]["networks"]) == {"public", "proxy_web"}  # 証明書の取得のため外へ出られるのは proxy だけ
    assert set(svcs["web"]["networks"]) == {"proxy_web", "web_api"}
    assert set(svcs["api"]["networks"]) == {"web_api", "api_db"}
    assert set(svcs["db"]["networks"]) == {"api_db"} and "ports" not in svcs["db"]
    assert "ocr-worker" not in svcs  # 初回の無料版では置かない
    assert prod["name"] == "warilink-prod"  # 開発(warilink)とプロジェクト名・ボリュームを分ける


def test_required_values_fail_closed_and_out_of_scope_features_are_fixed_off(prod):
    text = COMPOSE.read_text()
    for var in ("WARILINK_DOMAIN", "WARILINK_APP_DATABASE_URL", "WARILINK_RATE_LIMIT_SECRET",
                "WARILINK_DELETION_JOURNAL", "POSTGRES_BOOTSTRAP_USER", "POSTGRES_BOOTSTRAP_PASSWORD"):
        assert re.search(rf"\$\{{{var}:\?", text), var  # 未設定なら compose が拒否する
    env = prod["x-api-env"]
    for f in ("PASSKEYS", "PAYMENT_DESTINATIONS", "OCR", "ADS", "BILLING", "BANK_PROVIDER"):
        assert env[f"WARILINK_FEATURE_{f}"] == "false"  # env ファイルで上書きできない(値を直接書く)
    assert env["WARILINK_ENVIRONMENT"] == "production" and env["WARILINK_COOKIE_SECURE"] == "true"
    assert "warilink-dev-only" not in text and "localhost" not in text and "http://" not in text.replace(
        "http://api:8000", "")


def test_containers_are_hardened_and_non_root(prod):
    svcs = prod["services"]
    for name in ("proxy", "web", "api", "migrate"):
        assert svcs[name]["cap_drop"] == ["ALL"] and "no-new-privileges:true" in svcs[name]["security_opt"]
    assert svcs["web"]["read_only"] and svcs["api"]["read_only"]
    assert re.search(r"^USER app$", (ROOT / "apps/api/Dockerfile").read_text(), re.M)
    assert re.search(r"^USER node$", (ROOT / "apps/web/Dockerfile").read_text(), re.M)


def test_caddy_overwrites_forwarded_for_and_keeps_no_access_log():
    text = (ROOT / "infra/proxy/Caddyfile").read_text()
    body = "\n".join(ln.split("#", 1)[0] for ln in text.splitlines())
    assert "header_up X-Forwarded-For {remote_host}" in body
    assert "reverse_proxy web:3000" in body and "api:" not in body and "db:" not in body
    assert not re.search(r"^\s*log\b", body, re.M) and "trusted_proxies" not in body
    assert 'Referrer-Policy "no-referrer"' in body and "Strict-Transport-Security" in body


def test_roles_sql_separates_privileges_and_reads_secrets_from_the_environment():
    sql = (ROOT / "infra/ops/postgres-roles.sql").read_text()
    code = "\n".join(ln for ln in sql.splitlines() if not ln.lstrip().startswith("--"))
    for role in ("warilink_migrator", "warilink_app", "warilink_backup"):
        m = re.search(rf"CREATE ROLE {role} ([^;]+);", code)
        assert m, role
        for attr in ("NOSUPERUSER", "NOCREATEDB", "NOCREATEROLE", "NOBYPASSRLS", "NOREPLICATION"):
            assert attr in m.group(1), (role, attr)
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO warilink_app" in code
    assert "REVOKE ALL ON SCHEMA public FROM PUBLIC" in code and "OWNER warilink_migrator" in code
    assert not re.search(r"GRANT\s+(ALL|CREATE)[^;]*warilink_app", code)
    assert "PASSWORD '" not in code and all(f"\\getenv {v}" in code for v in (
        "migrator_password WARILINK_MIGRATOR_PASSWORD", "app_password WARILINK_APP_PASSWORD"))


# ---------------------------------------------------------------- 送信元 IP(Caddy → Next.js → FastAPI)


def _request(peer: str, xff: str | None) -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers, "client": (peer, 1234),
                    "query_string": b""})


def test_client_ip_through_the_production_chain(prod, monkeypatch):
    """Caddy は X-Forwarded-For を接続元で上書きし、Next.js(16)はそれを足し直さずに API へ渡す
    (next/dist/server/base-server.js: x-forwarded-for ??=)。API は Web の固定アドレスからの接続のときだけ使う。"""
    trusted = json.loads(prod["x-api-env"]["WARILINK_TRUSTED_PROXIES"])
    web_ip = prod["services"]["web"]["networks"]["web_api"]["ipv4_address"]
    monkeypatch.setattr(get_settings(), "trusted_proxies", trusted)
    # 利用者 198.51.100.7 が偽の X-Forwarded-For を付けても、Caddy が 198.51.100.7 に上書きする
    assert security.client_ip(_request(web_ip, "198.51.100.7")) == "198.51.100.7"
    # 利用者ごとに別の IP として数える(全員を Web の IP として数えない)
    assert security.client_ip(_request(web_ip, "192.0.2.44")) == "192.0.2.44"
    # 内部の別のコンテナ(信頼していない)からの X-Forwarded-For は使わない
    assert security.client_ip(_request("172.30.2.3", "198.51.100.7")) == "172.30.2.3"
    # 仮に入口を通らず偽の連鎖が届いても、右から見て最初の信頼していない値(Caddy の上書きが無ければ意味がない)
    assert security.client_ip(_request(web_ip, "1.1.1.1, 198.51.100.7")) == "198.51.100.7"


# ---------------------------------------------------------------- 起動前検査


def _good_env(tmp_path: Path, **over) -> tuple[Path, dict[str, str]]:
    s = {k: secrets.token_urlsafe(24) for k in ("app", "mig", "boot", "rate")}
    env = {
        "WARILINK_DOMAIN": "warilink.example.com",
        "WARILINK_APP_DATABASE_URL": f"postgresql+psycopg://warilink_app:{s['app']}@db:5432/warilink",
        "WARILINK_MIGRATOR_DATABASE_URL": f"postgresql+psycopg://warilink_migrator:{s['mig']}@db:5432/warilink",
        "WARILINK_RATE_LIMIT_SECRET": s["rate"] + s["rate"],
        "WARILINK_DELETION_JOURNAL": "s3://warilink-journal-prod/deletions",
        "WARILINK_BACKUP_OFFSITE": "s3://warilink-backup-prod/daily",
        "WARILINK_BACKUP_KMS_KEY_ID": "alias/warilink-backup",
        "POSTGRES_BOOTSTRAP_USER": "warilink_bootstrap",
        "POSTGRES_BOOTSTRAP_PASSWORD": s["boot"],
        **over,
    }
    env = {k: v for k, v in env.items() if v is not None}
    p = tmp_path / "production.env"
    p.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    p.chmod(0o600)
    return p, env


def _status(out: dict) -> dict[str, str]:
    return {c["name"]: c["status"] for c in out["checks"]}


def test_preflight_without_production_env_is_blocked_not_pass(pf):
    out = pf.run(None)
    assert out["preflight"] == "BLOCKED" and _status(out)["env.file"] == "BLOCKED"
    assert _status(out)["compose.published_ports"] == "PASS"


def test_preflight_passes_static_checks_and_never_prints_secrets(pf, tmp_path, capsys):
    path, env = _good_env(tmp_path)
    assert pf.main(["--env-file", str(path)]) == 3  # DB と S3・KMS の実接続は未確認 = BLOCKED
    raw = capsys.readouterr().out
    out = json.loads(raw)
    st = _status(out)
    assert {k for k, v in st.items() if v == "FAIL"} == set()
    assert st["api.production_settings"] == "PASS" and st["env.database_roles"] == "PASS"
    assert st["backup.live"] == "BLOCKED" and st["db.app_role"] == "BLOCKED"
    for k, v in env.items():
        if "PASSWORD" in k or "SECRET" in k or "DATABASE_URL" in k:
            secret = v.split(":")[-1].split("@")[0] if "DATABASE_URL" in k else v
            assert secret not in raw, k


@pytest.mark.parametrize("over, check", [
    ({"WARILINK_DOMAIN": "localhost"}, "env.domain"),
    ({"WARILINK_DOMAIN": "https://warilink.example.com"}, "env.domain"),
    ({"WARILINK_RATE_LIMIT_SECRET": None}, "env.required"),
    ({"WARILINK_APP_DATABASE_URL": "postgresql+psycopg://postgres:x" + "y" * 30 + "@db:5432/warilink"},
     "env.database_roles"),
    ({"WARILINK_APP_DATABASE_URL": "postgresql+psycopg://warilink_app:warilink-dev-only@db:5432/warilink"},
     "env.database_roles"),
    ({"POSTGRES_BOOTSTRAP_USER": "warilink_app"}, "env.database_roles"),
    ({"WARILINK_FEATURE_OCR": "true"}, "env.free_release_scope"),
    ({"WARILINK_FEATURE_BANK_PROVIDER": "1"}, "env.free_release_scope"),
    ({"WARILINK_DELETION_JOURNAL": "local:relative"}, "backup.deletion_journal"),
    ({"WARILINK_BACKUP_OFFSITE": "local:/srv/backups"}, "backup.offsite"),
    ({"WARILINK_RATE_LIMIT_SECRET": "short"}, "api.production_settings"),
])
def test_preflight_fails_closed_on_unsafe_values(pf, tmp_path, over, check):
    path, _ = _good_env(tmp_path, **over)
    out = pf.run(path)
    assert out["preflight"] == "FAIL" and _status(out)[check] == "FAIL", out


def test_preflight_rejects_a_readable_env_file_and_a_local_journal(pf, tmp_path):
    path, _ = _good_env(tmp_path, WARILINK_DELETION_JOURNAL="local:/srv/warilink-journal")
    path.chmod(0o644)
    st = _status(pf.run(path))
    assert st["env.file"] == "FAIL" and st["backup.deletion_journal"] == "BLOCKED"


def test_preflight_detects_a_weakened_compose_or_caddyfile(pf, tmp_path, monkeypatch):
    bad = COMPOSE.read_text().replace("    networks:\n      - web_api\n      - api_db\n",
                 "    ports:\n      - \"8000:8000\"\n    networks:\n      - web_api\n      - api_db\n", 1) \
        .replace("alembic current 2>/dev/null | grep -q '(head)'", "alembic upgrade head && true")
    (tmp_path / "compose.yaml").write_text(bad)
    caddy = (ROOT / "infra/proxy/Caddyfile").read_text().replace("header_up X-Forwarded-For {remote_host}\n", "") \
        + "\nlog\n"
    (tmp_path / "Caddyfile").write_text(caddy)
    monkeypatch.setattr(pf, "COMPOSE", tmp_path / "compose.yaml")
    monkeypatch.setattr(pf, "CADDYFILE", tmp_path / "Caddyfile")
    st = _status(pf.run(None))
    assert st["compose.published_ports"] == "FAIL" and st["compose.migration_separated"] == "FAIL"
    assert st["caddy.edge"] == "FAIL"


# ---------------------------------------------------------------- 実 DB のロール(専用の使い捨てコンテナだけ)

ROLE_CONTAINER = os.environ.get("WARILINK_ROLE_TEST_CONTAINER", "")
ROLE_PORT = os.environ.get("WARILINK_ROLE_TEST_PORT", "")


@pytest.mark.skipif(not ROLE_CONTAINER.startswith("warilink-roles-test-") or not ROLE_PORT.isdigit(),
                    reason="専用の使い捨てコンテナ(warilink-roles-test-*)を指定したときだけ実行する")
def test_roles_on_an_isolated_postgres(pf, tmp_path):
    """使い捨てのコンテナで: ロール作成 → migrator でマイグレーション → アプリのロールの属性と権限 → DDL できない →
    API がアプリのロールで動く。ブートストラップのユーザーは warilink_bootstrap(POSTGRES_USER)を想定。"""
    assert ROLE_CONTAINER not in ("warilink-db-1", "hakobu-postgres")
    pw = {k: secrets.token_urlsafe(24) for k in ("mig", "app", "bak")}
    env = {**os.environ, "WARILINK_DB_NAME": "warilink", "WARILINK_MIGRATOR_PASSWORD": pw["mig"],
           "WARILINK_APP_PASSWORD": pw["app"], "WARILINK_BACKUP_PASSWORD": pw["bak"]}
    sql = (ROOT / "infra/ops/postgres-roles.sql").read_bytes()
    r = subprocess.run(["docker", "exec", "-i", "-e", "WARILINK_DB_NAME", "-e", "WARILINK_MIGRATOR_PASSWORD",
                        "-e", "WARILINK_APP_PASSWORD", "-e", "WARILINK_BACKUP_PASSWORD", ROLE_CONTAINER,
                        "psql", "-v", "ON_ERROR_STOP=1", "-U", "warilink_bootstrap", "-d", "postgres", "-q"],
                       input=sql, env=env, capture_output=True, check=False)
    assert r.returncode == 0, r.stderr.decode()[-500:]
    local = f"localhost:{ROLE_PORT}"
    mig = f"postgresql+psycopg://warilink_migrator:{pw['mig']}@{local}/warilink"
    app = f"postgresql+psycopg://warilink_app:{pw['app']}@{local}/warilink"
    api_dir = ROOT / "apps/api"
    py = str(ROOT / ".venv/bin/python")
    base = {k: v for k, v in os.environ.items() if not k.startswith("WARILINK_")}
    # 1. アプリのロールではマイグレーションできない
    r = subprocess.run([py, "-m", "alembic", "upgrade", "head"], cwd=api_dir, capture_output=True, text=True,
                       env={**base, "WARILINK_DATABASE_URL": app, "WARILINK_ENVIRONMENT": "development"}, check=False)
    assert r.returncode != 0 and "permission denied" in r.stderr.lower()
    # 2. migrator でマイグレーションし、alembic_version への書き込みをアプリから外す(compose の migrate と同じ)
    r = subprocess.run([py, "-m", "alembic", "upgrade", "head"], cwd=api_dir, capture_output=True, text=True,
                       env={**base, "WARILINK_DATABASE_URL": mig, "WARILINK_ENVIRONMENT": "development"}, check=False)
    assert r.returncode == 0, r.stderr[-500:]
    from sqlalchemy import create_engine, text  # noqa: PLC0415

    with create_engine(mig).connect() as c:
        c.execute(text("REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON alembic_version FROM warilink_app"))
        c.commit()
    # 3. 起動前検査(実 DB の属性と権限)
    path, _ = _good_env(tmp_path, WARILINK_APP_DATABASE_URL=app.replace(local, "db:5432"),
                        WARILINK_MIGRATOR_DATABASE_URL=mig.replace(local, "db:5432"))
    st = _status(pf.run(path, check_database=True, db_host_override=local))
    assert st["db.app_role_attributes"] == "PASS" and st["db.app_privileges"] == "PASS"
    assert st["db.schema_head"] == "PASS"
    # 4. アプリのロールは DDL・TRUNCATE・alembic_version の書き換えができない
    with create_engine(app).connect() as c:
        for stmt in ("CREATE TABLE x (id int)", "TRUNCATE groups", "UPDATE alembic_version SET version_num = 'x'",
                     "DROP TABLE groups", "ALTER TABLE groups ADD COLUMN x int", "CREATE SCHEMA evil"):
            with pytest.raises(Exception, match="(?i)permission denied|must be owner"):
                c.execute(text(stmt))
            c.rollback()
    # 5. API はアプリのロールで動く(ゲスト作成・グループ作成・支出の登録)
    smoke = (
        "from fastapi.testclient import TestClient\n"
        "from app.main import app\n"
        "c = TestClient(app, headers={'X-WariLink-CSRF': '1'})\n"
        "assert c.post('/api/v1/identity/guest').status_code == 201\n"
        "g = c.post('/api/v1/groups', headers={'Idempotency-Key': 'k-roles-1'}, json={'title': 't', "
        "'my_name': 'A', 'other_member_names': ['B']}).json()\n"
        "ids = [m['id'] for m in g['members']]\n"
        "body = {'title': 'x', 'amount_minor': 1000, 'payers': [{'member_id': ids[0], 'amount_minor': 1000}], "
        "'split': {'mode': 'equal', 'shares': [{'member_id': m, 'value': 1} for m in ids]}}\n"
        "r = c.post(f\"/api/v1/groups/{g['public_id']}/expenses\", headers={'Idempotency-Key': 'k-roles-2'},\n"
        "           json=body)\n"
        "print(r.status_code)\n"
    )
    r = subprocess.run([py, "-c", smoke], cwd=api_dir, capture_output=True, text=True, check=False,
                       env={**base, "WARILINK_DATABASE_URL": app, "WARILINK_ENVIRONMENT": "development"})
    assert r.returncode == 0 and r.stdout.strip().splitlines()[-1] == "201", (r.stdout[-300:], r.stderr[-800:])
    # 起動前検査は、スーパーユーザーで接続する設定を FAIL にする
    root_url = app.replace("warilink_app:" + pw["app"], "warilink_bootstrap:" + os.environ.get(
        "WARILINK_ROLE_TEST_BOOTSTRAP_PASSWORD", ""))
    su_dir = tmp_path / "superuser"
    su_dir.mkdir()
    path2, _ = _good_env(su_dir, WARILINK_APP_DATABASE_URL=root_url.replace(local, "db:5432"))
    st = _status(pf.run(path2, check_database=True, db_host_override=local))
    assert st["env.database_roles"] == "FAIL" and st["db.app_role_attributes"] == "FAIL"
