# Claude Code プロジェクト指示 — WariLink

あなたは実装担当のシニアフルスタックエンジニア。日本語で報告し、技術設計は docs/ の仕様を正本として扱う。

## 最優先（変更禁止のプロダクト契約）

1. サービスは送金・銀行振替・決済指図を**実行しない**。
2. `bank_matched` は「所定口座に合致する入金記録を見つけた」ことだけを表す。**支払い保証・送金者本人確認・即時着金保証とは表示しない**。
3. 銀行から取引明細を取得する機能は、正式なデータ連携契約・ユーザー同意・必要な法務確認が終わるまで mock/provider-interface のみ。スクレイピング、ログイン情報の収集、架空APIやURLスキームの発明を禁止する。
4. PayPayやことらへ送金を実行する非公開API・未検証deep linkの実装は禁止する。送金先・金額の表示とコピーを提供する。
5. 登録なし（メール不要）ユーザーもURLグループ・手動割り勘・OCR基本枠を利用可能。振込先の永続保存は登録済みユーザーのみ。passkeyはユーザーが選択する認証方法。
6. 金額は JPY の**整数円**（DB BIGINT / Python int / TS安全整数の検証）で管理。多通貨は各最小単位と通貨コードを別設計とし、異なる通貨を換算なしで合算しない。
7. 金額はすべて**バックエンドが検証・確定**する。OCR結果は必ずreview可能なdraft。金額・明細・税・割引の不一致で自動確定しない。
8. 受取人が登録した口座は**支払当事者に限定して共有**し、暗号化・マスク表示を行う。変更時の古い振込先への上書きを避けるため支払指図表示時のimmutable versionを作る。
9. 利用目的を分離。OCR学習・購買履歴パーソナライズ広告・集計B2Bの同意はそれぞれ独立の任意opt-in。**銀行明細は広告・販売用途へ転用しない**。
10. ライセンスと著作権に配慮する。MIT/Apache 2.0以外のコピー不可コードは権利を確認するまで取り込まない。既存のユーザーGitHubリポジトリを勝手に変更しない。

## 推奨アーキテクチャ

- `apps/web`: Next.js 16 App Router + TypeScript + Tailwind CSS + shadcn/ui + TanStack Query（依存をlockfileで固定）。
- `apps/api`: Python 3.12+ / FastAPI + Pydantic v2 + SQLAlchemy 2 + Alembic。
- `apps/ocr-worker`: PaddleOCR 3.x, OpenCV, receipt parser + verification; APIプロセスとは分離。
- `packages/contracts`: OpenAPIから生成したTypeScript API型・クライアント（型の二重管理を避ける）。
- `infra`: Docker Compose (PostgreSQL 18, API, web, OCR worker); ジョブ状態をDBに永続化し、Redisは必要になってから導入。
- `tests`: pytest + Hypothesis + Playwright; 検証データはsyntheticのみ。

## プロトタイプの使用範囲

`prototype/index.html` はUI参照であり、実際の認証・課金・銀行連携は含まない。見た目・導線を参考にReactのアクセシブルなコンポーネントへ作り直す。静的HTMLを本番実装としてコピーしない。

## 開発方法

1. 初回は仕様を読み、ファイル構成とフェーズ0の実装計画、既存ソースの再利用リスクを短く示す。
2. 計画提示後は、契約未取得の外部金融機能を除く範囲で段階的に**実際のコード・テストを生成**する。コード断片や擬似コードだけで終了しない。
3. 変更対象を限定し、小単位で実装・テスト・レビュー。未依頼のGit操作、リモートpush、秘密情報の取得、外部サービス接続は禁止。
4. 各フェーズ終了時に`実装した機能 / 実行したコマンド / テスト結果 / 制約 / 次のフェーズ`を報告。
5. 依存関係は実行環境で検査し、セキュリティ更新と互換性を確認。Next.js/React/Python系のバージョンは盲目的に最新採用せず、stableをlockfileで固定。
6. コード/仕様の矛盾があれば安全側へ倒し、根拠と判断を明示。

## 仕様書を読む順番

1. `docs/PRODUCT_REQUIREMENTS.md`
2. `docs/SECURITY_PRIVACY.md`
3. `docs/DATA_MODEL.md`
4. `docs/API_CONTRACT.md`
5. `docs/SYSTEM_ARCHITECTURE.md`
6. `docs/TEST_AND_DELIVERY.md`
7. `docs/OSS_REUSE_PLAN.md`

## 絶対に失敗してはいけないテスト条件

- allocation合計 = expense確定額。
- 各通貨・各グループのsum(net balance)=0。
- 照合コードの一致だけでは `bank_matched` にしない（正規providerの取引・金額・受取口座・重複検査が必要）。
- `reported_paid` と `recipient_confirmed`、`bank_matched` を取り違えない。
- 銀行・PayPay送金の実行を示唆する表示や疑似送金の本番機能を出さない。
- 登録なしユーザーのCookie消去後の本人復元を保証しない。Passkeyや回復手段の導線を用意。
- 銀行データ、秘密鍵、アクセストークン、OCR画像の個人情報をアプリログに出さない。
