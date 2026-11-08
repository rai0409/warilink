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
