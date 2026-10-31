# WariLink(開発中)

登録不要の割り勘・精算アプリ。**送金は実行しません**(金額と送金先の案内、送金申告・受取確認のみ)。

- 仕様: `docs/`(金額計算・精算は `docs/SETTLEMENT_ENGINE.md`)/ 進捗: `IMPLEMENTATION_PLAN.md`
- 構成: `apps/api`(FastAPI・金額の正本) / `apps/web`(Next.js) / `packages/contracts`(OpenAPI生成TS型) / `infra`(Compose)

## 必要なもの

Docker, Python 3.12+, Node 22+, pnpm 9

## 起動(Docker Compose)

```bash
make up          # PostgreSQL 18(127.0.0.1:55432), API(:8000, 起動時にmigration), Web(:3000)
make up-ocr      # 上記 + PaddleOCR ワーカー(初回ビルドは数分、イメージ約2GB)
open http://localhost:3000
make down        # このプロジェクトのコンテナのみ停止(ボリュームは残す)
```

## 現在の状態(2026-10-31)

V0 の STEP1〜5、UI v2(PR #8)、OCR の改善(#9・#11)は main に入り、GitHub Actions(api / web / e2e)で通過済み。
大量の金額計算の検証(`CALCULATION_STRESS`)は PR #10 で PASS(`make test-stress`)。
商用公開の判定は引き続き **BLOCKED**: バックアップの暗号化・外部保管と通知先・外形監視が未設定、KMS が未導入、利用規約・
プライバシーポリシーが未確定、実レシートでの OCR 精度が未測定、非商用ライセンス。詳細は `docs/TEST_AND_DELIVERY.md` を参照してください。

## テストの安全装置

API テスト(`make test`)と E2E(`make e2e`)は DB を作り直します。接続先がローカルの `warilink_test*` / `warilink_e2e*`
でなければ実行前に中止します。CI の構成は `docs/TEST_AND_DELIVERY.md` を参照してください。

## 運用ジョブ

- 期限切れのレシート画像の削除と整合性検査: `scripts/warilink_purge_job.sh`(自動では登録しない。`infra/ops/README.md`)
- バックアップ・復元訓練: `.venv/bin/python scripts/warilink_backup.py drill --workdir <空のディレクトリ>`

## 本番設定について

`infra/compose.yaml` は開発用です。`WARILINK_ENVIRONMENT=production` にすると、Cookie の Secure・https の許可オリジン・
既定以外の DB パスワードなどがそろっていない限り API は起動しません。条件は `docs/SECURITY_PRIVACY.md` §2.1 を参照してください。

V0 beta はゲスト利用、グループ・招待、手入力支出、明細割り勘、円単位の精算案と手動確認を提供します。
`WARILINK_FEATURE_PASSKEYS`、`WARILINK_FEATURE_PAYMENT_DESTINATIONS`、`WARILINK_FEATURE_OCR`、
`WARILINK_FEATURE_ADS`、`WARILINK_FEATURE_BILLING`、`WARILINK_FEATURE_BANK_PROVIDER` は既定で `false` です。
有効化した機能だけ API と画面に表示されます。振込先と Passkey の既存データは、機能を止めた後も一覧・削除経路を維持します。

## ローカル開発

```bash
make setup       # .venv(uv、OCR 依存を含む)と pnpm 依存をインストール
make db          # DBのみ起動
make migrate     # 開発DBへmigration
make api         # uvicorn --reload (:8000)
make web         # next dev (:3000) — /api/v1 を :8000 へプロキシ
make worker      # OCR ワーカー(初回はモデルを ~/.paddlex に取得)
make test        # ruff + mypy + pytest(warilink_test DB)
make test-ocr    # 本物の PaddleOCR を使う結合テスト
make e2e         # Playwright(API :18000 / Web :13000 / OCR ワーカー / warilink_e2e DB)
make contracts   # OpenAPI → packages/contracts/src/schema.d.ts
```
