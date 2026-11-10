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

## 削除ジャーナル(STEP 01、2026-11-04、`docs/DATA_MODEL.md` §11)

- `WARILINK_DELETION_JOURNAL=local:/絶対パス`(本番は必須。未設定なら API が起動せず、`backup` も拒否する)。DB とは別の
  ディスク・保管先に置く。ローカルの実装は外部保管ではない(外部保管は STEP 02 まで `OFFSITE_STORAGE_LIVE=BLOCKED`)。
- 記録は追記だけで、消さない(`journal.jsonl` と `HEAD`、権限 600、ディレクトリ 700)。手で編集しない(連番・hash で検出され、
  復元に使えなくなる)。
- 確認: `cd apps/api && python -m app.modules.identity.retention journal-verify`
  (終了コード 0 = 正常 / 1 = 未確定あり / 2 = 壊れている / 3 = 未設定・読めない)。
- 未確定の確定: `journal-reconcile`(DB に操作の行がある = commit 済みのものだけを確定する)。`purge-images` の実行のたびにも行う。
- 監視への組み込み(`python -m app.ops.monitor`)は STEP 04。それまでは `journal-verify` を定期に実行し、終了コード 1・2・3 を
  通知する(出力は件数とチェックポイントだけ。ID・パスは出さない)。
- DB に操作の行が無い未確定(commit されなかった)は自動では取り消さない。**現用の DB で、その操作の後に古いバックアップから
  戻していないことを確かめてから** `journal-abort-absent --older-than-hours 24 --confirm-live-db` で取り消しとして記録する
  (戻した DB で実行すると、commit 済みの削除を取り消しと記録してしまう)。
- ジャーナル導入前の削除済み一覧は `journal-import-legacy` で移す(移すまで、その行は期限削除しない)。

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
| `WARILINK_BACKUP_OFFSITE` | 外部の保管先(`local:ディレクトリ` / `s3://バケット/接頭辞`、STEP 02)。書き込み専用の資格情報・バージョン管理とオブジェクトロック(30 日)・保管先の暗号化・復元用の読み取り権限を別担当にする | コードは実装済み・実 AWS は未接続(`OFFSITE_STORAGE_LIVE=BLOCKED`) |
| `WARILINK_BACKUP_KMS_KEY_ID` | バックアップの鍵を AWS KMS にする(エイリアス・鍵 ID・ARN。manifest には ARN を書く) | コードは実装済み・実 AWS は未接続(`KMS_LIVE=BLOCKED`) |
| `WARILINK_AWS_LIVE` | `1` のときだけ AWS に接続する(資格情報が環境にあっても、これが無ければ接続しない) | 未設定 |
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
1. `restore --from バックアップ --target-db warilink_restore_… --receipts-target 空のディレクトリ --tombstones $WARILINK_TOMBSTONE_FILE --journal $WARILINK_DELETION_JOURNAL`
   - 削除ジャーナルが壊れている・別のジャーナル・バックアップの時点より短い・未確定の削除がある → DB を作る前に拒否される
     (未確定は本番の現用 DB で `journal-reconcile` してから)。結果の `journal.status` が `verified` であることを確認する。
   - STEP 01 より前のバックアップ(チェックポイントが無い)は、本番では `--accept-legacy-backup` を付けたときだけ復元する
     (`journal.status` が `verified_without_checkpoint` / `unverified_legacy` として残る)。
2. 削除済み一覧がバックアップの時点より古い・指定が無い → 拒否される。最新の一覧(別の保管先の複製)を探す。
3. 認証に失敗(改ざん・鍵違い・欠け)→ DB には何も作られない。別の日のバックアップを試し、原因(保管先・鍵)を調べる。
4. 撮影時点で画像が欠けていたバックアップ → 欠けを承知のうえで `--accept-missing-images`。
5. 結果の `checks` がすべて true であることを確認(`tombstones_reapplied`・`deletions_hold` を含む)。

### 目標値(実測ではない)
RPO 24 時間・RTO 4 時間は目標。本番の量・保管先からの取り出し時間を含めた計測は未実施(`RPO_RTO_PROVEN=BLOCKED`)。

### バックアップの実績と復元の制約

ローカルだけの開発用バックアップの成功は、外部保管を含むバックアップの成功記録にはしない。本番の成功記録はダンプと画像の完全性確認、外部へのアップロードとハッシュ照合がすべて成功し、状態を DB に記録できた場合だけ更新する。状態の記録に失敗した場合は成功を返さない。終了コードは 0 (成功)、1 (画像不足)、3 (拒否・状態記録失敗)、4 (本番の外部保管未設定または外部保管失敗)。

外部で独立管理される削除済み一覧の最新性は現在の基盤では確認できず、バックアップ後のすべての削除の復元時再適用は保証できない。本番復元は BLOCKED。ライブ KMS、実際の外部保管、本番復元は未検証。

## S3・KMS の外部保管(STEP 02、2026-11-06。実 AWS は未接続)

コード(`apps/api/app/ops/s3_storage.py`・`aws_kms.py`・`scripts/warilink_fetch_backup.py`)とオフラインのテストまで。
実際のバケット・鍵・IAM は**作っていない・設定していない**。以下は導入先で運用者が行う手順で、承認・予算・検証用の
資格情報がそろうまで `OFFSITE_STORAGE_LIVE` / `KMS_LIVE` は BLOCKED。

### 準備(運用者)
1. `cd apps/api && uv sync --group aws`(boto3。API のコンテナ・CI には入れない)。
2. バケット(バックアップ用と削除ジャーナル用は分けてよい。接頭辞は必須):
   - バージョン管理: 有効。Object Lock: 有効、既定の保持は COMPLIANCE(または GOVERNANCE)30 日以上。
   - 既定の暗号化: SSE-KMS。パブリックアクセスはすべてブロック。
   - 本番の `backup` は `check_bucket()` でこの 3 点を確かめ、満たさなければ拒否する(設定はコードで変えない)。
   - 30 日を過ぎたバックアップの削除は、ライフサイクル規則(非現行の世代を含む)で行う。`prune` はローカル専用。
3. KMS の鍵: 対称暗号(SYMMETRIC_DEFAULT、ENCRYPT_DECRYPT)、自動ローテーションを有効にする。エイリアスを
   `WARILINK_BACKUP_KMS_KEY_ID` に設定。鍵を入れ替えたら、古い鍵は 30 日(保持期間)を過ぎるまで無効にしない
   (manifest の ARN で開くため)。
4. IAM(最小の範囲。ロール・ポリシーは導入先で作る。このリポジトリは何も適用しない):

| 実行する役割 | S3 | KMS |
|---|---|---|
| バックアップ(日次) | 対象の接頭辞への `s3:PutObject`・`s3:GetObject`・`s3:GetObjectVersion`(読み戻しの検証)、バケットの `s3:GetBucketVersioning`・`s3:GetBucketObjectLockConfiguration`・`s3:GetEncryptionConfiguration`。`s3:DeleteObject*`・`s3:PutBucket*`・`s3:BypassGovernanceRetention` は**与えない** | 対象の鍵への `kms:Encrypt`・`kms:Decrypt`(読み戻しの検証)・`kms:DescribeKey`。暗号化コンテキストの条件 `kms:EncryptionContext:purpose = warilink-backup-dek` を付ける |
| 削除ジャーナル(API・定期ジョブ) | ジャーナルの接頭辞への `s3:PutObject`・`s3:GetObject`・`s3:GetObjectVersion`・`s3:ListBucketVersions`(接頭辞の条件付き)。削除・上書き禁止の解除は与えない | なし(SSE-KMS のバケットなら、その鍵の `kms:GenerateDataKey`・`kms:Decrypt` を S3 経由に限って) |
| 取得・復元の検証(別担当) | `s3:GetObject`・`s3:GetObjectVersion`・`s3:ListBucketVersions` だけ | `kms:Decrypt` だけ(同じ暗号化コンテキストの条件) |

### 日次のバックアップ(S3)
- `WARILINK_AWS_LIVE=1 WARILINK_BACKUP_OFFSITE=s3://… WARILINK_BACKUP_KMS_KEY_ID=alias/… scripts/warilink_backup.py backup …`
- 置いた後に受領記録(`upload-receipt.json`: 各ファイルの世代と SHA-256)を置き、その世代を S3 から読み戻して
  SHA-256・暗号の認証・ファイルの一覧・削除ジャーナルのチェックポイントを確かめる。確かめられなければ終了コード 4 で、
  `maintenance_runs` に成功を記録しない。
- 出力の `offsite.prefix` と `offsite.receipt`(受領記録の世代と SHA-256)を運用記録に残す(取得に使う)。

### 取得と検証(復元の前に、別担当が行う)
1. `WARILINK_AWS_LIVE=1 scripts/warilink_fetch_backup.py --from s3://… --backup <offsite.prefix>
   --receipt-version <offsite.receipt.version_id> --receipt-sha256 <offsite.receipt.sha256> --out 空のディレクトリ`
   (終了コード 0 = 検証済み / 1 = 問題あり / 3 = 拒否)。
2. 検証済みなら `scripts/warilink_backup.py restore --from <取得先> --target-db warilink_restore_… …`
   (鍵は manifest の `key.provider` で選ぶ。環境変数の鍵で作った過去のバックアップも開ける)。

### 削除ジャーナル(S3)
- `WARILINK_DELETION_JOURNAL=s3://バケット/接頭辞`(本番の起動の検査は形式だけを確かめる。接続は最初に使うとき)。
- 1 件 = 1 オブジェクト(`records/000000000001.json` …)を上書き禁止の条件付き書き込み(If-None-Match)で置き、
  `HEAD` を条件付きで更新する。読むときは全世代を一覧し、記録に 2 つ以上の世代・削除の印があれば拒否する。
- Object Lock は上書き・削除を防ぐ設定の 1 つで、これだけで全履歴の完全性を保証するものではない。連番・hash の連鎖・
  HEAD の照合と、バックアップのチェックポイントを併用する。記録の末尾を HEAD ごと失った場合は検出できない。

## 本番構成(STEP 03)

本番の構成・ロール・起動前検査・導入/更新/戻し方/障害/秘密の入れ替えは `infra/ops/production-runbook.md`。
ロールの作成は `infra/ops/postgres-roles.sql`(導入時に 1 回、ブートストラップの管理者で。既存の DB では実行しない)。

## 定期ジョブ・監視・復元の検証・負荷(STEP 04)

- systemd のユニット: `infra/systemd/`(インストールしない。手順は `infra/ops/production-runbook.md`「定期ジョブと監視」)。
- 隔離した復元の検証: `scripts/warilink_restore_verify.py`(既定は計画だけ、`--execute` で実行。一時コンテナ
  `warilink-drill-*` / `warilink-restore-*` と `warilink_restore_*` の新しい DB にだけ復元する)。
- 負荷の簡易確認: `scripts/warilink_load_smoke.py --base-url http://127.0.0.1:<ポート> --execute`(localhost の
  開発・テストの API だけ。本番には使わない。上限: 利用者 50・並列 10・支出 20 件・300 秒)。
