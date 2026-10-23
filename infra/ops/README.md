# 運用ジョブ(導入先で設定する。このリポジトリは何も自動登録しない)

| ジョブ | コマンド | 推奨頻度 | 失敗時 |
|---|---|---|---|
| 期限切れ画像の削除+整合性検査 | `scripts/warilink_purge_job.sh` | 1 日 1 回 | 終了コード 1。`WARILINK_ALERT_CMD` に要約を渡す |
| バックアップ | `scripts/warilink_backup.py backup --db … --receipts-dir … --out …` | 1 日 1 回(保管先が決まってから) | 終了コード 1(撮影時点で画像が欠けている)/ 3(拒否) |
| 復元訓練 | `scripts/warilink_backup.py drill --workdir 空のディレクトリ` | 月 1 回 | 終了コード 1 |

- cron の例: `warilink-purge.cron.example`(コメントアウト済み)。compose・systemd への組み込みはしていない。
- `purge-images` は何度実行してもよい(削除済みは対象外。ファイルを消せなかった行は DB に記録せず次回再試行)。
- `check-storage` は読み取り専用。不一致(DB は画像ありなのにファイルが無い / DB に無いファイル)は終了コード 2。
- Compose の画像は名前付きボリューム `warilink_receipts`。バックアップでは `docker compose cp api:/data/receipts …` などで
  ディレクトリに取り出してから `--receipts-dir` に渡す(ボリュームを直接書き換えない)。
- 復元は `warilink_restore_*` の新しい DB と空ディレクトリにだけ行う。本番への切り戻し手順は、保管先・暗号化鍵・停止手順が
  決まってから別途作る(**未検証**)。
- バックアップには個人情報(レシート画像)が含まれる。暗号化と外部保管は本番の保管先が決まるまで**未実施**。
