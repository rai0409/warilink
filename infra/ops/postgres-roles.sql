-- WariLink の本番 DB のロール分離(STEP 03)。導入時に 1 回だけ、ブートストラップの管理者で実行する。
-- このリポジトリは自動では実行しない。既存の DB(開発の warilink-db-1 など)では実行しない。
--
--   WARILINK_DB_NAME=warilink WARILINK_MIGRATOR_PASSWORD=… WARILINK_APP_PASSWORD=… WARILINK_BACKUP_PASSWORD=… \
--     psql -v ON_ERROR_STOP=1 -U <bootstrap> -d postgres -f infra/ops/postgres-roles.sql
--
-- パスワードは psql の \getenv で環境変数から読む(コマンドライン・このファイル・ログに残さない)。
--
-- | ロール            | ログイン | 属性                                 | 権限                                                        |
-- |-------------------|----------|--------------------------------------|-------------------------------------------------------------|
-- | (ブートストラップ) | 導入時だけ | SUPERUSER(postgres イメージの初期ユーザー) | ロールと DB を作るだけ。アプリ・ジョブでは使わない            |
-- | warilink_migrator | する     | NOSUPERUSER NOCREATEDB NOCREATEROLE  | DB と public スキーマの所有者。alembic upgrade だけで使う    |
-- | warilink_app      | する     | 同上 + NOBYPASSRLS NOREPLICATION     | 表の SELECT/INSERT/UPDATE/DELETE と sequence だけ。DDL なし  |
-- | warilink_backup   | する     | 同上                                 | pg_read_all_data(読み取りだけ。pg_dump 用)                 |
-- 復元は新しい DB(warilink_restore_*)をブートストラップの管理者で作り、所有者を warilink_migrator にして行う
-- (infra/ops/production-runbook.md)。

\set ON_ERROR_STOP on
\getenv db_name WARILINK_DB_NAME
\getenv migrator_password WARILINK_MIGRATOR_PASSWORD
\getenv app_password WARILINK_APP_PASSWORD
\getenv backup_password WARILINK_BACKUP_PASSWORD

SELECT (:'db_name' ~ '^[a-z][a-z0-9_]{0,62}$') AS db_name_ok \gset
\if :db_name_ok
\else
  \echo 'WARILINK_DB_NAME は英小文字・数字・_ にしてください'
  \quit 3
\endif
SELECT (length(:'migrator_password') >= 24 AND length(:'app_password') >= 24 AND length(:'backup_password') >= 24)
  AS passwords_ok \gset
\if :passwords_ok
\else
  \echo 'WARILINK_MIGRATOR_PASSWORD / WARILINK_APP_PASSWORD / WARILINK_BACKUP_PASSWORD は 24 文字以上にしてください'
  \quit 3
\endif

CREATE ROLE warilink_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOREPLICATION
  PASSWORD :'migrator_password';
CREATE ROLE warilink_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOREPLICATION
  CONNECTION LIMIT 50 PASSWORD :'app_password';
CREATE ROLE warilink_backup LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOREPLICATION
  CONNECTION LIMIT 2 PASSWORD :'backup_password';

CREATE DATABASE :"db_name" OWNER warilink_migrator;
REVOKE ALL ON DATABASE :"db_name" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db_name" TO warilink_app, warilink_backup;
GRANT pg_read_all_data TO warilink_backup;

\connect :"db_name"
-- public スキーマは migrator の持ち物にし、ほかのロールには作成の権限を与えない
ALTER SCHEMA public OWNER TO warilink_migrator;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO warilink_app, warilink_backup;
-- これから migrator が作る表・sequence に、アプリの DML 権限だけを自動で付ける
ALTER DEFAULT PRIVILEGES FOR ROLE warilink_migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO warilink_app;
ALTER DEFAULT PRIVILEGES FOR ROLE warilink_migrator IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO warilink_app;
ALTER DEFAULT PRIVILEGES FOR ROLE warilink_migrator IN SCHEMA public
  REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
