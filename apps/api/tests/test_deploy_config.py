"""起動設定の一致(静的検査)。本番イメージ・compose・E2E の uvicorn が同じ安全な引数で起動することを確かめる。

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


def _playwright_api_command() -> str:
    text = (ROOT / "apps/web/playwright.config.ts").read_text()
    m = re.search(r"uvicorn app\.main:app[^`]*", text)
    assert m, "playwright.config.ts に API の起動コマンドがありません"
    return m.group(0)


@pytest.mark.parametrize("source", [_dockerfile_cmd, _compose_api_command, _playwright_api_command],
                         ids=["Dockerfile", "compose", "playwright"])
def test_uvicorn_runs_with_safe_flags_everywhere(source):
    cmd = source()
    assert "uvicorn app.main:app" in cmd
    for flag in REQUIRED:
        assert flag in cmd, f"{source.__name__}: {flag} がありません"
    assert "--proxy-headers " not in f"{cmd} " and "--forwarded-allow-ips" not in cmd


def test_api_image_keeps_migration_port_and_non_root_user():
    text = (ROOT / "apps/api/Dockerfile").read_text()
    cmd = _dockerfile_cmd()
    assert cmd.startswith("sh -c alembic upgrade head && uvicorn ")  # 起動時にマイグレーションしてから起動
    assert "--host 0.0.0.0 --port 8000" in cmd and "EXPOSE 8000" in text
    assert re.search(r"^USER app$", text, re.M)  # root で動かさない
