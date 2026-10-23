# 実装計画(Phase 0〜7)

状態: ✅ 実装・検証済み / 🟡 一部 / ⬜ 未着手。各フェーズはゲート(DoD)を満たし、ユーザー確認後に次へ進む。

| Phase | 内容 | 状態 | ゲート(DoD) |
|---|---|---|---|
| 0 | monorepo, Docker Compose(PG18/API/Web), Alembic, OpenAPI→TS型, pytest/Hypothesis/Playwright, lint/型検査 | ✅(CI定義は未実行) | compose で起動・migration up/down/check・全テスト通過・`.env`非追跡 |
| 1 | 整数円配分エンジン、商品別/数量/税・割引ルール、総額照合、残高、厳密最小送金+フォールバック、ゲスト/グループ/招待/支出/精算API、冪等性・楽観ロック・版管理 | ✅ | 不変条件のプロパティテスト・全探索一致・同時実行テスト・E2E通過 |
| 2 | 実フロント: 商品単位割当UI(ワンタップ/全員/前と同じ/一括/数量)、OCR確認画面、精算画面、受取設定、料金比較 | ✅(簡易CSS。OCR・振込先・課金は「準備中」表示、本デザインは後日) | モバイルE2E、アクセシビリティ確認、デモ表示の区別 |
| 3 | PaddleOCR 3.x ワーカー(DBジョブキュー/lease/retry)、OpenCV前処理・品質チェック、日本語レシートパーサー、bbox対応、合計検証、手動入力切替、計測保存、評価CLI | ✅(合成データでのみ評価。実レシートでの精度調整はユーザー作業。amd64 未確認) | 実画像で認識しJSON出力、合成データで評価、ジョブ再試行・失敗時の枠返還 |
| 4 | Passkey(py_webauthn / @simplewebauthn)、ゲスト昇格、振込先の暗号化・版・当事者限定開示、照合コード、コピー導線 | ⬜ | 非当事者が口座詳細にアクセス不可、変更時の旧版警告 |
| 5 | BankDataProvider + Mock、照合エンジン(コード・金額・受取口座・重複・取消)、evidence state | ⬜ | 照合テスト表の全ケース、本番providerはfail-closed |
| 6 | OCR利用枠台帳(無料3+報酬最大7=上限設定値)、広告報酬mock(nonce/上限)、Pro mock、同意、OCR訂正→評価データ・昇格ゲート | ⬜ | 二重消費・偽装報酬で枠が増えない、未同意データが学習/評価へ流入しない |
| 7 | 限定パイロット | ⬜ | 実測と信頼区間付きの精度のみ表示 |

## Phase 3 以降の要点(設計済み)

- OCR は `OcrAdapter` プロトコル(`recognize(image) -> tokens[{text,bbox,score}]`, `model_info()`)で交換可能にする。PaddleOCR 3.x の
  `PaddleOCR(lang="japan", ...)`/`predict()` APIに合わせ、モデル名・版・処理時間・エラーを `ocr_runs` に保存。
- APIとOCRワーカーは別プロセス/別コンテナ。`receipt_jobs` を `FOR UPDATE SKIP LOCKED` で取得、lease期限切れで再取得、
  最大試行後は dead-letter + 枠返還 + 手動入力導線。
- パーサーはルールベース(数量×単価=行金額、小計/税/割引/合計の行役割、8%/10%・軽減税率記号、値引・返品の負号)。
  結果は必ず draft。確定は Phase 1 のエンジンで検算。
- 評価CLI: `eval_datasets(verified ラベルのみ)` に対し行完全一致率・レシート完全一致率・金額誤認識率・平均訂正時間を算出し、
  ベースラインより悪化した候補を `rejected` にする。
- PaddlePaddle の linux/arm64 + Python 3.13: 2026-10-19 に動作確認済み(公式配布元の aarch64 wheel、cpu_threads ≤ CPU 数が条件)。詳細 `docs/OCR_PIPELINE.md`。
