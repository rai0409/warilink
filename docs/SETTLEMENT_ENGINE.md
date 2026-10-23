# 金額計算・精算最適化エンジン仕様(Phase 1 実装済み)

実装: `apps/api/app/domain/`(DB・HTTP に依存しない純粋関数)。API はこの層だけを使って金額を確定する。

## 1. 金額の表現

- すべて通貨の最小単位の **Python int**(JPY は 1 円)。DB は BIGINT、API は JSON 整数。float・Decimal は使わない。
- API は `strict` な整数だけを受け付ける(`100.0` や `"100"` は 422)。絶対値の上限は `10^12`(JS の安全整数の範囲内)。
- 通貨は現時点で JPY のみ(DB の CHECK 制約で固定)。多通貨は別設計。

## 2. 端数配分(最大剰余法 / Hamilton 法)

`apportion(total, weights, seed)`:

1. 各人の理想値 `|total| × w / W` を整数除算し、商(floor)と剰余を求める。
2. 不足分(`|total| − Σfloor`)を、剰余の大きい順に 1 円ずつ配る。
3. 剰余が同じ場合は `sha256(seed ‖ member_id)` の順で決める。入力順に依存しない決定論的なタイブレーク。
4. 負の total は絶対値を配分して符号を戻す(返品・割引)。

保証(Hypothesis で検証済み):

- 合計が total と一致する。
- 各人は理想値の floor か ceil になる(差は 1 円以内)。
- 重み 0 の人は 0 円。
- 並び順を変えても結果は同じ。

`seed` には支出 ID を使う。新規の支出は、クライアントが `client_expense_id` を生成してプレビューと登録の両方に使う。これにより、プレビューと確定で端数を受け取る人が一致する。

## 3. 分割方式

| mode | 入力 `shares[].value` | 検証 |
|---|---|---|
| `equal` | 無視(キー=負担者) | 1 人以上 |
| `weight` | 比率(0 以上の整数) | 合計 > 0 |
| `units` | 個数 | 合計 > 0 |
| `percentage` | basis point(0.01%単位の整数) | 合計 = 10000 |
| `exact` | 金額 | 合計 = 支出額(違えば `SHARE_SUM_MISMATCH`) |
| `itemized` | `receipt`(下記) | 下記 |

- 立替(`payers`)は複数人でもよい。立替額の合計は支出額と一致しなければならない(`PAYER_SUM_MISMATCH`)。
- 同じメンバーを重複して指定すると `DUPLICATE_MEMBER`。グループ外のメンバーを指定すると `UNKNOWN_MEMBER`。

## 4. 商品別割り勘(`itemized`)

```
receipt = { receipt_total_minor, items[], adjustments[] }
item    = { id, name, line_minor(返品は負), quantity, tax_group("8"|"10"|null), allocation }
allocation = { mode: equal|weight|units|exact, shares[] }
adjustment = { id, kind: tax|discount|coupon|service_charge|rounding|other, amount_minor, rule }
rule = { type: proportional|equal|members|exact, scope_item_ids?, scope_tax_group?, member_ids?, amounts? }
```

- **数量単位の割当**: `units` の場合、配った個数の合計は商品の数量と一致しなければならない(`UNITS_MISMATCH`)。例: ビール 3 杯 1,650 円を A 2 杯・B 1 杯 → A 1,100 円 / B 550 円。
- **調整額の符号**: 税・サービス料は 0 以上、割引・クーポンは 0 以下(`ADJUSTMENT_SIGN_INVALID`)。符号の取り違えによる二重計上を防ぐ。
- **配分ルール**
  - `proportional`: 対象商品に対する各人の負担額に比例して配る。対象は `scope_item_ids`、`scope_tax_group`、全商品のいずれか。外税 8%/10% の混在は、税率グループごとに調整行を分ければ、各税率の商品だけに配分される。返品で基準額が負になる場合は比例配分できないため、`ADJUSTMENT_RULE_INVALID` を返し、別のルールを明示的に選んでもらう。
  - `equal`: 対象商品の負担者で均等に分ける。
  - `members`: 指定したメンバーで均等に分ける。
  - `exact`: 金額を指定する(合計が一致することが必要)。
- **内税レシート**: 税額は総額にすでに含まれているので、調整行には入れない(入れると二重計上になる)。税率グループは表示・検証用として保持する。

### レシート総額との照合(自動補正しない)

`Σ items.line_minor + Σ adjustments.amount_minor == receipt_total_minor` が成り立たなければ、`RECEIPT_TOTAL_MISMATCH` を返し、差額(`difference`)も一緒に返す。**エンジンは差額を勝手に配分しない**。確定するには、ユーザーが明細を訂正するか、`kind=other` の差額調整行を明示的に追加する必要がある。未割当の商品は `ITEM_UNALLOCATED` で示す。

プレビュー(`/expenses/preview`)は問題点(issues)の一覧を返し、それを表示に使う。確定(`POST /expenses`、`PUT`)は、問題が 1 件でもあれば 422 で中止する。

### 確定前の不変条件(`verify_receipt` / `compute_expense`)

1. 各商品について、割当の合計 = 行金額。
2. 各調整額について、配分の合計 = 調整額。
3. 内訳の合計 = レシート総額。
4. 全員の負担額の合計 = レシート総額 = 支出額。

どれか 1 つでも破れた場合は `InvariantViolation` となる。API は 500 `CALCULATION_FAILED` を返し、トランザクション全体をロールバックする。

## 5. 純残高

`net = Σ立替 − Σ負担 + Σ送金済み(支払側) − Σ受取済み(受取側)`。正の値は受け取る側を表す。`Σ net == 0` でなければ `InvariantViolation`。

「送金済み」として数えるのは、`reported_paid`(送金申告済み)と `recipient_confirmed`(受取人確認済み)の送金だけ(**ロック済み送金**)。Phase 5 では銀行照合が `matched` の送金も加える。

#### 送金申告と受取確認の区別(2026-10-20 追加、P01)

- 再計算の扱いは変えない: `reported_paid` も `recipient_confirmed` もロック済みとして残高から差し引く(後方互換)。
- ただし表示と API では次のように分ける。どちらも銀行での確認ではなく、着金の事実を示さない。

| 状態 | 意味 | `status_label` | `is_recipient_confirmed` |
|---|---|---|---|
| `unpaid` | 未払い | 未払い | false |
| `reported_paid` | 支払者の自己申告。受取人は未確認 | 送金申告済み(受取人は未確認) | false |
| `recipient_confirmed` | 受取人の手動確認(申告なしでも可) | 受取人が確認済み | true |
| `superseded` | 新しい精算案に置き換え | 新しい精算案に置き換え済み | false |

- 残高の行には、`settled_*_minor`(ロック済み合計)のうち申告のみで未確認の額を `reported_unconfirmed_out_minor` / `reported_unconfirmed_in_minor` として示す。
- 確定バッチには `progress`(未払い・申告済み未確認・受取確認済みの件数と金額)、精算プレビューには `locked_progress` を付ける。
- 状態は前に戻らない: 受取確認後の申告は冪等に `recipient_confirmed` を返す。申告は支払者本人、確認は受取人本人だけ(他方は 403、当事者以外は 404)。

## 6. 送金回数の最適化

### 前提(API の `optimization.assumptions` で毎回返す)

| 項目 | 値 |
|---|---|
| 送金先の制限 | なし(誰から誰へでも送金できる) |
| 通貨 | JPY のみ・整数円 |
| 手数料 | 考慮しない |
| 1 件あたりの送金上限 | なし |
| ロック済み送金 | 変更しない(残った残高だけを最適化する) |

### 厳密解法

非ゼロ残高の人数を n、残高を「合計 0 の部分集合」に分割したときの最大個数を k とする。

- 最小送金件数は **n − k**。
  - 各部分集合は |S|−1 件で解消できる。
  - 逆に、どんな送金計画でも、その連結成分は合計 0 の部分集合になる。
- k は部分集合 DP で求める:`dp[mask] = max_i dp[mask∖i] + [sum(mask)=0]`、計算量は O(n·2ⁿ)。
- 前処理として、`+a` と `−a` の組を先に独立した部分集合として取り出す。この操作は最適性を損なわない。
- 各部分集合の中は、債務者・債権者の 2 ポインタで |S|−1 件の送金にする。
- 送金件数が n − k と一致しない場合は `InvariantViolation` とする。

### フォールバック

- 前処理後の人数が `settlement_exact_max_participants`(既定 20)を超える場合 → `fallback_reason=participant_limit`。
- 計算時間が `settlement_time_limit_ms`(既定 1500ms)を超えた場合 → `fallback_reason=time_limit`。
- フォールバックでは貪欲法(債務の最大者 → 債権の最大者)を使う。残高は必ず 0 になるが、件数が最小であることは保証しない。

### 表示ルール

| 条件 | `display_label` |
|---|---|
| 厳密解(`is_exact_minimum=true`) | 「最小送金回数」 |
| フォールバック | 「送金回数を削減」(`minimum_transfer_count=null`) |
| 送金不要 | 「精算の必要はありません」 |

API は次の項目も返す:

- `naive_transfer_count`: 支出ごとに「負担者 → 立替者」の債務を作り、2 人の間だけで相殺したときの件数。
- `optimized_transfer_count`、`method`、`elapsed_ms`。

### 実測(Apple M3、CPython 3.13、ランダム残高)

| 非ゼロ人数 | 最悪時間 |
|---|---|
| 14 | 9 ms |
| 16 | 39 ms |
| 18 | 169 ms |
| 20 | 772 ms |

本番サーバーの CPU で改めて測り、上限を調整すること。

### テストでの扱い(CI の速度差で結果が変わらないようにする)

- 2026-10-19 の GitHub Actions(push 実行)で、20人の固定入力が `time_limit` になり失敗した。手元(M3)では約 750ms だが、
  8 CPU に 16 プロセスの負荷をかけると毎回 1500ms を超えてフォールバックすることを再現した(遅い CI ランナーと同じ状況)。
- 時間制限を延ばしたり skip したりせず、テストを次の 3 つに分けた(`tests/domain/test_settlement.py`):
  1. **厳密性**: 20人の固定入力 2 件(CI で失敗した seed 3 と、和0の部分集合を 4 つ埋め込んだ入力)を、十分な時間制限で解き、
     件数を DP とは別手順のオラクル(和0の部分集合の全列挙+最大分割の探索)と照合する。
  2. **既定 1500ms の契約**: 厳密解なら件数がオラクルと一致、時間切れなら `greedy_fallback` / `time_limit` /
     `minimum_transfer_count=null` で、どちらでも残高が 0 になる。
  3. **時間切れ分岐**: 呼ばれるたびに 1 秒進む時計に差し替え、実時間に依存せずフォールバックを確かめる。

### 検証(送金後に全員の残高が 0)

`verify_transfers` で次を確認する:

- 送金額が正の整数であること。
- 自分自身への送金がないこと。
- 未知のメンバーが含まれないこと。
- 送金を適用した後、全員の残高が 0 になること。

確定(`POST /settlement-batches`)の直前にも同じ検証を再度実行する。

## 7. 同時実行・冪等性・版管理

| 対象 | 仕組み |
|---|---|
| 支出の作成 | `Idempotency-Key` 必須。同じキーで同じ内容なら保存済みの応答を返し、内容が違えば `409 IDEMPOTENCY_CONFLICT`。同時に送られた場合は一意制約で待ち合わせる |
| 支出の更新・削除 | `If-Match: <version>` 必須(ない場合は 428、版が違えば 409 `REVISION_CONFLICT`)。全版を `expense_revisions` に入力仕様と計算結果のスナップショットとして残す |
| 台帳の版 | 支出が変わるたびに `groups.ledger_version` を +1 する |
| 精算の確定 | `expected_ledger_version` が一致しなければ 409。同じ台帳版での二重確定は 409 `SETTLEMENT_ALREADY_CONFIRMED`。グループ行の `FOR UPDATE` ロックと「有効なバッチは 1 つだけ」の部分一意インデックスで二重確定を防ぐ |
| 再確定 | 旧バッチの `unpaid` 送金だけを `superseded` に変える。申告済み・確認済みの送金と旧バッチは変更せずに残す(履歴) |
| 状態遷移 | `transfer_events` に記録する。`report-paid` は支払者本人、`confirm-received` は受取人本人のみ。同じ操作の繰り返しは冪等 |
