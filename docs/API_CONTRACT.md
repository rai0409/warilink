# REST API Contract — WariLink v1

Base `/api/v1`, JSON, OpenAPI仕様をFastAPIから生成しTypeScriptクライアントへ配信する。`Idempotency-Key` は重要POSTで必須、楽観ロックに `If-Match`/revisionを使う。日付ISO8601、金額はJPY integer円。認証はsecure cookie + CSRF defense、登録ユーザーはPasskeyによりassurance向上。RLS採用時も必ずAPIで当事者権限チェック。

## 1. 身元・招待

| Method | Endpoint | Purpose | Guard |
|---|---|---|---|
| POST | `/identity/guest` | ゲストセッション開始 | Rate-limit, anonymous cookie |
| POST | `/identity/passkey/register/options` | Passkey登録のchallenge生成 | guest/registered session |
| POST | `/identity/passkey/register/verify` | challenge検証＋永続登録 | origin, RP ID, replay protection |
| POST | `/identity/passkey/auth/options` | Passkey login challenge | rate limit |
| POST | `/identity/passkey/auth/verify` | passkey login | sign counter/policy |
| POST | `/groups` | グループ作成 | guest+ |
| GET | `/groups/{public_id}` | group view | membership or scoped capability |
| POST | `/groups/{public_id}/invites` | 期限付きinvite発行 | owner |
| POST | `/invites/{token}/join` | invite参加 | valid+unused link |
| GET | `/groups/{public_id}/members` | メンバー取得 | member |

## 2. 支出・レシート

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/groups/{group_id}/expenses` | 支出一覧 |
| POST | `/groups/{group_id}/expenses` | 支出登録 |
| PATCH | `/expenses/{expense_id}` | revision付き変更 |
| PUT | `/expenses/{expense_id}/shares` | 金額配分プレビュー・確定 |
| POST | `/receipts/uploads` | 署名付きアップロード準備（MIME/size） |
| POST | `/receipts` | OCRジョブの原子的作成・quota予約 |
| GET | `/receipts/{receipt_id}/job` | 進行・エラー・再試行理由 |
| GET | `/receipts/{receipt_id}/draft` | OCR tokens+lines+warnings |
| PATCH | `/receipts/{receipt_id}/draft` | 訂正履歴つき編集 |
| POST | `/receipts/{receipt_id}/confirm` | 検算・確定→expenseへ |
| PUT | `/receipts/{receipt_id}/items/{item_id}/allocations` | 商品別負担配分 |

`POST /receipts` success response例:

```json
{"job_id":"uuid","receipt_id":"uuid","status":"queued","quota":{"reserved":1,"remaining":2}}
```

`GET /receipts/{id}/draft`は確認前 `status=needs_review|draft`、BBoxesはpixel or normalizedをschemaに明示。`receipt_total_minor`はレシートの最終請求額であること、`subtotal/tax/discount`の税込・税抜表現が矛盾しないことを検証する。

## 3. 精算・送金案内

| Method | Endpoint | Purpose | Guard |
|---|---|---|---|
| POST | `/groups/{group_id}/settlements/preview` | ネット残高と送金候補 | member |
| POST | `/groups/{group_id}/settlement-batches` | 精算版確定 | owner / policy |
| GET | `/settlements/{transfer_id}` | 支払者・受取人・額・状態 | 当事者のみ |
| POST | `/settlements/{transfer_id}/reference` | コード発行 | 当事者、idempotent |
| GET | `/settlements/{transfer_id}/payment-instructions` | マスク先・共有許可先詳細・コード | 当事者／用途制限 |
| POST | `/settlements/{transfer_id}/report-paid` | 支払者による送金申告 | payer |
| POST | `/settlements/{transfer_id}/confirm-received` | 受取人による手動確認 | payee |
| POST | `/settlements/{transfer_id}/dispute` | 異議申立て | 当事者 |
| GET | `/settlements/{transfer_id}/evidence` | 銀行照合状態（mock/provider gated） | 当事者。生明細非開示 |

`GET /settlements/{id}/evidence`例:

```json
{
  "payment_status":"reported_paid",
  "bank_evidence_state":"matched",
  "bank_connection_status":"active",
  "matched_amount_minor":6500,
  "currency":"JPY",
  "last_synced_at":"2026-10-16T05:25:00Z",
  "display_text":"銀行入金照合済み（送金者本人の認証・支払い保証ではありません）"
}
```

**注意:** このレスポンスは契約済みprovider・正式な同意・照合条件を満たした場合のみ。mockから同じresponseを返すテスト環境は画面全体にDEMOバッジを表示し、productionへの混入を禁止。

## 4. 登録済み受取先・銀行接続

| Method | Endpoint | Purpose | Guard |
|---|---|---|---|
| POST | `/account/payment-destinations` | 暗号化した受取先登録 | registered + high-assurance |
| GET | `/account/payment-destinations` | マスクした自己一覧 | registered owner |
| PATCH | `/account/payment-destinations/{id}` | 新しいversionを作成 | registered + re-auth |
| POST | `/account/payment-destinations/{id}/share-policy` | 限定共有設定 | owner |
| DELETE | `/account/payment-destinations/{id}` | archived + grants revoke | owner |
| GET | `/bank/connections` | 口座連携状態 | registered owner |
| POST | `/bank/connections/start` | provider認可開始 | **FEATURE_DISABLED until approved** |
| DELETE | `/bank/connections/{id}` | 連携解除・token revoke | owner |
| POST | `/bank/connections/{id}/sync` | 正規provider更新 | permission/rate-limit; gated |
| GET | `/bank/matches` | 自分の照合候補一覧 | payee, minimal evidence |
| POST | `/bank/matches/{id}/accept` | 候補の受取人承認 | payee |
| POST | `/bank/matches/{id}/reject` | 誤照合を否認 | payee |

## 5. Free/Rewarded/Pro

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/ocr/quota` | free+reward+proの消費・残高 |
| POST | `/ads/reward-attempts` | attempt nonce発行 |
| POST | `/ads/reward-attempts/{id}/claim` | 条件確認後の報酬申請。改ざん対策とcredit上限 |
| GET | `/account/consents` | 同意一覧 |
| PUT | `/account/consents/{purpose}` | purpose別の同意/撤回 |
| GET | `/plans` | プラン比較 |
| POST | `/billing/checkout` | 契約までdisabled/mock |

Web広告のclient callbackは信頼できる決済証明ではなく、server-side verificationの可用性は広告プロバイダごとに検証。成功表示よりも不正なクレジット付与制限を優先。

## 6. エラー・監査

- 標準: RFC7807 Problem Details形式: `type,title,status,detail,instance,code,request_id`。
- 特殊コード: `QUOTA_EXCEEDED`, `RECEIPT_UNREADABLE`, `RECEIPT_TOTAL_MISMATCH`, `SHARE_SUM_MISMATCH`, `REVISION_CONFLICT`, `ACCESS_DENIED`, `DESTINATION_NOT_SHARED`, `BANK_PROVIDER_NOT_ENABLED`, `BANK_MATCH_AMBIGUOUS`, `SETTLEMENT_ALREADY_CONFIRMED`, `IDEMPOTENCY_CONFLICT`。
- `403`権限不足、`404`非公開リソース隠蔽、`409`競合、`422`検証エラー、`429`乱用防止。
- APIログは相関ID、応答遅延、分類済み原因のみ。口座情報・銀行明細・OCR原文・tokenは出さない。


## 7. v1 実装済み(Phase 1, 2026-10-18)

OpenAPI: `GET /api/v1/openapi.json`(TS型は `packages/contracts` に生成)。共通ヘッダー:

- `X-WariLink-CSRF: 1` … 全ての状態変更リクエストに必須(欠落時 403 `CSRF_REJECTED`)。Origin が許可外でも403。
- `Idempotency-Key` … `POST /groups`, `POST /groups/{public_id}/expenses`, `POST /groups/{public_id}/settlement-batches` で必須(8〜128文字)。
- `If-Match: <version>` … 支出の更新・削除で必須(欠落 428、不一致 409 `REVISION_CONFLICT`)。

上記 §1〜3 からの変更点: パスの `{group_id}` は推測困難な `{public_id}` を使用。支出の更新は全置換の `PUT`(PATCHではない)。

| Method | Endpoint | 説明 | 権限 |
|---|---|---|---|
| POST | `/identity/guest` | ゲスト開始(既存セッションは再利用) | - |
| GET | `/identity/me` | 現在のidentity | - |
| POST | `/groups` | 作成(自分+名前だけのメンバー) | guest+ |
| GET | `/groups/{public_id}` | 概要・メンバー | member(非メンバー404) |
| POST | `/groups/{public_id}/members` | 名前だけのメンバー追加 | member |
| POST | `/groups/{public_id}/invites` | 招待トークン発行(生値は一度だけ返す) | owner |
| GET | `/invites/{token}` | 参加前確認(グループ名・未紐付けの名前のみ) | guest+ |
| POST | `/invites/{token}/join` | 名前を選んで/新規で参加。同じ名前の同時選択は1人だけ成功(409) | guest+ |
| GET | `/groups/{public_id}/expenses` | 一覧 | member |
| POST | `/groups/{public_id}/expenses/preview` | 保存せず負担額・問題点・反映後の精算案を計算 | member |
| POST | `/groups/{public_id}/expenses` | 登録(問題があれば422で中止) | member |
| GET | `/groups/{public_id}/expenses/{id}` | 詳細(入力仕様+計算内訳) | member |
| GET | `/groups/{public_id}/expenses/{id}/revisions` | 訂正履歴 | member |
| PUT | `/groups/{public_id}/expenses/{id}` | 訂正(新しい版を作成) | member |
| DELETE | `/groups/{public_id}/expenses/{id}` | 論理削除(版を作成) | member |
| GET | `/groups/{public_id}/balances` | 純残高と内訳 | member |
| POST | `/groups/{public_id}/settlements/preview` | 送金案・最適化情報 | member |
| POST | `/groups/{public_id}/settlement-batches` | 精算案の確定 `{expected_ledger_version}` | member |
| GET | `/groups/{public_id}/settlement-batches[/current]` | 履歴/現在の確定案(`is_stale`付き) | member |
| GET | `/settlements/{transfer_id}` | 送金1件の詳細 | 支払者・受取人のみ(他は404) |
| POST | `/settlements/{transfer_id}/report-paid` | 送金申告 | 支払者のみ |
| POST | `/settlements/{transfer_id}/confirm-received` | 受取の手動確認 | 受取人のみ |

精算レスポンスの `optimization`:

```json
{"method":"exact_subset_dp","is_exact_minimum":true,"display_label":"最小送金回数",
 "fallback_reason":null,"naive_transfer_count":5,"optimized_transfer_count":3,"minimum_transfer_count":3,
 "elapsed_ms":0.4,"assumptions":{"currency":"JPY","integer_minor_units":true,"any_member_can_pay_any_member":true,
 "transfer_fees_considered":false,"per_transfer_limit_minor":null,"locked_transfers_preserved":true}}
```

追加エラーコード: `RECEIPT_TOTAL_MISMATCH`(差額 `difference` 付き), `ITEM_UNALLOCATED`, `UNITS_MISMATCH`,
`ADJUSTMENT_SIGN_INVALID`, `ADJUSTMENT_RULE_INVALID`, `PAYER_SUM_MISMATCH`, `PERCENTAGE_SUM_MISMATCH`,
`DUPLICATE_MEMBER`, `UNKNOWN_MEMBER`, `CALCULATION_FAILED`(不変条件違反・確定中止), `TRANSFER_SUPERSEDED`,
`MEMBER_ALREADY_CLAIMED`, `INVITE_INVALID`, `CSRF_REJECTED`, `IDEMPOTENCY_KEY_REQUIRED`, `PRECONDITION_REQUIRED`。

### Phase 2 追加(2026-10-19)

| Method | Path | 説明 | 認可 |
|---|---|---|---|
| GET | `/plans` | 料金比較の表示用。OCR無料枠・広告報酬・月上限・Proの参考値を**設定値**(`WARILINK_OCR_*`)から返す。`ocr_available` / `ads_available` / `billing_available` が false の間、画面は「準備中」と表示し購入・視聴の導線を出さない | 不要(個人情報なし) |

画面(`apps/web`)は `/expenses/preview` の `breakdown.issues[].target`(商品ID・調整ID)で問題の行を強調し、
`RECEIPT_TOTAL_MISMATCH.detail.difference` を差額として表示する。差額調整行はユーザーが押したときだけ追加する(`kind=other`)。

### Phase 3 追加(2026-10-19)

| Method | Path | 説明 | 認可 |
|---|---|---|---|
| POST | `/groups/{public_id}/receipts` | multipart `file`。検証・EXIF除去・保存・枠消費・ジョブ登録。同じ画像は既存を返す(`duplicate=true`、枠消費なし) | member |
| GET | `/groups/{public_id}/receipts/{id}` | 状態・試行回数・失敗理由(日本語)・下書き(`draft`)・計測(`run`)・利用枠 | member |
| GET | `/groups/{public_id}/receipts/{id}/image` | 保存した画像(EXIF なし JPEG)。期限後は 410 | member |
| GET | `/ocr/quota` | 今月の利用枠 `{period, free, cap, used, remaining}` | 本人 |

`POST /groups/{public_id}/expenses` に任意項目 `source_receipt_id`, `review_elapsed_ms` を追加。指定時はレシートを
`confirmed` にし、下書きとの差分を `ocr_corrections` に記録する。同じレシートの二重登録は 409 `RECEIPT_ALREADY_USED`。
`GET /plans` の `ocr_available` は「設定で有効 かつ 稼働中のワーカーあり」。

追加エラーコード: `OCR_UNAVAILABLE`(503), `OCR_QUOTA_EXCEEDED`(429), `IMAGE_TOO_LARGE`(413), `IMAGE_TYPE_UNSUPPORTED` /
`IMAGE_UNREADABLE`(415), `IMAGE_EMPTY`, `IMAGE_EXPIRED`(410), `RECEIPT_NOT_FOUND`, `RECEIPT_ALREADY_USED`。
