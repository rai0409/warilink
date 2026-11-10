# 本番の運用手順(STEP 03、2026-11-08)

単一の Linux VM で `infra/compose.production.yaml`(Caddy + Next.js + FastAPI + PostgreSQL)を動かす前提の手順。
**このリポジトリは何もデプロイしない。** VM・DNS・証明書・AWS は未契約・未作成で、以下のコマンドは導入先の運用者が
実行する例(実行していない)。実稼働の検証が済むまで `FREE_RELEASE=NO_GO`。

## 構成

```
インターネット ─443/80─ proxy(Caddy, HTTPS 終端) ─proxy_web─ web(Next.js) ─web_api─ api(FastAPI) ─api_db─ db(PostgreSQL)
                                                                          (内部ネットワーク。ホストへ公開しない)
```

- 外へ公開するのは proxy の 80/443 だけ。web・api・db は `internal: true` のネットワークにだけ置く(DB の 5432・API の 8000 は
  ホストからも見えない)。
- 送信元 IP: Caddy が `X-Forwarded-For` を接続元で上書きし(利用者の偽の値を捨てる)、Next.js 16 の中継はそれを足し直さずに
  API へ渡す。API は Web の固定アドレス(`172.30.2.2/32`)からの接続のときだけその値を使う。2026-11-08 に一時コンテナで、
  偽の `X-Forwarded-For` / `X-Real-IP` / `Forwarded` がすべて捨てられること、Origin・CSRF のヘッダー・Cookie が変わらずに届き、
  `Set-Cookie`(HttpOnly・Secure・SameSite=Lax)がそのまま返ることを確認した(`docs/TEST_AND_DELIVERY.md`「STEP 03」)。
- アクセスログは出さない(Caddy の `log` なし、uvicorn の `--no-access-log`)。招待のトークンが URL に入るため。
- 無料版の対象外の機能(Passkey・振込先・OCR・広告・課金・銀行連携)は compose で false に固定。OCR のワーカーは置かない。

## 秘密の値(env ファイル)

`/etc/warilink/production.env`(所有者 root、権限 600。リポジトリ・バックアップの保管先に置かない)。

| 値 | 内容 |
|---|---|
| `WARILINK_DOMAIN` | 公開するホスト名(例 `warilink.example.com`。スキーム・ポートなし) |
| `POSTGRES_BOOTSTRAP_USER` / `POSTGRES_BOOTSTRAP_PASSWORD` | DB の初期化とロール作成だけに使う管理者(アプリ・ジョブでは使わない) |
| `WARILINK_APP_DATABASE_URL` | `postgresql+psycopg://warilink_app:<24 文字以上>@db:5432/warilink` |
| `WARILINK_MIGRATOR_DATABASE_URL` | `postgresql+psycopg://warilink_migrator:<別の値>@db:5432/warilink`(マイグレーションのときだけ) |
| `WARILINK_RATE_LIMIT_SECRET` | 32 文字以上の乱数 |
| `WARILINK_DELETION_JOURNAL` | `s3://バケット/接頭辞`(STEP 02。`local:` は同じ VM の全損に耐えないため起動前検査で BLOCKED) |
| `WARILINK_BACKUP_OFFSITE` / `WARILINK_BACKUP_KMS_KEY_ID` / `WARILINK_AWS_LIVE=1` / `AWS_REGION` | バックアップ(STEP 02) |

起動前検査(読み取りだけ。値は出力しない): `make preflight-production ENV_FILE=/etc/warilink/production.env`
(終了コード 0 = PASS / 1 = FAIL / 3 = 確かめられない項目あり)。DB の権限まで確かめるときは、SSH の転送などで DB に届く
ようにして `scripts/warilink_preflight.py --env-file … --check-db --db-host-override 127.0.0.1:<転送先>`。

## 初回の導入(例)

1. VM に Docker と compose を入れ、リポジトリの該当の版を置く。DNS の A/AAAA を VM に向ける。
2. `docker compose -f infra/compose.production.yaml --env-file /etc/warilink/production.env config --quiet`(必須の値の確認)。
3. DB だけを起動: `… up -d db`。
4. ロールと DB を作る(1 回だけ。パスワードは環境変数で渡し、コマンドラインに書かない):
   `docker compose … exec -e WARILINK_DB_NAME=warilink -e WARILINK_MIGRATOR_PASSWORD -e WARILINK_APP_PASSWORD
   -e WARILINK_BACKUP_PASSWORD db psql -v ON_ERROR_STOP=1 -U "$POSTGRES_BOOTSTRAP_USER" -d postgres -f /ops/postgres-roles.sql`
   (`infra/ops/postgres-roles.sql`。アプリのロールは SUPERUSER・CREATEDB・CREATEROLE・BYPASSRLS なし、DDL なし、DML だけ)。
5. マイグレーション(1 回だけ。migrator のロールで行い、終わったら `alembic_version` への書き込みをアプリから外す):
   `docker compose … --profile migrate run --rm migrate`。
6. 起動: `… up -d`。API は起動の前にスキーマが最新(alembic の head)かを確かめ、違えば終了コード 3 で起動しない。
7. 起動前検査を `--check-db` 付きで実行し、すべて PASS であることを確かめる。

## 更新(アプリの新しい版)

1. 新しい版のマイグレーションが**前の版のアプリと両立する**こと(列の追加は NULL 可・既定値つき、削除・名前の変更は 2 回に
   分ける)をレビューで確かめる。両立しない変更は、停止を伴う手順として別に計画する。
2. バックアップを取る(`scripts/warilink_backup.py backup …`。STEP 02 の読み戻しの検証まで成功すること)。
3. イメージを作る: `… build`。
4. `… --profile migrate run --rm migrate`。失敗したら API は前の版のまま(新しい版を起動しない)。
5. `… up -d`(API・web を入れ替える)。起動前検査を実行する。

## 戻し方(ロールバック)

- **アプリだけ戻す**: 前の版のイメージ(タグ)で `up -d`。マイグレーションが前の版と両立していれば、DB はそのまま。
- **本番の DB で `alembic downgrade` を安易に実行しない**(列・表の削除でデータを失う)。戻す必要があるときは、
  更新の前に取ったバックアップを `warilink_restore_*` の新しい DB に復元して検証し(STEP 01・02 の削除ジャーナルの再適用を含む)、
  停止の時間を取って切り替える。切り替えの手順は、保管先と VM が決まってから実地で作る(未検証)。

## 障害(単一の VM)

| 事象 | 影響 | 対応 |
|---|---|---|
| VM の停止・再起動 | サービス停止(冗長化なし) | `restart: unless-stopped` で自動で戻る。戻らなければ `… up -d` |
| ディスクの全損 | DB・画像・証明書を失う | 新しい VM で初回の導入の 1〜4 → 外部保管のバックアップを取得・検証(`warilink_fetch_backup.py`)→ 復元 → 削除ジャーナルの再適用 → 起動。RPO 24 時間・RTO 4 時間は目標で、未計測 |
| DB のボリュームの破損 | DB を失う | 上と同じ(ボリューム `warilink-prod_pgdata`) |
| 証明書の更新の失敗 | 期限切れで接続できない | Caddy が自動で再試行する。`caddy_data` ボリュームを消さない |

ボリューム: `pgdata`(DB)、`receipts`(OCR を止めている間は空)、`caddy_data`(証明書)、`caddy_config`。
`docker compose down -v` はボリュームを消すため、本番では使わない。

## 秘密の入れ替え

| 値 | 手順 |
|---|---|
| `warilink_app` のパスワード | `ALTER ROLE warilink_app PASSWORD …`(ブートストラップの管理者で)→ env ファイルを更新 → `up -d api`(短い停止) |
| `warilink_migrator` のパスワード | 同上(マイグレーションのときだけ使うので停止なし) |
| `WARILINK_RATE_LIMIT_SECRET` | env ファイルを更新して `up -d api`(回数制限の記録が一度だけ数え直しになる) |
| バックアップの鍵(KMS) | STEP 02(`infra/ops/README.md`)。古い鍵は 30 日を過ぎるまで無効にしない |
| ブートストラップの管理者 | ロール作成の後は使わない。パスワードを変えて保管する |

## 定期ジョブと監視(STEP 04、2026-11-10。インストールしていない)

`infra/systemd/` の service と timer を導入先で入れる(このリポジトリは systemctl を実行しない)。cron の例
(`infra/ops/warilink-purge.cron.example`)と**同時に使わない**。

| ジョブ | 時刻(日本時間) | 内容 | 失敗のとき |
|---|---|---|---|
| `warilink-purge` | 毎日 03:30 | 保持期限の削除・DB と画像の整合性・削除ジャーナルの未確定の確定(compose の api のイメージで実行) | 終了コード 1、`WARILINK_ALERT_CMD` に要約。6 時間に 3 回まで再試行 |
| `warilink-backup` | 毎日 04:00 | 暗号化 → S3 → 記録した世代の読み戻しの検証 → 成否の記録(ダンプは `warilink_backup`、記録は `warilink_app`)→ 30 日を過ぎたローカルの分を削除 | 成功を記録しない(監視の `job:backup` が 26 時間で異常)。12 時間に 2 回まで再試行 |
| `warilink-monitor` | 15 分ごと | ジョブの最後の成功・削除ジャーナル・DB・Web 経由の `/api/v1/health`・本番の設定の欠け | 0 正常 / 1 異常(通知)/ 2 通知の失敗(outbox に残して次回また送る) |
| `warilink-restore-verify` | 毎月 1 日 05:00 | 一時コンテナへ隔離した復元の検証(`scripts/warilink_restore_verify.py --execute`) | `restore-receipt.json` の result が FAIL。本番の DB には触れない |

用意するもの(導入先): 実行ユーザー `warilink`(docker グループ。root 相当なので専用にする)、`/srv/warilink`(リポジトリと
`uv sync --group aws` 済みの `.venv`)、`/etc/warilink/production.env`(compose の秘密)、`/etc/warilink/jobs.env`
(`WARILINK_COMPOSE_FILE`・`WARILINK_ENV_FILE`・ホストの `WARILINK_ALERT_CMD`・`WARILINK_DRILL_DB_PASSWORD`)。
インターネット側の HTTPS の外形監視は、外部の監視サービスで別に行う(コンテナの中からは測れない)。

### 監視が知らせること(`python -m app.ops.monitor`)

- `job:retention_purge` / `job:backup` / `job:tombstone_export`: 最後の成功が 26 時間より古い・失敗が後・未来の時刻。
  削除ジャーナルを使い一覧ファイルを置かない構成では `tombstone_export` は対象外。`job:backup` の成功は、外部保管の
  世代を読み戻して検証できたときだけ記録される(STEP 02)= 外部のバックアップが古いことも、これで分かる。
- `deletion_journal`: `corrupt` / `unreadable` / `misconfigured` / `pending N`(ジャーナルの未確定が 1 時間超)/
  `unconfirmed N`(DB の確定の印が 1 時間超付かない)。
- 本番だけ: `config:alert_cmd` / `config:api_url` / `config:offsite` / `config:deletion_journal` が未設定なら
  `not_configured`、`api:health` も `not_configured`(測れないものを正常にしない)。
- 通知には確認の名前と状態だけを入れる(URL・ID・パス・口座・トークンは入れない)。同じ内容は 6 時間まとめ、直ったら 1 回だけ「回復」。

## 復旧の目標と測り方(STEP 04)

- **目標**(実測ではない): RPO 24 時間(日次のバックアップ)、RTO 4 時間(新しい VM で取得 → 検証 → 復元 → 再適用 → 起動)。
- **測り方**: 月次の `warilink-restore-verify` の `restore-receipt.json` の `measured_restore_seconds`(取得・検証・復元・
  検査の合計)と、各段階の秒数。RTO の全体は、これに VM の用意・DNS の切り替え・起動の確認を足して、訓練の記録で測る。
  RPO は `job:backup` の最後の成功の時刻で決まる(監視の 26 時間の上限)。
- **2026-11-10 の実測(ローカル・合成データ 17 行・画像 1 枚、一時コンテナ)**: 取得・検証・復元・検査で 1.84 秒。
  本番の量・S3 からの取り出し・回線の速さを含まないため、**`RPO_RTO_PROVEN=BLOCKED`**。
- 依存: 復元には (1) 外部保管(S3)の受領記録の世代、(2) バックアップの鍵(KMS の Decrypt 権限、入れ替え前の鍵も 30 日)、
  (3) 削除ジャーナル(未確定が無いこと)の 3 つがそろう必要がある。どれかが欠けたら復元しない(BLOCKED として報告する)。

## 誤った復元・障害のときの止め方

- 復元は常に `warilink_restore_*` の新しい DB へ行い、検証の結果(receipt)が PASS になるまで本番へ切り替えない。
  検証の前に本番の DB を上書き・削除しない。誤って別のバックアップを戻したと分かったら、その復元先を破棄して
  最初からやり直す(本番へ「切り戻す」操作をしない)。
- 段階的に止める条件: `deletion_journal` が `corrupt` → 削除の操作(退会など)が 503 になり、復元も拒否される。
  運用者はジャーナルの保管先を調べ、直るまで復元しない。`job:backup` が 2 日続けて失敗 → 更新(デプロイ)を止める。
  `api:health` の失敗が続く → 外部の監視で利用者への影響を確かめ、VM の障害の手順へ。
- 権限: 復元の検証は取得・復号だけの権限(`infra/ops/README.md` の IAM の表)。本番の DB の管理者の権限は使わない。
