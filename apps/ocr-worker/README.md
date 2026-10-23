# OCR worker

PaddleOCR 3.x による非同期レシート読み取りワーカー。API とは別プロセス・別コンテナで動き、PostgreSQL の
`receipt_jobs` をキューとして処理する。コードは `apps/api/app/ocr/`(API と DB モデルを共有)、この
ディレクトリには Dockerfile だけを置く。仕様は `docs/OCR_PIPELINE.md`。

```sh
make up-ocr            # db/api/web に加えてワーカーを起動(初回ビルドでモデルを取得)
make test-ocr          # 本物の PaddleOCR を使う結合テスト(ローカルの .venv)
.venv/bin/python -m app.ocr.cli recognize 画像.jpg   # apps/api で実行。1枚を読み取って下書きを表示
```
