# レシート読み取り(OCR)パイプライン仕様(Phase 3 実装済み)

実装: `apps/api/app/ocr/`(API と DB モデルを共有)、ワーカーのコンテナは `apps/ocr-worker/Dockerfile`。

## 1. 全体の流れ

```
[画面] 画像を選ぶ
  → POST /groups/{id}/receipts(API: 検証・EXIF除去・保存・利用枠を1枚消費・ジョブ登録)
  → receipt_jobs(PostgreSQL のキュー)
  → OCR ワーカー(別プロセス): 前処理 → PaddleOCR → 日本語レシート解析 → ocr_runs に生結果と下書きを保存
  → [画面] 下書きを画像と見比べて確認・訂正 → 負担者を選ぶ → POST /expenses(source_receipt_id 付き)
  → Phase 1 のエンジンで検算して確定。下書きとの差分を ocr_corrections に記録(未検証)
```

- 読み取り結果は常に**下書き**。負担者は自動で決めない。合計が合わない場合は差額を表示し、補正しない。
- ワーカーの heartbeat が `ocr_worker_heartbeat_ttl_seconds`(既定 90 秒)以内に無い場合は、アップロードを
  503 `OCR_UNAVAILABLE` で断り、画面は「停止中・手入力してください」と表示する(待ち続けるジョブを作らない)。

## 2. アップロード(API 側、Pillow のみ)

| 検査 | 内容 |
|---|---|
| サイズ | `receipt_max_bytes`(既定 10MB)超は 413 |
| 形式 | JPEG / PNG / WebP のみ(実体を Pillow で検査。拡張子・Content-Type は信用しない)。HEIC は 415 |
| 展開爆弾 | `receipt_max_pixels`(既定 4,000 万画素)超は拒否 |
| メタデータ | EXIF の向きを反映したうえで、EXIF・ICC を付けずに JPEG で保存し直す(位置情報を保存しない) |
| 重複 | 元画像の SHA-256 が同じグループ内で一致すれば既存のレシートを返す(枠を消費しない)。同時アップロードは一意インデックスで判定 |
| 保存 | `receipt_storage_dir/{receipt_id}.jpg`(DB には置かない)。一時ファイル→rename で書き込む |
| 保存期間 | `receipt_image_retention_days`(既定 30 日)。`python -m app.ocr.cli purge-images` で画像だけ削除(下書き・訂正記録は残る) |
| 閲覧 | `GET .../receipts/{id}/image` はグループのメンバーのみ(他は 404)、`Cache-Control: private, no-store` |

## 3. ジョブ(DB キュー)

- 取得: `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING`。同時に動く複数ワーカーが同じジョブを取らない。
- lease: 取得時に `lease_expires_at = now + ocr_job_lease_seconds`(既定 120 秒)。ワーカーが落ちて期限が切れたら別ワーカーが再取得する。
- 書き込みの保護: 完了・失敗の記録は「`state=running` かつ `lease_owner` が自分」のときだけ。期限切れ後に遅れて戻った古いワーカーの結果は捨てる。
- 再試行: OCR エンジン内部の例外は `ocr_job_max_attempts`(既定 3)まで、`backoff × 試行回数` 秒後に再試行。超えたら `dead`。
- 画像不良(読めない・小さすぎる)は再試行せず `failed`。
- `dead` / `failed` のときは利用枠を返還し、画面で手入力へ案内する。返還はレシートごとに 1 回だけ(一意制約)。

## 4. 前処理・品質チェック(OpenCV、ワーカー側)

- 長辺 2000px を超える画像は縮小。短辺 300px 未満は `TOO_SMALL` で不合格。
- 暗さ・コントラスト・ぼけ(ラプラシアン分散)を計測して `ocr_runs.quality` に保存し、しきい値未満なら警告を表示する(不合格にはしない)。
- しきい値(`preprocess.THRESHOLDS`)は合成画像で決めた暫定値。**実レシートでの調整は未実施**。
- 反射(白飛び)の検出は、白い紙と区別できず誤検出したため実装していない。
- 傾き補正・文書の歪み補正(PaddleOCR の doc orientation / unwarping)は無効。効果は未評価。

## 5. OCR エンジン(交換可能)

`OcrAdapter` プロトコル(`recognize(image) -> [OcrToken(text, score, bbox)]`, `model_info()`)。現在の実装は
`PaddleOcrAdapter` のみ。

| 項目 | 値 |
|---|---|
| ライブラリ | paddleocr 3.7.0 / paddlepaddle 3.3.1(CPU)/ paddlex 3.7.2 |
| モデル | `PP-OCRv6_medium_det` + `PP-OCRv6_medium_rec`(名前で固定。Apache-2.0) |
| 取得元 | 初回に PaddleX が公式配布元から自動取得。コンテナではビルド時に取得してイメージに含める |
| 記録 | `ocr_runs` に モデル名・版・パラメータ・重みファイルの SHA-256(先頭16桁)・処理時間・エラー |
| スレッド | `ocr_cpu_threads`(未指定なら min(4, CPU数))。**CPU 数を超えると linux/aarch64 で推論中に SIGSEGV**(PaddleX の既定 10 で発生。2026-10-19 に Docker Desktop の 8CPU VM で確認) |

### 動作確認した環境(2026-10-19)

| 環境 | 結果 |
|---|---|
| macOS arm64(M3)/ Python 3.13 / PyPI の wheel | 動作。合成レシート 1 枚あたり約 2.6〜3.7 秒(CPU) |
| linux/arm64 コンテナ(python:3.13-slim)/ Paddle 公式配布元の aarch64 wheel | 動作(cpu_threads ≤ CPU 数)。約 3.4 秒/枚 |
| linux/amd64 | **未確認** |

## 6. 日本語レシート解析(ルールベース、`parser.py`)

1. トークンを縦方向の重なりで行にまとめる(NFKC 正規化、全角数字・¥記号の統一)。
2. 行の役割を判定: 商品 / 小計 / 税(外税・税率別)/ 内税表示 / 合計 / 値引・割引 / クーポン / サービス料 / 支払・釣銭等(無視)/ 日付。
3. 金額: 行末寄りの「¥」「円」「桁区切り」付きの数値を優先。`2¥1,300` のような数量との連結、`▲100` `-¥100` の負号に対応。
   長音記号「ー」は負号とみなさない(コーヒー等)。
4. 数量: `@650 x2`、`2個`、`x2`、名前末尾の数字。単価×数量と金額が合わない行は「要確認」。
5. 2 行に分かれた商品(名前の行+金額の行)と、傾きで「小計」と金額が別行になった場合(どちらの順でも)を結合。
6. 軽減税率の記号(※・軽)→ 8%。8% と 10% の税行が両方あれば記号なしの商品を 10% とする。
7. 税の扱い: `明細+調整+税 = 合計` なら外税として税を調整行にする。`明細+調整 = 合計` または「内税」表記なら内税として税行を調整に入れない(二重計上防止)。
8. 合計が見つからない・合わない場合は警告と差額を返す(補正しない)。確信度が `ocr_low_confidence`(既定 0.90)未満の行は「要確認」。
9. 合計より後の行(お預り・お釣り・ポイント等)は商品にしない。

出力(`ocr_runs.draft`): `items[{id, name, quantity, line_minor, tax_group, confidence, needs_review, reasons, bbox, source_lines}]`,
`adjustments[...]`, `subtotal_minor`, `total_minor`, `tax_mode`, `difference_minor`, `warnings`, `lines[行ごとの役割判定]`, `image_scale`。

## 7. 確認画面

- 画像の上に各行の bbox を重ねて表示。「要確認」の行は橙色。入力欄にフォーカスすると対応する bbox を強調。
- 下書きから商品・調整行・合計・店名を入力欄に入れる。負担者は空欄(未割当のままでは登録不可)。
- 登録時に `source_receipt_id` と確認にかかった時間(`review_elapsed_ms`)を送る。

## 8. 訂正の記録と評価

- `ocr_corrections`: 下書きと確定内容の差分を 1 項目ずつ保存。分類は
  `recognition`(文字・金額の誤認識)/ `structure`(税率・種類など対応付けの誤り)/ `missing`(読み落とし)/ `spurious`(余分な行)。
- すべて `label_status=proposed`。**利用者の訂正をそのまま正解ラベルにしない**。検証者の確認と、利用者の同意(Phase 6)がそろうまで評価・学習に使わない。
- 評価 CLI(`python -m app.ocr.cli evaluate DIR`)は `label_status` が `verified`(検証済み)または `synthetic`(合成)のラベルだけを使う。

| 指標 | 定義 |
|---|---|
| line_recall / line_precision | (商品名・数量・金額)が完全一致した行 / 正解の行数・読み取った行数 |
| receipt_exact_rate | 全商品・調整額・合計がすべて一致したレシートの割合 |
| total_correct_rate | 合計金額が一致した割合 |
| amount_error_rate | 正解に存在しない金額を読み取った行 / 読み取った行 |
| latency_ms_mean / p95 | 前処理+認識の時間 |
| 平均確認時間 | 実運用の `receipts.review_elapsed_ms`(`cli stats`)。オフライン評価では測れない |

- `--baseline 前回結果.json` を渡すと、同じデータセット(画像+ラベルのハッシュ一致)で指標が1つでも悪化した候補を `rejected`(終了コード 2)とする。データセットが違えば `incomparable`。
- `--reuse-tokens` で保存済みの認識結果を使い、パーサーだけを評価できる(パーサー調整用)。

### 合成データでの結果(参考。実レシートの精度ではない)

合成レシート生成(`cli synth`)は、このパーサーと同じ作者が作ったもので、配置・書式の多様性は実レシートより
はるかに小さい。以下は「パイプラインが動くこと」の確認であり、精度の主張には使わない。

| データ | 件数 | receipt_exact_rate | 備考 |
|---|---|---|---|
| synth easy(seed 7, macOS) | 20 | 1.00 | |
| synth hard(seed 11, 傾き・ぼけ・ノイズ, macOS) | 30 | 1.00 | 初回 0.967 → 小計と金額の行分かれを修正 |
| synth hard(seed 5, linux/arm64 コンテナ) | 10 | 1.00 | |

## 9. 利用者(あなた)が行う作業

- 実レシートでの精度調整・目視確認: `cli recognize 画像 --json` で 1 枚ずつ確認し、正解ラベル(`label_status: "verified"`)を付けた
  データセットで `cli evaluate` を実行する。ラベル形式は合成データの JSON と同じ(`items[{name, quantity, line_minor, tax_group}]`, `adjustments[{kind, amount_minor}]`, `total_minor`)。
- 調整候補: `ocr_low_confidence`、前処理のしきい値、`ocr_det_model` / `ocr_rec_model`(例: `PP-OCRv6_small_*`)、パーサーのキーワード。
- 実レシート画像・ラベルはリポジトリに入れない(個人情報)。
