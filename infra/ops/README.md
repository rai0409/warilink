# 運用ジョブ(導入先で設定する。このリポジトリは何も自動登録しない)

| ジョブ | コマンド | 推奨頻度 | 失敗時 |
|---|---|---|---|
| 保持期限の処理+整合性検査+削除済み一覧の書き出し | `WARILINK_TOMBSTONE_FILE=… scripts/warilink_purge_job.sh` | 1 日 1 回 | 終了コード 1。`WARILINK_ALERT_CMD` に要約を渡す |
| バックアップ | `scripts/warilink_backup.py backup --db … --receipts-dir … --out …` | 1 日 1 回(保管先が決まってから) | 終了コード 1(撮影時点で画像が欠けている)/ 3(拒否) |
| 復元訓練 | `scripts/warilink_backup.py drill --workdir 空のディレクトリ` | 月 1 回 | 終了コード 1 |
| 古いバックアップの削除 | `scripts/warilink_backup.py prune --root バックアップの置き場所` | 1 日 1 回(バックアップの後) | 終了コード 3(置き場所が無い) |

- cron の例: `warilink-purge.cron.example`(コメントアウト済み)。compose・systemd への組み込みはしていない。
- `purge-images` は何度実行してもよい(削除済みは対象外。ファイルを消せなかった行は DB に記録せず次回再試行)。
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
