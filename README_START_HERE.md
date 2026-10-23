# WariLink — Claude Code 実装キット

**目的:** 日本向け・メール登録不要の割り勘／レシートOCR／送金先共有／銀行入金照合アプリの実装準備。

## 同梱ファイル

| ファイル | 用途 |
|---|---|
| `prototype/index.html` | ブラウザで操作可能な、単独HTMLのフロント見本。外部API接続なし |
| `prototype/README.md` | モックで実際には動かない部分の説明 |
| `CLAUDE.md` | Claude Code向けプロジェクト指示・禁止事項・開発順序 |
| `docs/PRODUCT_REQUIREMENTS.md` | 全機能・ロール・画面・料金・受入要件 |
| `docs/SYSTEM_ARCHITECTURE.md` | Next.js/FastAPI/Postgres/OCR workerの技術構成 |
| `docs/DATA_MODEL.md` | DBスキーマ・アクセス・状態機械・不変条件 |
| `docs/API_CONTRACT.md` | REST API・認証・整合性・エラーコード |
| `docs/SECURITY_PRIVACY.md` | 銀行情報・金融規制・同意・OCR改善・広告 |
| `docs/TEST_AND_DELIVERY.md` | テスト・モックと本番のゲート・開発フェーズ |
| `docs/OSS_REUSE_PLAN.md` | ユーザーGitHub/外部OSSの再利用計画・権利確認 |
| `docs/CLAUDE_CODE_PROMPT.md` | Claude Codeへ貼り付けるマスター指示 |

## すぐUIを確認する

`prototype/index.html` をChrome/Edgeなどのブラウザで開く。バックエンドは不要。

## Claude Codeで実装開始

1. このフォルダを開発用の新規リポジトリのルートへ置く（既存リポジトリは書き換えない）。
2. WSLターミナルでルートに移動し、`claude` を起動。
3. `docs/CLAUDE_CODE_PROMPT.md` の本文を貼る（またはClaude Codeにそのファイルを読むよう指示する）。
4. フェーズ単位で生成し、ユーザーが検証結果を確認してから次フェーズへ進める。

※「銀行入金照合済み」は銀行明細との一致を意味します。**支払いの完全保証／銀行による送金者本人確認ではありません**。金融事業者の承認・契約が済むまで実口座アクセスは無効です。

## 実装方針

- **MVP** は支出・手動割り勘・認証・振込先限定共有・送金申告・手動受取確認・ローカルOCRのレビュー・精算のみ。
- **銀行自動照合**は `BankDataProvider` インターフェースとモックまでは作成し、正式な事業者契約・銀行ごとの実機確認までリアル接続を有効化しない。
- **広告**は報酬クレジットの仕組みを作成。Web広告のクライアントイベントだけを信頼して無制限クレジット付与しない。
- **有料**は支払い基盤インターフェースとsandbox/mockから開始。サーバーOCRを無制限と表現・実装しない。
- **データ二次利用**は全てデフォルトOFF、個別同意、銀行明細は広告・販売に使用しない。

## UI動作テスト

`smoke_test_prototype.py` はローカルのPlaywright/Chromiumを使い、5画面の表示、OCRダミー、商品配分、送金申告、銀行入金一致の疑似再現、登録状態の切替を検査します。`prototype/desktop_preview.png`, `mobile_preview.png`, `mobile_settlement_preview.png` はテスト時に生成したスクリーンショットです。
