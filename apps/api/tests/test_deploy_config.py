"""起動設定の一致(静的検査)。本番イメージ・本番 compose・開発 compose・E2E の uvicorn が同じ安全な引数で起動することと、
マイグレーションの扱いが環境ごとに正しいこと(STEP 03: 本番は起動に含めない)を確かめる。

uvicorn は既定で 127.0.0.1 からの X-Forwarded-For を信じて接続元を書き換え、アクセスログに URL(招待トークン)を残す。
どれか 1 か所でも引数が抜けると、回数制限の接続元の判定とログの扱いが環境ごとに変わる。
"""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
REQUIRED = ("--no-proxy-headers", "--no-access-log")


def _dockerfile_cmd() -> str:
    text = (ROOT / "apps/api/Dockerfile").read_text()
    (cmd,) = [ln for ln in text.splitlines() if ln.startswith("CMD ")]
    return " ".join(json.loads(cmd[4:]))


def _compose_api_command() -> str:
    text = (ROOT / "infra/compose.yaml").read_text()
    m = re.search(r"^  api:\n(?:    .*\n|\n)*?    command: (\[.*\])$", text, re.M)
    assert m, "infra/compose.yaml の api サービスに command がありません"
    return " ".join(json.loads(m.group(1)))


def _production() -> dict:
    import yaml  # noqa: PLC0415 - uvicorn[standard] に含まれる

    return yaml.safe_load((ROOT / "infra/compose.production.yaml").read_text())


def _production_api_command() -> str:
    return " ".join(_production()["services"]["api"]["command"])


def _playwright_api_command() -> str:
    text = (ROOT / "apps/web/playwright.config.ts").read_text()
    m = re.search(r"uvicorn app\.main:app[^`]*", text)
    assert m, "playwright.config.ts に API の起動コマンドがありません"
    return m.group(0)


@pytest.mark.parametrize("source", [_dockerfile_cmd, _compose_api_command, _production_api_command,
                                    _playwright_api_command],
                         ids=["Dockerfile", "compose", "compose.production", "playwright"])
def test_uvicorn_runs_with_safe_flags_everywhere(source):
    cmd = source()
    assert "uvicorn app.main:app" in cmd
    for flag in REQUIRED:
        assert flag in cmd, f"{source.__name__}: {flag} がありません"
    assert "--proxy-headers " not in f"{cmd} " and "--forwarded-allow-ips" not in cmd


def test_api_image_starts_without_migrating_on_port_8000_as_non_root():
    """STEP 03: イメージの既定の起動はマイグレーションしない(本番のアプリのロールには DDL の権限が無い)。"""
    text = (ROOT / "apps/api/Dockerfile").read_text()
    cmd = _dockerfile_cmd()
    assert cmd.startswith("uvicorn app.main:app ") and "alembic" not in cmd
    assert "--host 0.0.0.0 --port 8000" in cmd and "EXPOSE 8000" in text
    assert "--timeout-keep-alive 65" in cmd  # Next.js の中継の keep-alive(5 秒)より長く
    assert re.search(r"^USER app$", text, re.M)  # root で動かさない


def test_migration_runs_once_in_production_and_before_start_only_in_dev_and_e2e():
    prod = _production()["services"]
    api = _production_api_command()
    assert "upgrade" not in api  # 本番の API は起動時にマイグレーションしない
    assert "alembic current" in api and "(head)" in api and "exit 3" in api  # 最新でなければ起動しない
    assert api.index("(head)") < api.index("exec uvicorn")
    assert "--timeout-keep-alive 65" in api
    migrate = prod["migrate"]
    assert migrate["profiles"] == ["migrate"] and migrate["restart"] == "no"  # `run --rm migrate` で 1 回だけ
    assert " ".join(migrate["command"]).startswith("sh -c alembic upgrade head && ")
    assert "REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON alembic_version FROM warilink_app" in " ".join(
        migrate["command"])
    assert "MIGRATOR" in migrate["environment"]["WARILINK_DATABASE_URL"]
    assert "APP_DATABASE_URL" in prod["api"]["environment"]["WARILINK_DATABASE_URL"]
    # 開発と E2E は使い捨ての DB なので、従来どおりマイグレーションしてから起動する
    assert _compose_api_command().startswith("sh -c alembic upgrade head && uvicorn ")
    assert "alembic upgrade head && " in (ROOT / "apps/web/playwright.config.ts").read_text()
