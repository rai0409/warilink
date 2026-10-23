# OCR ワーカー。API と同じコード(apps/api)を使い、OCR 用の依存(dependency group "ocr")だけを追加する。
# ビルドコンテキストは apps/api(infra/compose.yaml 参照)。
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH \
    PADDLE_PDX_CACHE_HOME=/opt/paddlex PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True
# opencv-contrib-python(PaddleX の依存)が必要とする共有ライブラリ、合成レシート生成用の日本語フォント(OFL-1.1)
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.13.1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --group ocr
COPY . .
# モデル(PP-OCRv6_medium det/rec, Apache-2.0)をビルド時に取得してイメージに含める(起動時にダウンロードしない)
RUN python -c "from app.ocr.paddle_adapter import PaddleOcrAdapter as A; print(A(cpu_threads=1).model_info())"
RUN useradd --system --uid 10001 app && mkdir -p /data/receipts && chown -R app /app /data/receipts \
    && chmod -R a+rX /opt/paddlex
USER app
CMD ["python", "-m", "app.ocr.worker"]
