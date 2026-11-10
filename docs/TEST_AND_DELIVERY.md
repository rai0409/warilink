# テスト・開発フェーズ・受入条件

## フェーズ構成

### Phase 0: 基盤・意思決定（先に生成）
- monorepo/compose/Postgres18/Next16/FastAPI/Alembic/OpenAPI型生成/CI/lint/type-check/test。
- migrations、seed、認証骨格、config feature flag、安全なダミーデータ。
- DoD: フロント/API/DBがローカルで立ち上がり、CIが通り、env exampleに本番secretなし。

### Phase 1: 登録不要の基本割り勘
- guest session、招待URL/権限制御、group/member/expenses/shares/settlement preview。
- 比率/指定額/均等/商品別の整数配分・端数規則を実装。
- DoD: 全配分で金額一致、各通貨別の残高合計ゼロ、並行変更と二重POSTが安全。

### Phase 2: 実際のフロント画面
- `prototype/index.html`を参考にNext.jsでグループ/OCR/商品割当/精算/設定/料金比較を実装。
- Google Chrome/Edge mobile viewportとdesktopでPlaywrightのアクセシビリティ/応答性テスト。
- DoD: デモやモックは常に表示で区別し、本番の「送金する」を誤表示しない。

### Phase 3: レシートOCR＋正確性
- 画像upload、durable worker、PaddleOCR provider、前処理、OCR raw/BBox/明細抽出、review、差分。
- 3枚月次quota、OCR内部エラーの枠返還、重複画像識別。
- DoD: 合計不一致で確定不可、原本対照修正、ジョブ途中クラッシュ後の安全な再試行。

### Phase 4: 永続登録・銀行振込先・手動確認
- passkeyによる登録とゲスト昇格、振込先暗号化/バージョン/共有制御。
- 金額と照合コード表示、PayPay/ことらは手動導線、送金者申告・受取人手動確認。
- DoD: グループ外のユーザーが口座詳細にアクセスできない。変更時の古い情報警告。

### Phase 5: 銀行入金照合のモックと安全性
- `BankDataProvider`, MockProvider、明細インポート、照合候補ルール、重複/取消/同期遅延、status/evidence別管理。
- **本番口座接続は明示的にdisable**、口座同期UIも「利用準備中/デモ」。
- DoD: bank_matchesの論理テスト全通過。口座番号・コードのみで「本人確認・支払保証」と表示しない。

### Phase 6: 広告・課金・OCR改善の基盤
- reward ledger、3+最大7枚、Pro課金mock、OCR corrections→同意済み評価用セット、広告用途別のopt-in。
- DoD: clientの報酬callback偽装がOCR枠を無制限に増やせない。学習用データへの未同意データ流入なし。

### Phase 7: 限定パイロット
- 同意済み実レシートを100→500→1000枚程度まで増やし、店舗/レイアウト別に完全一致と訂正時間を計測。
- 実銀行を扱う前に規制・契約・銀行別確認を完了。明細取得providerとの本番接続は**別承認ゲート**。
- DoD: 精度保証値は実測と信頼区間を伴ってのみ表示。サポート/削除/監査/事故対応が整う。

## 不変条件テスト (property-based)

- 任意の人数≥1、非負整数amount、正の整数重みで `sum(apportion(amount,weights))==amount`。
- 同じ入力では同じ出力、参加者の順序が変わってもparticipant ID対応額に影響しない。
- JPY端数例: 4252 / 3 → (1418,1417,1417) の並びはstable tie-breakに依存するが合計4252で必ず一致。
- 2,970円＝商品2,700 + 税270、商品選択と税分配後の全員合計は2,970。
- 金額0、極大値、無効割合、空の参加者リスト、refund、通貨混在、負数テスト。
- 受取額、立替額、負担額の一致；精算によるグループネット残高総和ゼロ。

## OCRテスト

- レシート/請求書/クレカ利用票を区別。金額が複数行、商品数量、税込・税別、軽減税、値引、サービス料、領収書二枚重ね、ブレ、反射、縦長。
- OCR confidenceが高いが内容が誤り、複数エラーが相殺、合計は一致するが商品名が誤り、という反例。
- `raw OCR` → `draft` → `user confirmed`を明確に区別。annotation無しで100%正答とは扱わない。
- 合意済みの実データはprivate eval storageで評価、トレーニング・評価を混同しない。

## 支払・銀行照合テスト

| Case | Expected |
|---|---|
| 金額一致＋正しいコード＋受取口座＋一意銀行取引＋有効期間 | provider mockでは`matched`候補（productionでは正規providerのみ） |
| 金額のみ一致・コード欠落 | `candidate` or `needs_review` |
| コードのみ一致・金額不一致 | `needs_review` |
| 同額複数入金・複数候補 | 自動match禁止 |
| 一つの銀行取引を2精算に割り当て | DB unique/transactionで拒否 |
| providerが明細を後で変更 | re-evaluate→stale/disputed; history残す |
| 同額の後日別口座入金 | 不一致 |
| コードを他人が使って代理送金 | 銀行入金に一致しても本人送金保証を出さない |
| 銀行接続未契約/未同意 | sync/API fail-closed、手動へ |
| PayPay/ことらの送金申告 | bank matchedへ移行せず手動確認 |
| provider syncが数日遅れ | 最終同期時刻を明示、「未払い」と決めつけない |

## 権限テスト（必須）

- Guest AがGuest Bの口座詳細を直接ID指定→不可。
- Group memberでも精算当事者以外は振込先閲覧不可。
- 支払者が受取人を装って銀行明細を受信/受取承認する→不可。
- Group ownerが他人の銀行連携・Passkey・同意を代理変更する→不可。
- 契約なしbank providerを本番設定値だけでONにできない→fail-closed。
- 同意撤回後、新規のOCR学習exportへレシートが入らない。
- ログ/スナップショット/エラーに口座原文やbank tokenがない。

## E2E

1. 未ログインでLP→グループ→4人参加→新規支出→均等割り→精算表示。
2. レシート画像アップロードmock→job完了→不一致検出→修正→商品選択→額確定。
3. 任意登録passkey（テスト用認証器）→口座登録→一件の精算に限定共有→変更警告。
4. 銀行振込名義コピー→送金申告→受取人確認。
5. mock銀行明細に同額入金（コードなし）→要確認、コードあり一意一致→`matched`（デモ表記）。
6. 無料枠3枚消費→4枚目拒否、reward attemptで最大10枚、濫用reject。
7. OCR学習/広告/統計同意を個別にOFF/ONしてデータ経路を確認。

## 運用ゲート

- 本番リリース前にテストコマンド・結果、migration up/down、backup/restore、OWASP相当の権限レビュー、秘密情報の漏洩テスト。
- リスクのある銀行API接続、OCR学習、データ第三者提供、広告SDK、課金の外部サービス有効化は別途書面で承認。


## Phase 1 テストの対応表(2026-10-18 実装)

| 要件 | テスト |
|---|---|
| 任意金額・人数の1円単位分配(合計一致・±1円以内・順序非依存・負数対称) | `apps/api/tests/domain/test_money.py`(Hypothesis 2000例 + 乱数20,000件) |
| 商品別・数量別・割合別、税・割引・返品・負の調整 | `tests/domain/test_allocation.py`(ランダム明細1500例の不変条件) |
| レシート総額不一致の検出(補正しない) | `test_receipt_any_mismatch_detected`, `tests/api/test_expenses.py::test_mismatch_blocks_confirmation` |
| 厳密最小送金件数 = 全探索 | `test_exact_matches_brute_force*`(独立したバックトラッキング全探索と1800例比較) |
| フォールバック(人数上限・時間制限)と表示 | `test_participant_limit_fallback`, `test_time_limit_fallback_is_safe`, API `test_large_group_settlement_fallback_label` |
| 複数立替・一部送金ロック後の再最適化で残高0 | `test_balances_multi_expense_and_partial_settlement`, API `test_reoptimization_preserves_locked_transfers` |
| 認証・グループ間分離・当事者限定 | `tests/api/test_flow.py`(404隠蔽, 当事者以外の送金詳細不可, CSRF) |
| 冪等性・同時POST・楽観ロック・二重確定 | `test_idempotent_*`, `test_concurrent_*`, `test_optimistic_lock_*`, `test_settlement_batch_double_confirm_*` |
| E2E(登録なし→4人→支出→3人の精算→コピー→申告→受取確認) | `apps/web/e2e/guest-settlement.spec.ts`(Pixel 7 / Desktop Chrome) |

実行: `make test`(pytest、PostgreSQLの `warilink_test` DBを使用), `make e2e`(Playwright、`warilink_e2e` DB)。

## Phase 2 テストの対応表(2026-10-19 実装)

| 要件 | テスト |
|---|---|
| 商品別UI: 数量単位(ビール3杯をA2・B1)、全員、前の商品と同じ、一括割当、未割当の表示と登録不可 | `apps/web/e2e/itemized.spec.ts` |
| 外税の比例配分、総額不一致で自動補正せず差額を表示→明示的な差額調整行で解消、全員の負担合計=レシート合計 | 同上 |
| 編集で新しい版を作成し履歴に残る / 別画面の先行保存を上書きしない(If-Match 409) | 同上(2本目) |
| グループ外ユーザーは概要・精算・レシート・設定のいずれも閲覧不可 | `e2e/guest-settlement.spec.ts` |
| アクセシビリティ: 開始・概要・支出入力・レシート(不一致表示)・精算・設定・料金で WCAG 2.x A/AA の自動検出違反0 | `e2e/a11y.spec.ts`(axe-core。手動の読み上げ確認は未実施) |
| 未提供機能(OCR・広告・課金・振込先)を「準備中」と表示し、「送金する」ボタンを出さない | `e2e/itemized.spec.ts`, `e2e/guest-settlement.spec.ts` |
| 料金の数値が設定値から返る | `apps/api/tests/api/test_meta.py` |

いずれも Pixel 7(モバイル)と Desktop Chrome の2プロジェクトで実行する。Edge は未実行。

## Phase 3 テストの対応表(2026-10-19 実装)

| 要件 | テスト |
|---|---|
| 日本語レシート解析(数量連結・2行商品・単価不一致・軽減税率・内税/外税・値引の範囲・合計不一致を補正しない・合計なし・長音記号・行分かれ・低確信度・合計後の行) | `apps/api/tests/ocr/test_parser.py` |
| 誤認識なしの合成レシートで正解ラベルを完全に復元(Hypothesis 300例) | `tests/ocr/test_synthetic_roundtrip.py` |
| 評価指標・悪化した候補の不採用・未検証ラベル(proposed)の除外・訂正の分類 | `tests/ocr/test_evaluate_and_corrections.py` |
| 本物の PaddleOCR で画像を認識し JSON を出力、モデル名・重みハッシュの記録 | `tests/ocr/test_paddle_real.py`(`make test-ocr`) |
| ワーカー未稼働時は受け付けない / 月3枚・同時アップロードでも超えない / 同じ画像は枠を消費しない | `tests/api/test_receipts.py` |
| エンジン内部エラーの再試行→dead→枠返還(1回だけ)/ 画像不良は再試行せず返還 | 同上 |
| ワーカーが落ちた後の lease 期限切れ再取得・古いワーカーの遅れた書き込みの破棄 / SKIP LOCKED で別ジョブ | 同上 |
| 形式・サイズ検証、EXIF(位置情報)除去、非メンバーは画像も見えない、別グループのレシートを紐付けられない | 同上 |
| 確認後の登録で訂正差分を proposed として記録、二重登録不可 | 同上 |
| E2E: 本物のワーカーで合成レシートを読み取り→下書きの値を確認→負担者を選んで登録→合計一致→枠が減る / 不良画像は失敗表示・返還・手入力へ / 確認画面のアクセシビリティ | `apps/web/e2e/ocr.spec.ts` |

合成データの結果は実レシートの精度を表さない(`docs/OCR_PIPELINE.md §8`)。linux/amd64 での動作は未確認。


## 運用ジョブの検証(2026-10-20、P05)

| 対象 | テスト・手順 | 実行環境 |
|---|---|---|
| 期限切れ画像の削除(dry-run・期限内の保護・二度実行・生の文字の削除・410) | `tests/api/test_receipts.py::test_purge_dry_run_retention_idempotent_and_scrubs_raw_text` | 使い捨て DB `warilink_test` |
| 一部失敗の再試行・終了コード 1 | `test_purge_partial_failure_is_retried_and_reported` | 同上 |
| DB と画像の不一致検出(終了コード 2) | `test_check_storage_detects_missing_and_orphan_files` | 同上 |
| 失敗時の通知(モック) | `WARILINK_ALERT_CMD="cat >> file" scripts/warilink_purge_job.sh` を到達できない DB で実行 | 手動 |
| バックアップ→復元→検証→片付け | `.venv/bin/python scripts/warilink_backup.py drill --workdir 空のディレクトリ` | `warilink_restore_*` の使い捨て DB(作成して最後に削除) |
| 復元の拒否(共有 DB 名・既存 DB・空でない画像ディレクトリ・改ざんしたダンプ/画像) | `warilink_backup.py restore` を各条件で実行 | 手動 |

本番の保管先・暗号化・切り戻しは未検証。


## CI とリリースゲート(2026-10-20、P06)

`.github/workflows/ci.yml`(**GitHub 上では未実行**。push 権限がないため後日の PR で確認する)

| ジョブ | 内容 | DB |
|---|---|---|
| `api` | ruff・mypy・pytest(実モデルのテストは除外) | ジョブ専用のサービスコンテナ `warilink_test` |
| `web` | 型チェック・本番ビルド | なし |
| `e2e`(重い。`api`・`web` の成功後) | 本物の PaddleOCR の結合テスト(`pytest -m ocr`)と Playwright E2E(モバイル・デスクトップ、実 API・実 DB・実ワーカー) | ジョブ専用のサービスコンテナ `warilink_e2e` |

- 起動条件: `main` への push、すべての PR、手動。同じ PR の古い実行は取り消す(push と PR の二重実行をやめた。PR では毎回実行される)。
- 失敗を隠さない: E2E の `retries: 0`、CI では `test.only` を禁止(`forbidOnly`)。失敗時は Playwright のレポートを 7 日保存。
- **破壊的なテストの保護(fail-closed)**:
  - `tests/api/conftest.py` は、接続先がローカル(localhost / 127.0.0.1 / ::1)の `warilink_test` / `warilink_test_*` でなければ、
    downgrade・TRUNCATE の前に中止する。接続後にも `current_database()` を確かめる。
  - `playwright.config.ts` は、`E2E_DATABASE_URL` がローカルの `warilink_e2e` / `warilink_e2e_*` でなければ起動しない。
  - 残るリスク: 別のローカル PostgreSQL(例: 他プロジェクトのコンテナ)に偶然 `warilink_test` という DB があると防げない。
- linux/amd64 での PaddleOCR の動作は CI の初回実行で確かめる(ローカルでは arm64 のみ確認済み)。


## OCR パーサーの回帰(2026-10-20、P10)

| 対象 | テスト |
|---|---|
| 途中の合計で商品を落とさない・候補の記録・支払後の合計を使わない・食い違う合計は補正せず警告 | `tests/ocr/test_parser.py`(P10 の節) |
| 合計後の商品・印字点数との照合・ポイントの付与/利用/充当・返品・小計 | 同上 |
| 性質テスト: 途中の合計を挟んでも全商品を読む / 1 行の読み落としは必ず警告になる(点数が印字されている場合) | `tests/ocr/test_synthetic_roundtrip.py` |

修正前のパーサーで失敗した記録は、作業時の証跡(gate.json と before_fix.log)に残した。


## OCR 評価ゲート(2026-10-21、P12)

| 対象 | テスト |
|---|---|
| 0 件 → blocked、件数不足 → insufficient、未測定・古い形式 → blocked | `tests/ocr/test_benchmark_gates.py`、`test_evaluate_and_corrections.py` |
| 種類違いの同額 → 不一致、税区分の違い → structure_exact のみ不一致 | 同上 |
| 同じ合成データで before/after: 同じ → accepted、商品 1 行の欠落 → rejected | 同上 |
| データ・ラベルの変更 → incomparable、モデルとパーサーの同時変更 → incomparable | 同上 |
| 古いキャッシュ・別モデルの混在・画像の変更 → blocked | 同上 |
| 出所の区分と、実レシートの同意・分割の要件 | 同上 |
| CLI の終了コード(0 / 2 / 3) | 同上 |

実レシートでの評価(Stage C)は未実施。


## 受入判定(P13、2026-10-21、ブランチ `feat/commercial-hardening` HEAD 5b25186 時点)

### 実測したテスト(ローカル macOS arm64、使い捨て DB `warilink_test` / `warilink_e2e` / `warilink_restore_*`)

| 対象 | コマンド | 結果 |
|---|---|---|
| ruff・mypy・API/ドメイン/OCR の pytest | `make test` | 198 passed(実モデル 1 件は除外) |
| 本物の PaddleOCR の結合テスト | `make test-ocr` | 1 passed |
| Web 型チェック・本番ビルド | `pnpm web:typecheck && pnpm web:build` | 成功 |
| E2E(モバイル・デスクトップ、実 API・実 DB・実ワーカー、4 並列) | `make e2e` | 22 passed(精算・権限・Passkey・振込先・検索/集計・OCR・アクセシビリティ) |
| バックアップ → 復元 → 検証 → 片付け | `scripts/warilink_backup.py drill` | PASS |
| 合成データの OCR ベンチマーク(eval-0.2.0) | `cli evaluate`(合成 hard 30 枚) | exact 1.00(CI 0.89–1.00)、structure_exact 0.77(CI 0.59–0.88) |
| 静的監査 | `git grep` の秘密情報パターン・`.env` の追跡・実画像の追跡 | いずれも 0 件 |

- E2E は、ホストがスワップ 10GB・load 77〜195 の間(P10・P11 の作業中)に、OCR と無関係の画面で 5 秒の待ち時間切れが
  断続的に出た。負荷が下がった状態(load 3.6)では 22/22 で通過。CI の専用ランナーでの安定性は未確認。
- `git diff --check`(基準 f465b2d からの差分)で、P07・P08 の新規ファイル 2 件の末尾の空行を検出(この判定のあと別コミットで修正)。

### 未実行・未確認(GO の根拠に含めない)

- GitHub Actions(push していないため一度も実行していない)。linux/amd64 での PaddleOCR。
- **日本語の実レシートでの精度(100 → 500 → 1000 枚の holdout)**: データ・同意・権利の確認がなく未実施。
- 実際の認証器(iOS / Android / Windows Hello)と端末間の同期での Passkey、本番の逆プロキシ・TLS・Cookie、KMS による鍵管理、
  バックアップの暗号化と外部保管、負荷試験、侵入試験、WCAG の手動監査。
- 銀行連携(本番 provider)・送金・多通貨・オフライン同期・定期支出・広告・課金は**対象外(未実装)**。

### 依存とライセンス(SBOM の要点)

- Python 107 パッケージ: LGPL は psycopg / psycopg-binary(既知)、crc32c(bce-python-sdk 経由)、python-bidi(PaddleX 経由)。いずれも動的利用。
  third_party_notices に未記載の推移的依存(crc32c・python-bidi)がある。
- Node(本番依存): MIT / Apache-2.0 / ISC / BSD が中心。`@img/sharp-libvips`(LGPL-3.0、Next.js の画像処理)、`caniuse-lite`(CC-BY-4.0)。
- このリポジトリ自体は RAI NON-COMMERCIAL SOURCE-AVAILABLE LICENSE。**商用公開には権利者との別途の合意が必要**。

### 採点(社内の判定基準。実測ではない項目は満点にしない)

| 項目 | 配点 | 点 | 根拠 |
|---|---|---|---|
| 正確性・データ整合 | 25 | 21 | 整数円・配分和・残高和 0・厳密解のオラクル・申告/受取確認の区別を自動テストで確認。CI と本番相当の負荷では未確認 |
| 認証・セキュリティ・プライバシー | 25 | 18 | Passkey(リプレイ・Origin・RP・UV・署名回数)、権限表と IDOR、振込先の暗号化と支払者限定、本番設定の fail-closed を確認。KMS・侵入試験・認証 API の流量制限・IP 単位の制限が無い |
| OCR の構造解析・実画像の精度・訂正 | 20 | 8 | 静かな誤りの修正と評価ゲートは実装。精度は合成のみで、実レシートは未測定。税区分の構造一致は 0.77 → 0.90(rule-ja-0.2.1) |
| CI・復旧・運用 | 20 | 12 | 隔離 DB の安全装置、画像削除・整合性検査、バックアップ訓練は実測。CI は未実行、E2E は高負荷で不安定、本番の保管・切り戻しは未設計 |
| UX・競合の中核機能 | 10 | 6 | カテゴリ・検索・集計・振込先表示を追加。多通貨・オフライン・定期支出が無く、操作数・時間の比較は未測定 |
| **合計** | 100 | **65** | |

### 判定

- **商用公開: NO_GO**。理由: 実レシートの精度が未測定、CI が一度も通っていない、鍵管理(KMS)とバックアップの暗号化が未実装、
  リポジトリのライセンスが非商用(商用利用の合意が未確認)。
- **限定的な内部検証(合成データ・身内のテスト)の継続: CONDITIONAL**。条件: PR で CI(api / web / e2e)を通す、
  OCR は手入力と並行して使い下書きを必ず確認する、実レシートの評価データ(同意・verified・holdout)を用意する。

## STEP3: 濫用対策の回数制限(2026-10-28)

| 対象 | テスト |
|---|---|
| ゲスト作成(Cookie なしの作り直し)の IP 上限・Retry-After(固定時刻で正確に)・拒否時に作成しない・期間後に回復 | `tests/api/test_rate_limits.py` |
| 既存セッションの再利用は数えない / 全体の上限(IP を毎回変えても止まる) | 同上 |
| 再起動(設定の読み直し・アプリの作り直し)後も制限が残る | 同上 |
| 12 並列の同時要求で上限 5 回を超えない(advisory lock) | 同上 |
| 偽装した X-Forwarded-For の無視・信頼する中継の扱い・IPv6 /64・生の値を保存しない | 同上 |
| 無効な招待の推測を止める・429 から token の有無が分からない・本人(IP を変えても)・token 単位の上限・参加拒否時に変更なし・招待発行の上限 | 同上 |
| Passkey 検証の失敗で開始を止める・開始の上限・既定の上限で通常の登録が通る | 同上 |
| 期限切れ行の削除(保存量の頭打ち)・開発/本番の既定・バックアップから除外 | 同上 |
| 本番の設定検証(秘密・中継・無効化・上限の範囲・不明な名前) | `tests/test_production_config.py` |

計測(使い捨て DB `warilink_test_v0step3`、ローカル macOS): ゲスト作成の遅延 p50 2.61→6.21 ms、p95 4.27→9.51 ms。
既定の上限で、別々の IP の利用者 60 人が作成・招待・確認・参加を行う 210 回の要求で誤拒否 0 件。

## STEP4: 退会・離脱・一時データの削除(2026-10-28、方針に依存しない部分)

| 対象 | テスト |
|---|---|
| ゲストの退会で共有の台帳(残高・精算案・版)が変わらず、名前だけのメンバー行が残る・本人は以後 401 | `tests/api/test_account_deletion.py` |
| 明示的な確認と CSRF が必須(拒否時は何も変えない) | 同上 |
| 登録済みは step-up が必須・期限切れなら何も変えない・振込先の暗号文/Passkey/セッション/本人の招待をすべて失効・別端末の Passkey ログインも不可・監査に口座を書かない | 同上 |
| 途中で失敗するとトランザクションごと巻き戻る | 同上 |
| 1 人の退会が他人のアカウント・振込先に影響しない | 同上 |
| 参加者の離脱(以後 404、台帳は不変)・作成者は 409・部外者は 404 | 同上 |
| 招待の一括失効(作成者だけ、参加済みのメンバーは不変) | 同上 |
| 期限切れ・使用済み challenge の削除が冪等・定期ジョブの出力 | 同上 |
| 画面: 離脱(取り消し/実行)・作成者には出さない・退会(確認文・実行後に戻れない)・他のメンバーからは名前だけ残る | `apps/web/e2e/account-deletion.spec.ts` |

| 作成者の自動・手動の引き継ぎ・退会時の引き継ぎ | 同上 |
| 終了したグループは閲覧のみ(変更は 409)・招待の失効・金額は不変 | 同上 |
| 最後の参加者の離脱で参加者不在になり、参加で取り消す / 30 日後にすべての行と画像を削除(冪等)・終了から 1 年で削除 | 同上 |
| 保持期限(セッション・招待 30 日、監査ログ 1 年)を過ぎたものだけを消す | 同上 |
| 削除済み一覧の書き出し(重複なし)と、復元を再現した DB への再適用(冪等)・CLI | 同上 |
| バックアップの prune(30 日、manifest の無いものに触れない、dry-run) | 同上 |
| 画面: 作成者の引き継ぎ・終了の操作と終了後の表示 | `apps/web/e2e/account-deletion.spec.ts` |

保持期間は `docs/DATA_MODEL.md` §11(2026-10-28 に決定)。

## STEP5: 運用・本番設定の点検(2026-10-29)

| ゲート | 結果 | 根拠 |
|---|---|---|
| `CI_HEAD_MATCH` | STEP5 の PR の head SHA と一致する run で判定(PROMPT04) | 必須ジョブ: api(Ruff・mypy・pytest・秘密情報の検査・OpenAPI の一致)、web(TS 契約の一致・型検査・本番ビルド)、e2e(実 OCR・Playwright) |
| `PRODUCTION_CONFIG` | PASS(自動で拒否する項目)/ 導入先の確認が残る | `tests/test_production_config.py`。`docs/SECURITY_PRIVACY.md` の公開前チェックリスト |
| `BACKUP_RESTORE` | PASS(隔離した一時コンテナでの訓練) | backup → restore → 行数・画像のチェックサム・外部キー・アプリからの読み戻し。15 行・画像 1 枚で 4 秒 |
| 用意された削除済み一覧の復元時の再適用 | PASS(API テスト。外部一覧の最新性は未検証) | `tests/api/test_account_deletion.py::test_tombstones_reapply_deletions_after_restore` |
| バックアップの暗号化・外部保管・アクセス権 | BLOCKED | 保管先・資格情報が未決定(手順のみ) |
| 監視・通知 | BLOCKED | 通知先・外形監視が未設定(失敗時の終了コードと `WARILINK_ALERT_CMD` は実装済み) |
| `CALCULATION_STRESS` | PASS(2026-10-30、下の「計算ストレステスト」) | 100,000 件のランダム配分・5,000 件の明細・2,000 件の台帳・10,000 件の 1 円不一致。`make test-stress` |
| `RELEASE_READY` | **BLOCKED** | 上記の BLOCKED と、非商用ライセンス・KMS 未導入。公開の可否はプロダクト責任者が判断する |

- 見つけて直したもの: STEP1 で OCR を既定で無効にした後、復元訓練(`drill`)の種データの画像アップロードが 503 で失敗していた
  (訓練の元 DB だけ OCR を有効にした)。uvicorn の既定(127.0.0.1 からの `X-Forwarded-For` を信じる・アクセスログに URL を残す)を
  compose と E2E で無効にした。本番の起動時に拒否する項目に、有効期間・保持期間の範囲と DB のスーパーユーザー接続を追加した。
- 残るもの: `apps/api/Dockerfile` の既定の起動コマンドへの同じ引数の追加(STEP5 の変更範囲外)、ベースイメージの digest 固定。

## OCR: 税区分の構造一致(rule-ja-0.2.1、2026-10-26 実装・2026-10-29 に main へ)

- 原因は OCR(PP-OCRv6)が「※」を読み落とすことだった(`docs/OCR_PIPELINE.md §6.2`)。パーサー rule-ja-0.2.1 で、
  凡例による 10% の判定、税額からの推定(1 通りに決まる場合だけ・要確認)、読めない場合の警告を追加。
- 合成 hard 30 枚(seed 11、同じ認識結果): structure_exact 0.77 → **0.90**(CI 0.74–0.97)、exact 1.00 のまま。
  テスト: `tests/ocr/test_parser.py` の「軽減税率の凡例」「記号を読めなかった場合」の節。
- 実レシートでの精度は未測定のままなので、採点(OCR 8 点)は変えない。

## 計算ストレステスト(2026-10-30、`CALCULATION_STRESS`)

`make test-stress`(`pytest -m stress`、通常の pytest と CI では実行しない)。乱数の種は固定(`WARILINK_STRESS_SEED`、既定 20261030)で、
結果は `apps/api/var/stress-report.json` に書き出す。計測環境: ローカル macOS(arm64)、Python 3.13、使い捨て DB。全体 53 秒。

| 対象 | 件数 | 確かめたこと | 結果 |
|---|---|---|---|
| ランダム配分(均等・比率・割合・金額指定) | 100,000 件(各 25,000、1〜50 人、0〜10 億円・返品・境界値) | 合計の一致、理想値との差が 1 円未満(最大 0.98 円)、重み 0 は 0 円、同じ入力で同じ結果、並び順に依存しない | PASS(1 件 p95 0.04 ms) |
| 明細 5,000 件のレシート | 1 枚・30 人・税 8%/10%・サービス料・割引・クーポン・返品・数量/金額指定 | 商品ごと・調整ごと・総額の一致(`verify_receipt`)、同じ結果の再現 | PASS(53 ms) |
| 1 円の不一致 | 10,000 件(合計を ±1 円) | 必ず `RECEIPT_TOTAL_MISMATCH`(差額も正しい)、内訳は補正しない、差額行の明示追加で一致 | PASS(検出 10,000 / 自動補正 0。差額行で解消 9,806、返品のため比例配分できず明示ルールが必要 194) |
| 支出 2,000 件の台帳 | 2・3・5・8・12・20・21・30・50 人 | 残高の和 0、精算案どおりに送金すると全員 0、件数 ≤ 人数-1、厳密解は貪欲法以下 | PASS(20 人の厳密解 730 ms、21 人以上は貪欲法) |
| 最少送金回数の照合 | 2,000 件(2〜8 人) | 厳密解の件数が全探索の答えと一致 | PASS |
| 時間切れ・人数超過 | 200 件(20 人、上限 1 ms)+ 26 人 | 貪欲法へ切り替え(`time_limit` / `participant_limit`)、残高は必ず 0 | PASS(切り替えの遅れ p50 77 ms・最大 87 ms) |
| API: 50 人・支出 2,000 件 | 2,000 回の登録と精算案 | 保存した負担額から計算し直した残高と API の残高が一致、精算案で全員 0、確定時の再検証も通る | PASS(登録 p50 12 ms・p95 16 ms、精算案 0.5 秒) |
| API: 上限のレシート | 300 品目・調整 50 件・30 人 | 登録でき、負担額の合計が一致 | PASS(79 ms) |

- 見つかった不具合: なし(計算の約束は 1 件も崩れなかった)。
- 残る注意点: 20 人ちょうどの厳密解は手元で 0.7 秒かかり、既定の上限(1.5 秒)に近い。本番の CPU が遅いと貪欲法に切り替わり、
  表示が「送金回数を削減」(最少の保証なし)になる場合がある(金額は正しい)。時間切れの判定は部分和の前計算の後に始まるため、
  上限から最大 0.1 秒ほど遅れて切り替わる。

## 数値の独立オラクルと反例の修正(2026-11-03、P01/P02)

本体の戻り値を使わずに期待値を作るオラクルを `apps/api/tests/stress/support.py` に置いた。
`oracle_apportion`(Fraction の理想値と仕様どおりの同順位の決め方)、`hamilton_violations`(配り方の手順に依らない最大剰余法の性質)、
`oracle_receipt`(商品ごと・調整ごと・メンバー別の期待値)。精算は既存の全探索(8 人以下)と列挙オラクルを 9〜16 人にも使う。
オラクルの検出力: 同順位の決め方を逆にした版と、剰余の大きい順を逆にした版をわざと作ると、それぞれ 5,000 件中 550 件・3,147 件で不一致を検出した
(従来の「理想値との差が 1 円未満」の確認ではどちらも検出できない)。計測環境: ローカル macOS(arm64)、Python 3.13、使い捨て DB。

| 対象 | 件数 | 結果 |
|---|---|---|
| 単純分割をオラクルと「誰が 1 円多いか」まで照合(同順位の剰余を含む 25,863 件) | 50,000 件 | 不一致 0(1 件 p95 0.012 ms) |
| 商品別(税率グループ・対象商品指定の比例配分・均等・メンバー・金額指定)をオラクルと照合 | 3,000 枚・82,569 行 | 不一致 0(1 枚 p95 0.38 ms) |
| 最少送金回数: 9〜16 人を列挙オラクルと照合 | 300 件 | 不一致 0 |
| 20 人(+a/-a の組なし)を既定の上限で計算 | 20 件 | 厳密解 p50 728 ms・p95 762 ms、貪欲法への切り替え 0 件(時間は合否にしない) |
| 単体(hypothesis): 配分・単純分割・商品別のオラクル一致、行の並び順、符号反転・名前の入れ替え、1 円の残高、21 人の表示契約 | — | PASS |

見つかった反例と修正(どれも正しい入力の計算結果は変えない。修正の前後で 3,000 枚のレシートと 20 人の精算案が一致、20 人の所要時間も同じ):

| 反例 | 修正 |
|---|---|
| 値引きの対象に指定した商品の一部が明細に無いと、残りの商品の負担者へ黙って配り直していた | `ADJUSTMENT_RULE_INVALID` を返す(`allocation.py`) |
| 1 件の上限(10^12 円)の支出が 2 件以上あると、純残高が上限を超えて精算案が 422 になり、そのグループは精算できなくなっていた | 精算では JS の安全整数(2^53-1)までを扱う(`settlement.py`)。応答の整数がすべてその範囲内であることを API のテストで確認 |
| 支出 ID を大文字の UUID で送ると、プレビューと登録後の負担額が 1〜3 円ずれていた(端数の種の表記が違う) | プレビューも正規化した UUID を種にする(`expenses/router.py`。編集時に元の支出を除く処理も同じ表記で行う) |

## PR B: 利用規約・プライバシー・保存期間の表示(2026-10-31)

| 対象 | テスト |
|---|---|
| 保存期間の方針が設定値・バックアップのスクリプトと一致、削除済み一覧 ≥ バックアップ | `tests/api/test_retention_api.py` |
| 削除ジョブの実績: 記録なし・失敗だけ・古い成功は `unverified`、実際の `purge-images` の成功で `verified`、dry-run は数えない | 同上 |
| 既存の `/plans` のキーを変えない | 同上 |
| 画面: 規約・プライバシーは「準備中」で下書きを正式な条件として出さない、保存期間はジョブ未実行の環境で自動削除を約束しない | `apps/web/e2e/policy.spec.ts` |

- 下書き: `docs/legal/`(`OWNER_APPROVAL_REQUIRED` の一覧は `docs/legal/README.md`)。
- `LEGAL_APPROVAL=BLOCKED`(運営者・専門家の確認待ち)、`RETENTION_RUNTIME=BLOCKED`(導入先で定期ジョブが未登録)。

## PR C: 暗号化バックアップ・削除の復元防止・監視(2026-10-31)

| 対象 | テスト |
|---|---|
| 暗号化の往復(0 バイト〜複数の塊)、1 ビットの改ざん・切り詰め・塊の入れ替え・後ろへの追加・別ファイル/別バックアップとしての復号・鍵違いの拒否、包んだ鍵の付け替えの拒否、環境変数の鍵の検証 | `tests/ops/test_backup_crypto.py` |
| 外部保管: 置いた後のハッシュ確認・再実行で重複しない・再試行・manifest を最後に置く・保管先の外に書かない・未対応の保管先は接続しない | `tests/ops/test_offsite.py` |
| バックアップの形式(平文が残らない・鍵を含めない)、復元の拒否(削除済み一覧なし・古い・鍵違い・改ざん 6 種・包んだ鍵の付け替え・撮影時点の画像の欠け)を DB に触れる前に行う、本番の条件(平文不可・鍵必須・外部保管なしは 4)、prune | `tests/ops/test_backup_script.py` |
| 監視: ジョブの最後の成功(なし・古い・成功後の失敗)、/health(URL を通知に含めない)、通知の重複の抑制と回復の通知、通知の失敗、終了コード | `tests/api/test_monitor.py` |
| 復元訓練(隔離した一時コンテナ): 暗号化 backup → 退会 → 一覧の書き出し → restore → 削除の再適用 → 読み戻し | `scripts/warilink_backup.py drill`(2026-10-31 PASS) |

| ゲート | 結果 |
|---|---|
| `PR_C_CODE` | CI 結果による |
| `ENCRYPTED_BACKUP_TEST` | PASS(上のテスト) |
| `RESTORE_DRILL` | PASS(一時コンテナ `warilink-drill-db`、終了後に削除) |
| `OFFSITE_STORAGE_LIVE` / `KMS_LIVE` / `MONITOR_ALERT_LIVE` / `RPO_RTO_PROVEN` | **BLOCKED**(保管先・KMS・通知先が未選定、本番相当の計測なし) |
| `RELEASE_READY` | **BLOCKED** |

## PR D: OCR の品質ゲート・計算ストレスの定期実行・画面の頑健さ(2026-10-31)

| 対象 | テスト |
|---|---|
| OCR の反例: 「※」の一部の読み落とし(税額から推定 / 決まらなければ空にして要確認)、内訳の無い凡例(仮定を警告)、整合した記号(警告なし)、¥ 付きのレシートの裸の数字(必ず不一致を警告)、合計が合っても品目が欠けたら正答にしない、税額の整数比較が有理数の計算と同じ | `tests/ocr/test_parser_counterexamples.py` |
| 公開判定のゲート(合成では PASS にしない・件数不足・閾値未決定)、分割の汚染の検出、同意の無い実レシートを使わない | `tests/ocr/test_release_gates.py` |
| 計算: 返品・0 円・100 人(API の上限)、20 人の境界で上限時間を縮めた場合(遅い CPU の再現)、精算の同時確定(6 並列で 1 件だけ)と冪等な再送 | `tests/stress/test_settlement_stress.py`・`tests/api/test_stress_api.py` |
| 画面: 登録の多重タップで 1 件、全角・カンマの金額、整数でない金額は登録不可、通信の切断で日本語のエラー → 復帰後 1 件、戻る・進むで二重登録なし | `apps/web/e2e/robustness.spec.ts` |
| 計算ストレスの定期実行 | `.github/workflows/stress.yml`(週 1 回・手動。種 20261030 / 11 / 4242、30 分の上限、結果は 14 日の artifact) |

- 手元での実行(3 つの種、各 11 件): すべて PASS(各 90〜96 秒)。20 人の厳密解は上限 200 ms では常に貪欲法に切り替わる(1,500 ms では厳密解)。
- 判定: `OCR_REAL_HOLDOUT=BLOCKED`(実レシート 0 件)、`OCR_RELEASE=BLOCKED`(閾値未決定)、
  `SCHEDULED_RUN=UNEXECUTED`(定期実行の初回をまだ観測していない)、`DEVICE_MANUAL_QA=UNEXECUTED`(`docs/QA_DEVICE_CHECKLIST.md`)。

バックアップ状態の検証では、開発環境のローカル保存と、完全な暗号化バックアップを外部保管に送りハッシュ照合した成功を区別する。外部で独立管理される削除済み一覧の最新性は現行基盤では確立できず、本番復元は BLOCKED。ローカルの模擬テストはライブ KMS、実際の外部保管、本番復元の検証ではない。

## 無料版(手入力)の画面の提供範囲と公開前ゲート(2026-11-03、P05)

- 料金プラン: 読み取りを止めているときは、読み取り枚数を「停止中」とし、「月N枚まで」「広告で追加」の説明を出さない。広告を止めている間は、広告で増える枚数を示さない(`/plans` の応答の形は変えず、画面だけで判断する)。
- ホーム: 送金を実行しないこと、送金の申告と受け取りの確認はどちらも入金を保証しないこと、登録しない場合はブラウザのデータを消すと元に戻せない場合があることを示す。
- テスト: `apps/web/e2e/policy.spec.ts`(`/plans` を本番の既定値=読み取り・広告 OFF に差し替えて確認)。
- ゲートの一覧と判定: `docs/FREE_RELEASE_GATES.md`(`FREE_RELEASE=NO_GO`)。
- 実行記録(2026-11-03、コミット f4bdab8、ローカル macOS arm64、使い捨て DB): API の pytest 376 件成功、web の typecheck・build 成功、E2E 42 件成功(隔離した E2E 用 DB)。独立レビュー(P01+P02 と P05)はどちらも PASS。
- 商品を削除したときは、その商品だけを対象にした値引き等の対象からも外す。対象がすべて消えた調整は全商品へ広げず、「削除した商品(選び直してください)」と示して保存できないままにする(`apps/web/e2e/itemized.spec.ts`。修正前のコードでは失敗することを確認)。

## STEP 01: 削除ジャーナルの耐久契約と安全な復元(2026-11-04)

設計は `docs/DATA_MODEL.md` §11 と `apps/api/app/ops/deletion_journal.py` の冒頭。削除の 3 つの経路(退会・振込先の消去・グループの
完全な削除)は、DB の外へ `prepare` → DB の削除を commit → `finalize` の後に成功を返す。

| 確かめたこと | テスト |
|---|---|
| 形式: 連番・直前の hash・自分の hash、ID と UTC の時刻だけ、再試行の重複なし、取り消した操作は再開できない | `tests/ops/test_deletion_journal.py` |
| 欠落・改ざんの検出: 1 件の書き換え(hash を計算し直しても検出)・途中の欠落・順序の入れ替え・切り詰め・HEAD の欠け/不一致・壊れた行 | 同上 |
| 落ちたときの回復: HEAD を書く前に落ちた(1 つ遅れ)・行の途中で落ちた(次の追記で切り捨て)。4 プロセスの同時追記で 1 本の連鎖 | 同上 |
| 書けないときは成功にしない(読み取り専用・置き場所を作れない) | 同上 |
| 退会: 記録を確定してから 200(応答のキーは従来どおり)。本人の ID だけを記録し、他人には触れない | `tests/api/test_account_deletion.py`(削除ジャーナルの節) |
| `prepare` を書けない → 503 で何も削除しない / 変更の途中の失敗 → ロールバックして abort / `finalize` を書けない → 503(成功と返さない)、後で reconcile / commit の結果が曖昧 → 未確定のまま残し、自動で取り消さない | 同上 |
| 振込先の消去の再送: 未確定なら確定して 204、確定後は従来どおり 404。他人の未確定は確定しない | 同上 |
| グループの完全な削除: 記録できなければ消さない(`groups_failed`)、記録できれば確定まで | 同上 |
| 削除済み一覧の期限削除: ジャーナルが止まったまま 31 日・60 日を過ぎても未確定の行は消さない。ジャーナル導入前の行は移すまで消さない | 同上 |
| 復元: 古い状態に戻した DB へ、ジャーナルの確定済みの削除を再適用(冪等)し、戻した DB でも確定済みとして扱う | 同上 |
| 復元の拒否(DB を作る前): ジャーナルが無い・未確定あり・別のジャーナル・改ざん・バックアップより短い・HEAD の破損。旧形式は本番で明示的な承知が必要 | `tests/ops/test_backup_script.py`(削除ジャーナルの節) |
| 本番: ジャーナルが未設定・相対パス・未対応の形式なら起動しない。バックアップも拒否 | `tests/test_production_config.py`・`tests/ops/test_backup_script.py` |
| 運用コマンドの終了コード(`journal-verify` など) | `tests/api/test_account_deletion.py::test_journal_cli_exit_codes` |

- 実行記録(2026-11-04、base main `aea833c`、ローカル macOS arm64・Python 3.13、使い捨て DB `warilink_test_ui`):
  API の pytest 416 件成功、Ruff・mypy 成功、OpenAPI はコミット済みと一致、migration と model の差分なし。
  わざと保証を外した 8 通りの変更(finalize を飛ばす・期限削除を絞らない・prepare をしない・DB に無い未確定を自動で取り消す・
  commit の失敗で取り消す・finalize の失敗を成功にする・復元で未確定を無視する・復元でチェックポイントを確かめない)は、
  どれもテストが失敗することを確かめた。
- 復元訓練(`drill`、隔離した一時コンテナ `warilink-drill-db-step01`、終了後に削除): PASS(6.8 秒、合成データ 17 行・画像 1 枚、
  `journal.status=verified`)。
- 判定: `STEP01_CODE` はこのブランチの CI 結果で判定する。`OFFSITE_STORAGE_LIVE` / `KMS_LIVE` は **BLOCKED**(ローカルの
  ジャーナルは外部保管ではない。外部保管は STEP 02)、`FREE_RELEASE=NO_GO`。
- 残る制約: 記録の末尾(バックアップより後)を HEAD ごと失うと検出できない。分散トランザクションではないため、commit の直後に
  落ちた操作は reconcile まで未確定として残る(その間は成功を返さず、復元を拒否する)。

## STEP 02: S3・KMS の外部保管と削除ジャーナルの接続(2026-11-06、実 AWS は未接続)

設計は `apps/api/app/ops/s3_storage.py`・`aws_kms.py` の冒頭と `infra/ops/README.md`「S3・KMS の外部保管」。AWS には一切
接続していない。テストは boto3 と同じ呼び方をする偽の S3・KMS を使い、テストの間はソケットの接続を禁止して、
boto3 を読み込んでいないことも確かめる。

| 確かめたこと | テスト |
|---|---|
| `s3://バケット/接頭辞` の形式、`WARILINK_AWS_LIVE=1` が無ければ資格情報があっても接続しない、boto3 が無ければ案内して止める | `tests/ops/test_s3_storage.py`・`test_offsite.py` |
| 世代と SHA-256 の記録(ETag は使わない)、バージョン管理が無効なら失敗、記録した世代を読む、読み戻しの改ざん・世代違いの拒否 | `tests/ops/test_s3_storage.py` |
| 重複送信(置き直さない)、一部の失敗(再試行の後に失敗・manifest を置かない)、manifest → 受領記録の順、権限・署名・時間切れの失敗で秘密を出さない | 同上 |
| バケットの前提条件(バージョン管理・Object Lock 30 日以上・KMS の既定の暗号化)を確かめるだけで設定は変えない | 同上 |
| S3 の削除ジャーナル: STEP 01 と同じ形式、上書き禁止の条件付き書き込み、同時の書き手の競合 → 読み直して再試行、世代のすり替え・削除の印・途中/末尾の欠落・HEAD の欠け/巻き戻しの検出 | 同上 |
| KMS: エイリアスを ARN に直して manifest に書く、AAD(バックアップ ID・作成日時・鍵 ID)の違い・別の鍵・ARN 以外の鍵 ID を拒否、自動ローテーションとエイリアスの付け替えの後も過去の分を開ける、無効な鍵・権限不足で止める | `tests/ops/test_aws_kms.py` |
| 復元は manifest の鍵の提供元で鍵をほどく(KMS に切り替えた後も、環境変数の鍵の過去のバックアップを開ける。偽 KMS は開かない) | `tests/ops/test_backup_script.py`(S3・KMS の節) |
| S3 へのバックアップは、記録した世代を読み戻して SHA-256・暗号の認証・一覧・ジャーナルのチェックポイントまで確かめてから成功。読み戻しの改ざん・権限不足・ジャーナルの差し替えでは終了コード 4 で成功を記録しない。本番は前提条件を満たさないバケットを拒否 | 同上 |
| 取得の専用スクリプト: 空のディレクトリへ取得して検証、S3 では世代の指定が必須、改ざんは終了コード 1、DB に触れない | 同上(`warilink_fetch_backup.py`) |
| 受領記録と読み戻した実バイトの照合(保管先のファイルを後から差し替えた場合) | `tests/ops/test_offsite.py` |

- 実行記録(2026-11-06、base main `d05c276`、ローカル macOS arm64・Python 3.13、使い捨て DB `warilink_test_ui`):
  API の pytest 458 件成功(うち ops 101 件)、Ruff・mypy 成功、OpenAPI はコミット済みと一致、`uv lock --check` 一致
  (追加は任意の依存グループ `aws` の boto3・botocore・jmespath・s3transfer だけで、既存の版は変えていない)。
- わざと保証を外した 9 通りの変更(世代の確認をしない・受領記録のハッシュを照合しない・読み戻しを飛ばす・ジャーナルの
  複数世代を見逃す・暗号化コンテキストに AAD を入れない・復元で今の鍵を使う・ジャーナルの上書きを許す・接続の許可を
  確かめない・鍵を ARN に直さない)は、どれもテストが失敗することを確かめた。
- SDK との整合(リポジトリ外の一時環境、boto3 1.43.125、接続なし): botocore の Stubber で、S3(put/head/get、バケットの
  設定の確認、条件付き書き込み、世代の一覧)と KMS(DescribeKey・Encrypt・Decrypt)の呼び出しの引数がサービスの定義に
  合うことを確かめた。
- 復元訓練(`drill`、隔離した一時コンテナ `warilink-drill-db-step02`、終了後に削除): PASS(6.7 秒。ローカルの外部保管で
  受領記録 → 読み戻しの検証 → 復元 → 削除の再適用、`journal.status=verified`)。
- 判定: `STEP02_CODE` はこのブランチの CI 結果で判定する。**`OFFSITE_STORAGE_LIVE` / `KMS_LIVE` は BLOCKED**(実 AWS の
  承認・予算・検証用の資格情報が無く、バケット・鍵・IAM は作っていない。偽物での成功は実際の保管の証拠にしない)。
  `FREE_RELEASE=NO_GO`。
- 範囲外の変更: STEP 01 の `tests/test_production_config.py` の 1 件(「`s3://bucket/journal` を拒否」→「接頭辞の無い
  `s3://bucket` を拒否」)。S3 の削除ジャーナルを受け付ける新しい契約と両立しないため、利用者の承認を得て変更した。

## STEP 03: 本番の構成・HTTPS の入口・DB のロール分離(2026-11-08、未デプロイ)

構成は `infra/compose.production.yaml`・`infra/proxy/Caddyfile`・`infra/ops/postgres-roles.sql`、手順は `infra/ops/production-runbook.md`、
起動前検査は `scripts/warilink_preflight.py`。VM・DNS・証明書は未契約で、何もデプロイしていない。

| 確かめたこと | どうやって |
|---|---|
| 外へ出るのは proxy の 80/443 だけ、web・api・db は内部ネットワーク、DB・API をホストへ公開しない、必須の値が無ければ compose が拒否、対象外の機能は false に固定、非 root・権限の削除・読み取り専用 | `tests/ops/test_production_infra.py`、`docker compose config`(Docker の構成の解析だけ。既存のコンテナに触れない) |
| マイグレーションの分離: イメージの既定の起動はマイグレーションしない、本番の API は最新でなければ起動しない、migrate は `run --rm` で 1 回だけ、開発と E2E は従来どおり | `tests/test_deploy_config.py`(古い固定の検査を、環境ごとの新しい契約に置き換えて強化) |
| 信頼する中継: Web の固定アドレスだけ。本番の起動の検査が公開アドレス・広すぎる範囲を拒否 | `tests/test_production_config.py`・`tests/ops/test_production_infra.py` |
| 起動前検査: env ファイルが無ければ BLOCKED(PASS にしない)、危険な値は FAIL、秘密を出力しない、構成・Caddyfile の弱体化を検出、権限 600 | `tests/ops/test_production_infra.py` |

実物での検証(すべて一時コンテナ・一時プロセスで行い、終了後に削除。既存のコンテナ・DB には触れていない):

- **送信元 IP**(Next.js 16.4.2 の standalone と Caddy 2.10):
  - Next.js の中継は、届いた `X-Forwarded-For` をそのまま渡し、無ければ何も足さない。そのため Next を直接公開してはいけない。
  - 本番の Caddyfile(サイトのアドレスと中継先だけ置き換え)を前に置くと、偽の `X-Forwarded-For`(1 つ・複数)・`X-Real-IP`・
    `Forwarded` はすべて捨てられ、API 側には Caddy が見た接続元だけが届いた。
  - Origin・CSRF のヘッダー・Cookie は変わらずに届き、`Set-Cookie`(HttpOnly・Secure・SameSite=Lax)はそのまま返った。
  - セキュリティのヘッダーは付いた。`Server` は消えた。アクセスログは出なかった。
  - 本番の Caddyfile は `caddy validate` を通った。`WARILINK_DOMAIN` が空なら Caddy は起動を拒否する。
- **DB のロール**(使い捨ての PostgreSQL 18、`WARILINK_ROLE_TEST_CONTAINER` を指定したときだけ動くテスト):
  - ロールを作った後、アプリのロールではマイグレーションできず、migrator ではできた。
  - 起動前検査の `--check-db` は、属性・権限・スキーマの版がすべて PASS。
  - アプリのロールでは CREATE / DROP / ALTER / TRUNCATE / `alembic_version` の書き換え / スキーマの作成が拒否された。
  - API はアプリのロールで動いた(ゲスト・グループ・支出の作成)。スーパーユーザーで接続する設定は FAIL になった。
- **API のイメージ**(本番と同じ読み取り専用・権限の削除・本番の設定):
  - マイグレーションの前は終了コード 3 で起動しなかった。アプリのロールでの migrate は `permission denied` で失敗した。
  - compose の migrate のコマンドは migrator で成功し、その後 API は uid 10001 で起動し `/health` が 200 を返した。
  - アクセスログは 0 行。アプリのロールは `alembic_version` を読むだけ。
- **見つけて直した不具合**: 起動前検査の DB の権限の確認で、結果の列に `t` という名前を付けていた。SQLAlchemy 2.0 の Row の
  属性 `.t`(行全体)と衝突して常に「TRUNCATE の権限あり」と誤判定していた。偽物のテストでは見つからず、実 DB のテストで見つかった。
- **わざと保証を外した 9 通りの変更**は、どれもテストが失敗することを確かめた:
  - API のポートを公開する
  - Caddy が利用者の `X-Forwarded-For` を残す
  - イメージが起動時にマイグレーションする
  - 中継の範囲の確認を外す
  - 起動前検査が接続先を出力する
  - API がスキーマの版を確かめない
  - Web のネットワーク全体を信頼する
  - OCR を env で有効にできる
  - ロールの SQL がアプリにスキーマの作成を許す
- **実行記録**(2026-11-08、base main `ca0575e`、ローカル macOS arm64・Python 3.13、使い捨て DB `warilink_test_ui`):
  API の pytest 485 件成功・1 件スキップ(実 DB のロールのテスト。上の一時コンテナで別に実行して成功)。
  Ruff・mypy 成功、OpenAPI はコミット済みと一致。
- **判定**:
  - `STEP03_CODE` はこのブランチの CI 結果で判定する。
  - 本番の HTTPS・送信元 IP・専用 DB・DB の権限は **BLOCKED**(VM・DNS・証明書が未契約で、本番で起動前検査を実行していない)。
  - ロールバックは手順の文書だけで、訓練していない。
  - `FREE_RELEASE=NO_GO`。

## STEP 04: 定期ジョブ・監視・復元の検証・負荷の確認(2026-11-10、timer は未登録)

ユニットは `infra/systemd/`、手順は `infra/ops/production-runbook.md`「定期ジョブと監視」「復旧の目標と測り方」。
timer の登録・実際の通知・AWS への接続・本番への負荷は行っていない。

| 確かめたこと | テスト |
|---|---|
| 監視: 本番で外形監視の URL・通知先・外部保管・削除ジャーナルが未設定なら `not_configured` の異常、削除ジャーナルの `corrupt` / `pending` / `unconfirmed` / `misconfigured`、ジャーナルの構成では旧方式の書き出しを必須にしない、要約に URL・パス・設定値を出さない。既存の終了コード 0/1/2・重複の抑制・回復の通知は変えない | `tests/api/test_monitor.py` |
| 削除ジョブ: ジャーナルがあれば未確定の確定(確定できなければ失敗)、無ければ一覧ファイルが必須(従来どおり)、本番の compose の中で実行、二重起動の拒否と印の解放、通知の要約に設定値を出さない | `tests/ops/test_ops_jobs.py` |
| systemd のユニット: すべてに timer、oneshot・専用ユーザー・時間の上限・有限の再試行・権限の削減、秘密を書かない、時刻、バックアップは読み取り専用のロールでダンプし成否は app で記録、復元の検証は一時コンテナだけ | `tests/ops/test_ops_jobs.py`、`systemd-analyze verify`(下) |
| バックアップの記録のロール(`--record-user`) | `tests/ops/test_backup_script.py` |
| 復元の検証: 計画だけでは何もしない、本番・共有のコンテナ・`warilink_restore_*` 以外・空でない作業場所・ジャーナルや鍵が無い場合は DB に触れる前に拒否、各段階の失敗で止まり、隔離した復元先と作業場所(復号した画像)だけを片付ける | `tests/ops/test_restore_verify.py` |
| 負荷の確認: localhost の開発・テストの API だけ、上限、計画だけでは送らない、本番・不明な環境は拒否、測ったものだけを出す、時間の上限で新しい要求を止める、何も測れなければ PASS にしない | `tests/ops/test_load_smoke.py`(DB を使わない偽の API) |

実物での確認(一時コンテナ・一時プロセスで行い、終了後に削除。既存のコンテナ・DB には触れていない):

- **systemd**: Debian 12 の一時コンテナ(systemd 252)で 8 つのユニットに `systemd-analyze verify` を実行し、エラーなし。
  `systemd-analyze security` は 4 つのサービスとも 3.3(OK)。権限を絞る前は 8.5 だった。
  `OnCalendar` の時刻は日本時間どおりに解決された(03:30 → 18:30 UTC、毎月 1 日 05:00 → 前日 20:00 UTC)。
  実際のホストでの docker との組み合わせは確かめていない。
- **復元の検証**(`warilink_restore_verify.py --execute`、一時コンテナ `warilink-drill-step04`):
  - 合成データ 17 行・画像 1 枚を、環境変数の鍵で暗号化してバックアップした(ローカルの外部保管・削除ジャーナルを使用)。
  - その後に 1 人を退会させ、その退会をジャーナルに記録した。
  - 復元の検証は、ジャーナルの完全性 → 取得と検証 → 隔離した復元と再適用(退会 1 件)→ 検査 → 片付けがすべて PASS で、1.84 秒だった。
  - 復元先の DB と作業ディレクトリは消えていた。
  - 最初の実行はシェルの書き方の誤りで種データが入らず、0 行の復元になった。この結果は採らず、データを入れてやり直した。
- **負荷の確認**(`warilink_load_smoke.py`、ローカルの uvicorn と一時コンテナの PostgreSQL 18、macOS arm64):
  - 条件と全体: 利用者 20・並列 5・支出 5 件/人で 180 要求、成功率 1.0、1.01 秒。精算はすべて厳密解(`exact_subset_dp`)。
  - p50 / p95 / p99(ms):
    - ゲストの作成 7.2 / 76.5 / 79.1
    - グループの作成 14.8 / 40.3 / 45.5
    - 支出の登録 21.8 / 30.2 / 34.9
    - 精算案 12.2 / 22.9 / 27.6
    - 表示 7.8 / 16.6 / 17.2
  - 小さな規模の手元の値で、本番の性能の根拠にはしない。
- **わざと保証を外した 10 通りの変更**は、どれもテストが失敗することを確かめた:
  - 本番の未構成の外形監視を正常にする
  - ジャーナルの未確定を見逃す
  - 削除ジョブがジャーナルの失敗を無視する
  - 二重起動の防止を外す
  - 復元の検証が任意のコンテナを受け付ける
  - 復号した作業場所を残す
  - 負荷の確認が任意のホストを受け付ける
  - 何も測れなくても PASS にする
  - 成否をダンプのロールで記録する
  - ユニットを Restart=always にする

  「任意のホスト」の変更で、最初はテストが通ってしまった。外のホストへの接続に失敗して、たまたま拒否と同じ
  終了コードになっていたためで、拒否の理由と「要求を送らないこと」まで確かめるようにテストを直した。
- **定期のストレス**: 週 1 回の定期実行(`.github/workflows/stress.yml`)の初回を確認した。2026-11-08 に `schedule` で起動し、
  種 3 つとも成功した(https://github.com/rai0409/warilink/actions/runs/42528666176)。PR D の `SCHEDULED_RUN=UNEXECUTED` は解消した。

判定:

- `STEP04_CODE` はこのブランチの CI 結果で判定する。
- 次は **BLOCKED** のまま:
  - `RETENTION_RUNTIME`: timer が未登録
  - `MONITOR_ALERT_LIVE`: 通知先が未選定で、到達を確かめていない
  - `RPO_RTO_PROVEN`: 本番の量・S3・回線を含む実測が無い
- `FREE_RELEASE=NO_GO`。
- STEP 05・06(規約・権利・実機確認・公開の判定)の項目は PASS にしていない。
