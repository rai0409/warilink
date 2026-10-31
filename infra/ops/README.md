# 運用ジョブ(導入先で設定する。このリポジトリは何も自動登録しない)

| ジョブ | コマンド | 推奨頻度 | 失敗時 |
|---|---|---|---|
| 保持期限の処理+整合性検査+削除済み一覧の書き出し | `WARILINK_TOMBSTONE_FILE=… scripts/warilink_purge_job.sh` | 1 日 1 回 | 終了コード 1。`WARILINK_ALERT_CMD` に要約を渡す |
| バックアップ | `scripts/warilink_backup.py backup --db … --receipts-dir … --out …` | 1 日 1 回(保管先が決まってから) | 終了コード 1(撮影時点で画像が欠けている)/ 3(拒否) |
| 復元訓練 | `scripts/warilink_backup.py drill --workdir 空のディレクトリ` | 月 1 回 | 終了コード 1 |
| 古いバックアップの削除 | `scripts/warilink_backup.py prune --root バックアップの置き場所` | 1 日 1 回(バックアップの後) | 終了コード 3(置き場所が無い) |

- cron の例: `warilink-purge.cron.example`(コメントアウト済み)。compose・systemd への組み込みはしていない。
- `purge-images` は何度実行してもよい(削除済みは対象外。ファイルを消せなかった行は DB に記録せず次回再試行)。
  実行のたびに成功・失敗の時刻を `maintenance_runs` に残す。画面「データの保存期間」は直近 48 時間以内の成功があるときだけ
  「動いています」と表示する(ジョブを登録しない環境では「確認できません」と表示し、自動削除を約束しない)。
- `check-storage` は読み取り専用。不一致(DB は画像ありなのにファイルが無い / DB に無いファイル)は終了コード 2。
- Compose の画像は名前付きボリューム `warilink_receipts`。バックアップでは `docker compose cp api:/data/receipts …` などで
  ディレクトリに取り出してから `--receipts-dir` に渡す(ボリュームを直接書き換えない)。
- 復元は `warilink_restore_*` の新しい DB と空ディレクトリにだけ行う。本番への切り戻し手順は、保管先・暗号化鍵・停止手順が
  決まってから別途作る(**未検証**)。
- バックアップには個人情報(レシート画像)が含まれる。暗号化と外部保管は本番の保管先が決まるまで**未実施**。

## データの保持(STEP4、`docs/DATA_MODEL.md` §11)

- `purge-images` は、画像(30 日)のほか、使用済み・期限切れの challenge、失効・期限切れから 30 日のセッションと招待、
  1 年を過ぎた監査ログ、30 日を過ぎた削除済み一覧、参加者不在から 30 日・終了から 1 年のグループを消す。何度実行しても同じ結果。
- バックアップは 30 日分を残し、`prune` で古いものを消す。退会・振込先の消去はこれで最長 30 日でバックアップからも消える。
- **復元の手順(必須)**: `restore` の後、DB の外に書き出してある削除済み一覧を再適用する。
  `cd apps/api && WARILINK_DATABASE_URL=<復元先> ../../.venv/bin/python -m app.modules.identity.retention apply-tombstones --in "$WARILINK_TOMBSTONE_FILE"`
  (古いバックアップで戻すと、DB 内の一覧も古くなるため、必ず DB の外の一覧を使う)。何度実行しても同じ結果。
- `WARILINK_TOMBSTONE_FILE` はバックアップと同じ保管先に置く(ID だけで個人情報は含まない)。未設定だと定期ジョブは失敗として通知する。

## 監視と通知(STEP5)

- `scripts/warilink_purge_job.sh` は失敗(画像の削除失敗・DB とファイルの不一致・削除済み一覧の書き出し失敗)を終了コード 1 と
  `WARILINK_ALERT_CMD` で知らせる。通知先(メール・チャット)は導入先で設定する(**未設定**)。
- 外形監視は `GET /api/v1/health`(DB に触れない)を使う。OCR を有効にする場合は `/plans` の `ocr_available` でワーカーの停止に気づける。
- 復元訓練は月 1 回、**本番・既存の DB とは別の使い捨てコンテナ**で行う(例: `--container warilink-drill-db --port 55433`。
  2026-10-29 に一時コンテナで実施し PASS、終了後に削除)。既存のコンテナで `drill` を実行しない(DB の作成・削除を伴う)。
- API は `--no-proxy-headers --no-access-log` で起動する(`docs/SYSTEM_ARCHITECTURE.md` §8)。

## 暗号化バックアップ・監視の手順(PR C、2026-10-31)

### 運用者が決めて設定するもの(未設定の間は該当のゲートが BLOCKED)

| 設定 | 内容 | 状態 |
|---|---|---|
| `WARILINK_BACKUP_KEYS` / `WARILINK_BACKUP_ACTIVE_KEY` | バックアップの KEK(32 バイトの乱数を base64)。シークレットストアで管理し、バックアップの保管先に置かない。入れ替えたら古い鍵 ID は 30 日(保持期間)残す | 未設定(KMS へ移す: `KMS_LIVE=BLOCKED`) |
| `WARILINK_BACKUP_OFFSITE` | 外部の保管先。書き込み専用の資格情報・バージョン管理またはオブジェクトロック(30 日)・保管先の暗号化・復元用の読み取り権限を別担当にする | 未選定(`OFFSITE_STORAGE_LIVE=BLOCKED`) |
| `WARILINK_TOMBSTONE_FILE` | 削除済み一覧(DB の外)。バックアップとは別の保管先にも複製し、30 日以上残す | 未設定 |
| `WARILINK_ALERT_CMD` | 通知コマンド(標準入力で要約を受け取る) | 未設定(`MONITOR_ALERT_LIVE=BLOCKED`) |
| `WARILINK_MONITOR_API_URL` | 外形監視する API の URL(`/api/v1/health`) | 未設定 |
| `WARILINK_RESTORE_DB_PASSWORD` | 復元先 DB の接続(削除の再適用に使う) | 復元のときに設定 |

### 日次
1. `scripts/warilink_purge_job.sh`(保持期限の削除・整合性検査・削除済み一覧の書き出し)
2. `scripts/warilink_backup.py backup --db … --receipts-dir … --out …`(暗号化。外部保管へ送り、成功を `maintenance_runs` に記録)
3. `scripts/warilink_backup.py prune --root …`(30 日を過ぎたもの。manifest の無いもの・知らない形式には触れない)
4. `cd apps/api && python -m app.ops.monitor`(1 時間ごとを推奨。ジョブの最後の成功が 26 時間より古い・DB に届かない・
   `/health` が 200 以外・OCR ワーカーの停止で通知。同じ内容は 6 時間まとめ、直ったら 1 回「回復」を送る)

### 月次
- 復元訓練: `scripts/warilink_backup.py --container warilink-drill-db --port 55433 drill --workdir 空のディレクトリ`
  (既存のコンテナでは行わない。一時コンテナを作り、終わったら消す)。暗号化 backup → 退会 → 削除済み一覧の書き出し →
  restore(削除の再適用)→ 件数・ハッシュ・外部キー・削除が保たれていること → アプリから読み戻し、まで行う。
  2026-10-31 に一時コンテナで実施し PASS(7.7 秒、合成データ 17 行・画像 1 枚)。**本番相当の量での RTO は未計測**。
- 鍵の入れ替えの確認(新しい鍵で作れること・古い鍵の版で過去のバックアップを開けること)。

### 復元の手順(本番の切り戻しは未設計。以下は検証用の復元)
1. `restore --from バックアップ --target-db warilink_restore_… --receipts-target 空のディレクトリ --tombstones $WARILINK_TOMBSTONE_FILE`
2. 削除済み一覧がバックアップの時点より古い・指定が無い → 拒否される。最新の一覧(別の保管先の複製)を探す。
3. 認証に失敗(改ざん・鍵違い・欠け)→ DB には何も作られない。別の日のバックアップを試し、原因(保管先・鍵)を調べる。
4. 撮影時点で画像が欠けていたバックアップ → 欠けを承知のうえで `--accept-missing-images`。
5. 結果の `checks` がすべて true であることを確認(`tombstones_reapplied`・`deletions_hold` を含む)。

### 目標値(実測ではない)
RPO 24 時間・RTO 4 時間は目標。本番の量・保管先からの取り出し時間を含めた計測は未実施(`RPO_RTO_PROVEN=BLOCKED`)。
