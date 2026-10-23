# DBスキーマ・状態遷移・不変条件

## 1. PostgreSQL 18の論理テーブル

`id`はUUIDv7相当（UUIDv4でも可）・日時はtimestamptz、表示はAsia/Tokyo。金額は円整数BIGINT、他通貨もminor_units。APIの権限を問わず公共IDの直接参照だけでデータアクセスを認めない。

| テーブル | 主なカラム（概念） | 制約・メモ |
|---|---|---|
| identities | id, type(guest/registered), created_at, disabled_at | 匿名でも一意なid |
| sessions | id_hash, identity_id, expires_at, revoked_at, assurance_level | raw tokenはDB保存しない |
| passkey_credentials | id, identity_id, credential_id, public_key, sign_count | WebAuthn準拠・復旧方針 |
| groups | id, public_id, owner_identity_id, title, currency, version | 外部IDは推測困難 |
| memberships | group_id, identity_id, member_name, role, join_status | group + identityに一意性 |
| invitations | token_hash, group_id, scope, expires_at, max_uses, uses | link/cookieとメンバー権限は別 |
| expenses | id, group_id, payer_member_id, amount_minor, currency, state, version | 変更はrevisionを残す |
| expense_revisions | id, expense_id, version, previous, next, changed_by | 監査・訂正 |
| expense_shares | id, expense_id, member_id, amount_minor, source | sum=expense amount（確定時） |
| receipts | id, group_id, object_key, checksum, status, delete_at | private storage、期限消去 |
| receipt_jobs | id, receipt_id, state, attempts, lease_until, model_version | 一重処理・再試行 |
| receipt_ocr_drafts | id, receipt_id, raw_ref, parsed, warnings, confidence | confirmedと明確に区別 |
| receipt_items | id, receipt_id, title, qty, unit_minor, line_minor, bbox, version | 商品行ごとの確定情報 |
| receipt_adjustments | id, receipt_id, kind(tax/discount/tip/etc), amount_minor | 符号保存、二重計上禁止 |
| item_allocations | receipt_item_id, member_id, weight, amount_minor | 商品と負担者の関係 |
| settlement_batches | id, group_id, version, state, created_at | 計算スナップショット |
| settlement_transfers | id, batch_id, from_member_id, to_member_id, amount_minor, currency, status, version | amount>0; from≠to |
| payment_destinations | id, owner_identity_id, kind, encrypted_details, masked_label, version, share_policy, archived_at | データ暗号化＋アクセス監査 |
| destination_grants | id, destination_version_id, transfer_id, viewer_identity_id, expires_at | 精算当事者だけ |
| settlement_references | id, transfer_id, reference_code, expires_at, dest_version_id, revoked_at | 一意コード、旧版凍結 |
| payment_reports | id, transfer_id, reporter_id, reported_at, note | 未照合の申告 |
| recipient_confirmations | id, transfer_id, confirmer_id, confirmed_at, evidence_type | 受取人本人の申告 |
| bank_connections | id, owner_identity_id, provider, encrypted_token_ref, account_ref, sync_state, last_synced_at, revoked_at | **feature disabled by default** |
| bank_transactions | id, connection_id, provider_tx_id, account_ref, amount_minor, booked_at, normalized_description, source_version, received_at | 必要最小限、短期retention |
| bank_matches | id, transfer_id, bank_transaction_id, state, evidence_version, matched_at, reviewed_by | double-spend検出、候補→確定 |
| ocr_quota_ledgers | id, identity_id, period, source(free/reward/pro), delta, reason, idempotency_key | 一意の台帳、同時性 |
| rewarded_ad_attempts | id, identity_id, session_id, nonce, status, claimed_at | 広告不正抑止、サーバー上限 |
| consent_records | id, identity_id, purpose, consent, version, captured_at, revoked_at | 用途別、初期OFF |
| ocr_training_candidates | id, receipt_id, correction_id, consent_record_id, eligibility, retention_at | 同意なしで投入しない |
| audit_events | id, actor_id, action, target_type, target_id, occurred_at, safe_metadata | 秘密情報・原本を記録しない |

**注意:** `payment_destinations`の口座情報と`bank_connections`のbank read accessは別物。銀行口座の登録だけでは口座明細を取得できない。

## 2. 重要な計算ルール

- `sum(expense_shares.amount_minor) == expense.amount_minor`（確定時）。
- `sum(group_balances.net_minor) == 0` （各通貨・グループ単位、適用済み精算を適切に控除）。
- 商品総額 = `sum(receipt_items.line_minor) + sum(receipt_adjustments.amount_minor)` 。当該明細の課税・割引表現に矛盾がないこと。
- `sum(item_allocations.amount_minor) == item.line_minor` （各itemごと）。
- bank_matchesは **one transaction→one transfer** を基本。分割入金や複数精算をまとめた入金は別`allocation`構造を追加するまで人手確認、MVP自動match対象外。
- チェック制約: `currency`はISO 4217。JPYのunitは整数。non-JPYへの換算時は換算元・換算先・rate snapshot・端数法を保持。
- 変更時は `version` / `If-Match` / row locking、immutable settlement batch snapshots; 銀行照合済みの過去履歴を単純な再計算で破壊しない。
- idempotency keyは書き込み/課金/送金申告/OCRに必須。内容hashとペアで二重実行を区別。

## 3. 支払ステータス

`unpaid` → `reported_paid`（送金者申告）→ `recipient_confirmed`（受取人の手動確認）

銀行照合は**上記ステータスと別のevidence state**として管理する（誤表示を防ぐ）。

`bank_evidence_state`: `not_connected`, `not_seen`, `candidate`, `matched`, `disputed`, `revoked`, `stale`。

- `recipient_confirmed` は銀行証明ではない。
- `bank_evidence_state=matched` は正式な明細源が存在し、一意な対象取引・受取口座・正金額・コード照合・未使用・照合期間・最新データのルールを満たしたことを示す。本人送金の証明ではない。
- 未検出≠未払い。UIは「最終銀行同期日時点で未検出」と表示。
- 銀行側で取引取消/変更/重複が発生したら`stale/disputed`へ遷移可能。過去のmatchイベントの監査を残す。
- 銀行CSV手動アップロードの場合、evidence_source=`user_csv`とし`bank_api_verified`とは区別。

## 4. 振込照合コード

- `secrets`等の暗号学的擬似乱数による7〜10文字の英数字（銀行で利用可能な文字に制限し、送金元と受取銀行の表示検証を行う）。
- コードは**本人確認用の秘密ではなく、公開しても良い相関ID**。送金者になりすますことも、代理で同じコードを使うことも可能。
- 使用期間とdestバージョンを固定。更新後に古い振込先へ送金されないよう強い警告。
- 数字・ラテン文字の見間違い（0/O, 1/I）、全角半角、空白を考慮。曖昧一致は原則しない。銀行の文字切り捨てに備えて先頭へ置く。
- 銀行から依頼人名・摘要が返らない組み合わせではmanual confirmationへフォールバック。

## 5. ログとデータ寿命

- 機密: 口座番号は暗号化したバイナリor envelope encryption、表示は末尾等のみ。nonce/key version付き。DBバックアップも暗号化。
- bank transaction: 未照合の他人の入出金明細は集めず、照合に必要な信用できる最小データのみ保持。契約・利用目的に合う短期retentionを確定。
- レシート画像: 精算確定後の削除猶予は例えば7〜30日を候補。本人削除・バックアップ残存範囲をポリシー化。
- 集計広告/分析DBはidentity・銀行接続DBから論理分離する。


## 6. Phase 1 実装での確定事項・追加(2026-10-18)

正本はマイグレーション `apps/api/alembic/versions/*_phase1_core_ledger.py`。上表との差分:

| テーブル | 追加・変更 |
|---|---|
| memberships | `identity_id` NULL許可(名前だけのメンバー。招待URLで本人が紐付け)、`position`(表示順、group内一意)、`status` |
| groups | `ledger_version`(支出変更ごとに+1、精算確定の楽観ロック)。`currency='JPY'` CHECK |
| expenses | `split_mode`, `version`, `state(active/deleted)`, `occurred_on` |
| expense_payers | **新規**。複数人立替(`sum = expense.amount_minor`) |
| expense_revisions | `spec`(入力全体JSON)/`result`(計算内訳JSON)/`change_kind`。全版を不変保存 |
| settlement_batches | `ledger_version`, `state(active/superseded)`, `method`, `is_exact_minimum`, `fallback_reason`, `naive_transfer_count`, `minimum_transfer_count`, `balances`(確定時残差), `assumptions`, `elapsed_ms`。`(group_id) WHERE state='active'` 部分一意 |
| settlement_transfers | `group_id`, `status` に `superseded` を追加。`amount>0`, `from<>to` CHECK |
| transfer_events | **新規**。状態遷移の監査(actor, from/to status) |
| idempotency_keys | **新規**。`(identity_id, key)` PK, scope, request_hash, 保存済み応答 |

商品別割当(Phase 1)は `expense_revisions.spec/result` に明細・割当・調整ルールごと保存する。OCR連携(Phase 3)で
`receipts / receipt_items / receipt_adjustments / item_allocations` を正規化テーブルとして追加し、確定時に同じ
エンジンで計算・検証する。

### item_allocations / receipt_adjustments(Phase 3 で追加予定のカラム)

- item_allocations: `mode(equal/weight/units/exact)`, `value`(重み・個数・金額), `amount_minor`(確定額)。units合計=quantity。
- receipt_adjustments: `kind`, `amount_minor`(符号ルールあり), `rule_type(proportional/equal/members/exact)`, `scope_item_ids`, `scope_tax_group`, `member_ids`, `exact_amounts`。
- receipts: `receipt_total_minor`(印字された請求総額), `tax_mode(inclusive/exclusive/mixed)`, `mismatch_acknowledged_by`(差額調整行をユーザーが明示追加した記録)。

### OCR継続改善(Phase 3/6 設計)

| テーブル | 主なカラム | 制約・メモ |
|---|---|---|
| ocr_runs | id, receipt_id, adapter, model_name, model_version, preprocess_version, parser_version, started_at, elapsed_ms, error_code, cost_estimate_micros | 処理原価・時間・エラーを計測 |
| ocr_tokens | run_id, idx, text, bbox(polygon, pixel), score | 生結果。確定結果と別テーブル |
| ocr_parsed_lines | run_id, line_no, role(item/subtotal/tax/discount/total/...), fields JSON, token_idx[], confidence | パーサー出力 |
| ocr_corrections | id, receipt_id, run_id, field_path, before, after, bbox, error_class(`recognition`=文字誤認識 / `structure`=商品・金額の対応付け誤り / `missing` / `spurious`), corrected_by, corrected_at, elapsed_ms | 修正前後と座標を保存 |
| eval_datasets / eval_samples | dataset_id, version, sample_id, image_ref, label JSON, label_status(`proposed`/`verified`), verified_by, split(train/eval, 店舗・レイアウト単位), consent_record_id | **利用者訂正は proposed のまま。検証者の確認後のみ verified** |
| eval_runs | id, dataset_version, adapter/model/parser versions, metrics JSON(行完全一致率・レシート完全一致率・金額誤認識率・平均訂正時間), created_at | 比較の基準 |
| model_promotions | candidate_run_id, baseline_run_id, decision(promoted/rejected), reason | 指標が悪化した更新は不採用 |

### 利用枠(Phase 6、設定値化)

`ocr_quota_ledgers` の付与・消費は台帳加算のみ。上限は `plans` テーブルの設定値(`free_monthly=3`, `reward_max_monthly=7`,
`total_cap_monthly=10`, `pro_monthly`は検証値)で、コード定数にしない。消費は `idempotency_key = receipt_id` で一意。

## 7. Phase 3 で追加したテーブル(2026-10-19)

正本はマイグレーション `apps/api/alembic/versions/20261019_*_phase3_receipts_ocr.py`。

| テーブル | 主なカラム | 制約・メモ |
|---|---|---|
| receipts | group_id, uploaded_by, original_sha256, content_type, byte_size, width, height, storage_key, state(uploaded/processing/draft_ready/failed/confirmed), failure_code, expense_id, review_elapsed_ms, image_deleted_at | `(group_id, original_sha256) WHERE image_deleted_at IS NULL` 一意(重複画像)。画像本体はファイル |
| receipt_jobs | receipt_id(一意), state(queued/running/succeeded/failed/dead), attempts, max_attempts, available_at, lease_owner, lease_expires_at, last_error_code | `(state, available_at)` インデックス。SKIP LOCKED で取得 |
| ocr_runs | receipt_id, job_id, attempt, worker_id, adapter, model_name, model_version, preprocess_version, parser_version, params, quality, tokens(JSONB), draft(JSONB), preprocess_ms/recognize_ms/parse_ms/elapsed_ms, error_code | 試行ごとに1行(失敗も記録) |
| ocr_corrections | receipt_id, run_id, field_path, before, after, bbox, error_class(recognition/structure/missing/spurious), label_status(proposed/verified/rejected), corrected_by | 作成時は必ず proposed |
| ocr_quota_ledger | identity_id, period(YYYY-MM, 日本時間), kind(consume/refund), units, receipt_id, reason | `(receipt_id, kind)` 一意(二重消費・二重返還の防止)。追記のみ |
| ocr_worker_heartbeats | worker_id, model_name, model_version, started_at, last_seen_at | API が稼働中のワーカーの有無を判定 |

§6 の設計からの変更点:

- `ocr_tokens` / `ocr_parsed_lines` は別テーブルにせず、`ocr_runs.tokens` / `ocr_runs.draft.lines` の JSONB とした
  (1 レシートで数十行・常に run 単位で読むため)。確定値(expense_revisions)とは別に保存する点は同じ。
- `receipt_items` / `receipt_adjustments` / `item_allocations` の正規化は見送った。確定した明細は従来どおり
  `expense_revisions.spec/result` が正本。集計が必要になった時点で追加する。
- `eval_datasets` / `eval_runs` / `model_promotions` は DB ではなく、評価 CLI の入出力ファイル(データセットのハッシュ・指標・
  `accepted/rejected` 判定)として実装した。同意データの管理(Phase 6)と合わせて DB 化する。
