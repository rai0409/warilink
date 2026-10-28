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
| OCR の構造解析・実画像の精度・訂正 | 20 | 8 | 静かな誤りの修正と評価ゲートは実装。精度は合成のみで、実レシートは未測定。税区分の構造一致 0.77 の残課題 |
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
