FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH
RUN pip install --no-cache-dir uv==0.13.1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY . .
RUN useradd --system --uid 10001 app && mkdir -p /data/receipts && chown -R app /app /data/receipts
USER app
EXPOSE 8000
# --no-proxy-headers: X-Forwarded-For は アプリの WARILINK_TRUSTED_PROXIES だけで扱う(送信元 IP の偽装で回数制限を
#   すり抜けられないように)。--no-access-log: URL(招待トークンなど)をアクセスログに残さない。
# マイグレーションは起動に含めない(STEP 03)。本番は compose.production.yaml の migrate を 1 回だけ実行し、
# アプリのロール(warilink_app)には DDL の権限を与えない。開発の infra/compose.yaml と E2E は自分のコマンドで
# マイグレーションしてから起動する(tests/test_deploy_config.py で各環境を確認)。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "65", "--no-proxy-headers", "--no-access-log"]
