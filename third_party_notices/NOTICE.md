# Third-party notices

## 直接依存(ロックファイルで固定)

| パッケージ | ライセンス | 固定元 |
|---|---|---|
| FastAPI, Pydantic, pydantic-settings, SQLAlchemy, Alembic, Uvicorn, httpx | MIT | `apps/api/uv.lock` |
| psycopg 3 | LGPL-3.0(バイナリwheelを未改変で動的利用) | `apps/api/uv.lock` |
| pytest, Hypothesis(MPL-2.0), ruff, mypy | MIT / MPL-2.0 | `apps/api/uv.lock`(開発のみ) |
| Next.js, React | MIT | `pnpm-lock.yaml` |
| openapi-typescript | MIT | `pnpm-lock.yaml`(開発のみ) |
| Playwright | Apache-2.0 | `pnpm-lock.yaml`(開発のみ) |
| @axe-core/playwright 4.14.0, axe-core 4.14.0 | MPL-2.0(未改変で利用、E2Eのアクセシビリティ検査のみ) | `pnpm-lock.yaml`(開発のみ) |
| PostgreSQL(Docker image `postgres:18-alpine`) | PostgreSQL License | `infra/compose.yaml` |
| Pillow | MIT-CMU(HPND) | `apps/api/uv.lock` |
| python-multipart | Apache-2.0 | `apps/api/uv.lock` |
| PaddleOCR 3.7.0, PaddleX 3.7.2 | Apache-2.0 | `apps/api/uv.lock`(group `ocr`、ワーカーのみ) |
| PaddlePaddle 3.3.1(CPU) | Apache-2.0 | `apps/api/uv.lock`(取得元 https://www.paddlepaddle.org.cn/packages/stable/cpu/ — PyPI に linux/aarch64 版が無いため) |
| OpenCV(opencv-contrib-python 4.10、PaddleX の依存) | Apache-2.0 | `apps/api/uv.lock` |
| PaddleX の推移的依存(huggingface-hub, modelscope, aistudio-sdk ほか) | 各ライセンス(主に Apache-2.0 / MIT) | `apps/api/uv.lock`。**個別のライセンス監査は未実施** |
| モデル `PP-OCRv6_medium_det` / `PP-OCRv6_medium_rec` | Apache-2.0(モデルの README 記載) | PaddleX が公式配布元から取得。ワーカーイメージのビルド時に同梱。重みの SHA-256 は `ocr_runs.params` に記録 |
| Noto Sans CJK(Debian `fonts-noto-cjk`) | SIL OFL-1.1 | ワーカーイメージ内で合成レシートの描画にのみ使用 |

## 参照のみ(コードの複写なし)

| 参照先 | ライセンス | 参照内容 | 取り込み |
|---|---|---|---|
| https://github.com/spliit-app/spliit `src/lib/shares.ts` | MIT | 最大剰余法・決定論的タイブレークという設計方針 | なし(独自実装 `apps/api/app/domain/money.py`)。コミットSHAは未取得=未クローン |
| https://github.com/oss-apps/split-pro `src/lib/simplify.ts` | MIT | 貪欲法の相殺という一般的手法 | なし(独自実装。厳密解はDPで別途実装) |

ユーザー保有リポジトリ(rai0409/*、非商用ソース公開ライセンス)は本フェーズで参照・取り込みしていない。
