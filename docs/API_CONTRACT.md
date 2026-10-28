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
| POST | `/groups/{public_id}/invites` | 招待トークン発行(生値は一度だけ返す) | owner(member は 403) |
| GET | `/invites/{token}` | 参加前確認(グループ名・未紐付けの名前のみ) | guest+ |
| POST | `/invites/{token}/join` | 名前を選んで/新規で参加。同じ名前の同時選択は1人だけ成功(409) | guest+ |
| POST | `/groups/{public_id}/invites/revoke` | 有効な招待をすべて失効(STEP4)。`{revoked}` を返す。参加済みのメンバーはそのまま | owner(member は 403、他は 404) |
| POST | `/groups/{public_id}/leave` | グループから抜ける(STEP4)。204。メンバー行は名前だけ残し本人との結び付きを外す。以後 404。作成者が抜けると参加の早い参加者へ自動で引き継ぎ、誰もいなければ参加者不在(30 日後に削除) | member |
| POST | `/groups/{public_id}/owner` | 作成者を参加済みの別のメンバーへ引き継ぐ `{member_id}`(STEP4)。未参加・自分は 422 | owner(他は 403) |
| POST | `/groups/{public_id}/close` | グループを終了(STEP4)。以後は閲覧のみ、招待は失効、1 年後に削除。`GroupOut.closed=true` | owner(他は 403) |
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

送金(`TransferOut`)には `status_label`(日本語の表示名)と `is_recipient_confirmed` を付ける。`reported_paid` は
「送金申告済み(受取人は未確認)」で、確認済みとは表示しない。確定バッチの `progress`、プレビューの `locked_progress`、
残高行の `reported_unconfirmed_out_minor` / `reported_unconfirmed_in_minor` は 2026-10-20 に追加(既存項目は不変。詳細は SETTLEMENT_ENGINE.md)。

### 権限表(2026-10-20 確定、P02)

| 操作 | owner | member(参加済み) | 精算の当事者 | 非メンバー | 未認証 |
|---|---|---|---|---|---|
| グループ・メンバー・支出・残高・精算案の閲覧 | ○ | ○ | - | 404 | 401 |
| 名前だけのメンバー追加 `POST /members` | ○ | ○ | - | 404 | 401 |
| 支出の登録・訂正・削除、精算案の確定 | ○ | ○ | - | 404 | 401 |
| 招待URLの発行 `POST /invites` | ○ | 403 | - | 404 | 401 |
| 送金1件の閲覧 | - | 当事者以外 404 | ○ | 404 | 401 |
| 送金申告 / 受取確認 | - | 当事者以外 404 | 支払者のみ / 受取人のみ(逆は 403) | 404 | 401 |

- 判断: PRD の「group_owner: 参加者管理」と、この表の「メンバー追加は member も可」は衝突しうる。
  既存の画面(設定タブの追加フォーム)と、アプリを使わない人を後から足す導線を壊さないため、**追加は member も可**、
  **招待の発行(=第三者がグループに入る経路)は owner のみ**とした。メンバーの削除・改名・グループ閉鎖は未実装で、
  実装時は owner のみとする。メンバー追加は `audit_events(action=group.members.add)` に記録する。
- 認可は URL の公開IDだけで決めない。すべて「Cookie のセッション → identity → そのグループの有効な membership」で判定し、
  支出・送金は ID がそのグループ(送金は当事者)に属することも確かめる(他グループの ID は 404)。
- 招待: 期限切れ・取り消し・回数切れは 404 `INVITE_INVALID`。参加済みの本人は回数切れ後も冪等に参加済みとして返す(回数を消費しない)。

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
`GET /plans` の `ocr_available` は「設定で有効 かつ 稼働中のワーカーあり」。`passkeys_available` は `WARILINK_FEATURE_PASSKEYS`、`payment_destinations_available` は `WARILINK_FEATURE_PASSKEYS` **かつ** `WARILINK_FEATURE_PAYMENT_DESTINATIONS`(振込先は Passkey の登録・再確認が前提)。

追加エラーコード: `OCR_UNAVAILABLE`(503), `OCR_QUOTA_EXCEEDED`(429), `IMAGE_TOO_LARGE`(413), `IMAGE_TYPE_UNSUPPORTED` /
`IMAGE_UNREADABLE`(415), `IMAGE_EMPTY`, `IMAGE_EXPIRED`(410), `RECEIPT_NOT_FOUND`, `RECEIPT_ALREADY_USED`。
2026-10-20 追加(P04): `IMAGE_TOO_MANY_PIXELS`(413、ヘッダーの寸法で判定)、`LENGTH_REQUIRED`(411、Content-Length のない本文)、
`OCR_RATE_LIMITED`(429、利用者・グループの流量または読み取り待ちの数)、`OCR_BUSY`(503、全体の待ち行列が上限)。
本文の大きさは multipart の解析前に `Content-Length` で判定する(上限 = `receipt_max_bytes` + 64KB)。


### P07 追加(2026-10-20): Passkey(任意)

| Method | Path | 説明 | 認可 |
|---|---|---|---|
| POST | `/auth/passkeys/register/options` | 登録の challenge(`flow_id` と WebAuthn の options)。`{nickname?}` | セッション(ゲスト可) |
| POST | `/auth/passkeys/register/verify` | `{flow_id, credential, nickname?}`。成功するとゲストは同じ identity のまま `registered` に昇格し、セッションを作り直す | 登録を始めた本人・同じセッション |
| POST | `/auth/passkeys/login/options` | 認証の challenge(端末に保存された Passkey から選ぶ) | 不要(CSRF ヘッダーは必須) |
| POST | `/auth/passkeys/login/verify` | `{flow_id, credential, merge_current_guest}`。成功すると新しいセッションを発行し、この端末の以前のセッションは失効 | 認証を始めたセッション |
| GET | `/auth/passkeys` | 自分の Passkey 一覧(公開鍵・クレデンシャル ID は返さない) | 本人 |
| DELETE | `/auth/passkeys/{id}` | 失効。最後の 1 件は 409 `LAST_PASSKEY`。他人のものは 404 | 本人 |
| POST | `/auth/logout` | ログアウト(204)。ゲストは 409 `GUEST_CANNOT_LOGOUT`(戻れなくなるため) | 本人 |
| POST | `/auth/sessions/revoke-others` | この端末以外のセッションをすべて失効 | 本人 |
| DELETE | `/account` | 退会(STEP4)。本文 `{confirm: true}` 必須。`{groups_left, destination_erased}` を返し、Cookie を削除。以後 401 | 本人 + CSRF。登録済みで Passkey の機能が有効なら step-up も必須 |

- `GET /identity/me` に `assurance_level`(`guest` / `passkey`)と `passkey_count` を追加(既存項目は不変)。
- エラー: `PASSKEY_CHALLENGE_INVALID`(400、期限切れ・使用済み・別の人/セッションの challenge)、
  `PASSKEY_VERIFICATION_FAILED`(400、署名・Origin・RP ID・ユーザー検証・署名回数・未登録の Passkey。どれが原因かは返さない)、
  `PASSKEY_ALREADY_REGISTERED`(409)。
- 終了したグループへの支出・精算・メンバー・招待・参加の変更は `409 GROUP_CLOSED`(STEP4)。`GroupOut` に `closed`(bool)を追加(既存キーは不変)。
- 回数制限(STEP3): `POST /identity/guest`(新規作成のときだけ)、`POST /groups/{id}/invites`、`GET /invites/{token}`、`POST /invites/{token}/join`、`/auth/passkeys/register/*`、`/auth/passkeys/login/*` は、上限を超えると `429 RATE_LIMITED`。`Retry-After` ヘッダーと本文の `retry_after_seconds` に次に試せるまでの秒数を返す(`docs/SECURITY_PRIVACY.md` §2.3.1)。


### P08 追加(2026-10-20): 振込先(登録済みの本人のみ)

旧案(§4 の `/account/payment-destinations` 複数件・PATCH)から、**1 人 1 件・PUT で新しい版**に簡略化した。

| Method | Path | 説明 | 認可 |
|---|---|---|---|
| GET | `/account/payment-destination` | 自分の振込先(伏せ字・版・共有の可否)。未登録は null | 本人 |
| POST | `/account/payment-destination` | 登録 `{kind: bank|paypay, bank|paypay, share_with_payers}` | 登録済み + step-up |
| PUT | `/account/payment-destination` | 変更 = 新しい版(以前の版は書き換えない) | 登録済み + step-up |
| POST | `/account/payment-destination/sharing` | 支払者への表示の許可・取り消し | 登録済み本人 + step-up |
| GET | `/account/payment-destination/reveal` | 自分の全桁 | 本人 + step-up |
| DELETE | `/account/payment-destination` | 消去(全版の暗号文と包んだ鍵を消す) | 登録済み本人 + CSRF。Passkey の機能が有効なら(振込先の機能の状態に関係なく)step-up も必須。Passkey の機能が停止中の場合だけ、step-up なしで登録済み所有者による不可逆消去を許可 |
| GET | `/settlements/{transfer_id}/destination` | 受取人が共有した振込先・照合コード・変更の警告 | **その送金の支払者だけ**(受取人 403、他は 404) |

- V0 の機能フラグ: 振込先の登録・変更・共有設定・全桁表示・支払者への表示は、Passkey と振込先の**両方**が有効なときだけ。どちらかが停止中なら、認証(401)・CSRF(403)を通った後に `503 FEATURE_DISABLED`。本人の伏せ字の取得(GET)と既存データの消去は停止中も残す。

- step-up: 直近 `step_up_max_age_seconds`(既定 600 秒)以内に Passkey で確認したセッション。満たさなければ 401 `STEP_UP_REQUIRED`
  (画面は Passkey で再確認してから同じ操作をやり直す)。
- 支払者への表示: 最初に表示した版と照合コード(8 文字、0/O/1/I を除く)を `transfer_destination_pins` に固定する。受取人が後で
  変更すると `changed_since_first_view=true` と以前の伏せ字を返す。精算案が置き換えられた送金は 409。共有なし・未登録は
  `{available: false, reason: "not_shared"}`。
- エラー: `REGISTRATION_REQUIRED`(403)、`DESTINATION_EXISTS`(409)、`DESTINATION_ERASED`(410)、
  `DESTINATIONS_UNAVAILABLE`(503、鍵が未設定)、`DESTINATION_INTEGRITY`(500、改ざん・付け替え)。
- 入力の検証: 銀行コード 4 桁・支店コード 3 桁・口座番号 7 桁・種類(普通/当座/貯蓄)・名義(正規化してカタカナ・英大文字・数字)。
  銀行名・支店名とコードの対応は検証していない(銀行マスタを持たない)。


### P09 追加(2026-10-20): カテゴリ・検索・集計

- 支出の入力(`ExpenseIn`)に `category`(`food` / `drinks` / `groceries` / `transport` / `lodging` / `entertainment` / `daily` / `other`、
  省略・null = 未分類)を追加。出力(`ExpenseOut`)にも `category`。既存の支出は未分類のまま。金額・端数の計算は変わらない。

| Method | Path | 説明 | 認可 |
|---|---|---|---|
| GET | `/groups/{public_id}/expenses/search` | `q`(内容の部分一致。`%` `_` は文字として扱う)・`category` / `uncategorized`・`date_from` / `date_to`(支出日、未入力は登録日 JST)・`payer_member_id`・`created_by_me`・`sort`(`date_desc` / `date_asc` / `amount_desc` / `amount_asc`)・`limit`(1〜100)・`cursor` → `{items, total_count, total_amount_minor, next_cursor}` | member(他は 404) |
| GET | `/groups/{public_id}/expenses/stats` | `date_from` / `date_to` → カテゴリ別・月別・メンバー別(`paid_minor` 立て替えた額 / `share_minor` 負担する額)・受取確認待ちの送金 | member |

- `cursor` は不透明な文字列(中身は件数のオフセット。上限 10,000)。不正・改ざんは 422 `INVALID_CURSOR`。
- 検索・集計はそのグループの有効な支出だけ。他グループのメンバー ID で絞っても他グループの支出は返らない。
- 集計はメンバーへの表示のためだけに計算する。広告・外部提供への再利用はしない(同意の対象外の用途として扱わない)。
