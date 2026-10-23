# Claude Codeに貼り付ける実装依頼（マスター指示）

このフォルダは、ユーザーから受け取ったWariLinkの仕様書・操作可能なフロント見本です。
まずルートの `CLAUDE.md` を読み、次に `docs/PRODUCT_REQUIREMENTS.md`, `docs/SYSTEM_ARCHITECTURE.md`, `docs/DATA_MODEL.md`, `docs/API_CONTRACT.md`, `docs/SECURITY_PRIVACY.md`, `docs/TEST_AND_DELIVERY.md`, `docs/OSS_REUSE_PLAN.md`, `prototype/index.html` を参照してください。

**目的:** 上記プロトタイプの操作性とUIトーンを継承した、日本向けの商用割り勘Webアプリを、この新規リポジトリ内で実際に作成する。フロントはNext.js/TypeScript、バックエンドはFastAPI/Python、DBはPostgreSQL 18、OCRはPaddleOCRによる分離ワーカー。精算を正確に行い、送金実行ではなく送金案内と本人申告・受取確認を提供する。

**今すぐの実装範囲:**

1. 実装計画（phase0〜6）をリポジトリ内 `IMPLEMENTATION_PLAN.md` に具体的なファイル/テスト・段階ごとのゲート付きで作成。
2. `apps/web`, `apps/api`, `apps/ocr-worker`, `packages/contracts`, `infra`, `tests` の実稼働monorepo骨格を作成。Docker Composeとローカルコマンドを整備。
3. FastAPIのguest session、group、membership、expense、shares、settlement、code referencesの最小APIを実装し、Postgres migrationを作成する。正確な整数円の配分・残高不変条件をpytest + Hypothesisで検証。
4. 画面見本`prototype/index.html`を**単なるHTML貼り付けでなく、Next.jsコンポーネントとして**再実装。モバイルのタブナビ・グループ概要・OCR確認画面・商品別負担・精算画面・登録者向け受取設定を揃える。
5. 最初のE2Eは「メール登録なしで開始→4人参加→支出登録→3人への精算額表示→金額と振込名義コードコピー→送金申告→受取人の手動確認」。実際の銀行API・PayPay送金は一切呼ばない。
6. OCR workerへの依存インターフェースを作り、synthetic receiptのOCR draftとuser-review画面を実装。PaddleOCRが起動できる構成も作り、実機性能・精度は未検証と明示。OCRデモやシミュレーション結果を実測と誤表示しない。
7. 登録者のPasskey・受取口座暗号化、支払当事者への限定共有、バージョン管理、銀行照合モックを設計。フェーズ毎に順番に実装する。正式な銀行provider連携は契約・法務確認までFeature Flag OFFを維持。
8. 無料OCR月3枚、サーバーが管理する報酬追加7枚の台帳と課金プランのモック、用途別opt-in consent modelを追加。支払保証を示す表記を一切使わない。

**重要制約:**
- 銀行APIの本番接続や認証情報の自動取得はしない。スクレイピング・未検証URLスキーム・実送金機能・実課金は作らない。
- 金額/負担の計算はFastAPIを正本とし、フロントJSの仮計算に依存して精算額を確定しない。
- 送金案内の支払者本人照合はできない。銀行明細の金額+コード一致は「銀行入金照合済み」であって「支払を保証済み」ではない。
- 振込先・照合コード・銀行接続は登録済みユーザーの権限モデルに従い、グループ外・精算非当事者の閲覧を拒否する。
- 利用者の銀行明細は広告・購買分析・外部データ販売に使わない。OCR学習なども任意opt-in。未承諾データ流出をテストで防ぐ。
- Spliit/ SplitPro等のMIT OSSの必要コードのみ、ライセンス通知を保持して参考/移植。既存ユーザーGitHubの非商用公開コードは著作権/第三者権利を確認できるまで勝手に取り込まない。
- 未検証の処理を`verified/guaranteed/success`などと表示しない。API/非同期ジョブのモックをUI内で明示する。
- Git push、実サービスへのデプロイ、外部サービスの契約/課金/秘密情報の取得はしない。

**作業手順:** まず仕様の矛盾/リスク/依存を一覧にしてMVP前提を確定し、現ディレクトリの状態・利用可能なランタイムを確認。その後、可能な範囲でPhase0とPhase1を**コードまで完成**させてテストを実行し、変更ファイル、起動方法、テスト結果、未完タスクを報告。止まらずに勝手な本番金融連携へ進まない。未検証なら未検証と正直に記録する。
