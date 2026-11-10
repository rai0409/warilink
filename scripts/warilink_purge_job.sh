#!/usr/bin/env bash
# 保存期間を過ぎたデータ(レシート画像・一時データ・監査ログ・グループ)の削除、DB・画像の整合性検査、
# 削除の記録の確定(削除ジャーナル)または削除済み一覧の書き出しを 1 回行う(定期実行用)。
# 失敗・不一致があれば WARILINK_ALERT_CMD(任意のコマンド)に短い要約を標準入力で渡す。
# 要約には件数とレシート ID だけを含め、画像の中身・パス・接続文字列は含めない。
#
# 例: WARILINK_ALERT_CMD='logger -t warilink-alert' scripts/warilink_purge_job.sh
# このスクリプトはどこにも自動登録しない。スケジュールは infra/systemd/(STEP 04)を見て導入先で設定する。
#
# 実行の場所(STEP 04):
# - 既定: このリポジトリの .venv の Python で apps/api から実行する(開発・同じホストに DB がある場合)。
# - WARILINK_COMPOSE_FILE を設定すると、本番の compose(infra/compose.production.yaml)の api のイメージで
#   `docker compose run --rm` して実行する(DB は内部ネットワークにだけあるため)。env ファイルは WARILINK_ENV_FILE。
# 削除の記録(STEP 01・02):
# - WARILINK_DELETION_JOURNAL があれば(compose では env ファイルにあれば)journal-reconcile で未確定の削除を確定し、
#   確定できない削除が残っていれば失敗にする。WARILINK_TOMBSTONE_FILE は任意(旧方式の一覧も書き出す)。
# - 無ければ従来どおり、WARILINK_TOMBSTONE_FILE への書き出しが必須。
# 二重起動の防止: ${WARILINK_LOCK_DIR:-/tmp}/warilink-purge-job.lock(実行中なら終了コード 1。古い印は自動で消さない)。
# 終了コード: 0 = 成功 / 1 = 失敗・不一致・実行中(従来どおり)。
set -u
cd "$(dirname "$0")/../apps/api" || exit 1

LOCK="${WARILINK_LOCK_DIR:-/tmp}/warilink-purge-job.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "purge job is already running (lock: warilink-purge-job.lock)" >&2
  exit 1
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

if [ -n "${WARILINK_COMPOSE_FILE:-}" ]; then
  if [ -z "${WARILINK_ENV_FILE:-}" ] || [ ! -f "${WARILINK_ENV_FILE}" ]; then
    echo "WARILINK_ENV_FILE (本番の env ファイル) がありません" >&2
    exit 1
  fi
  PY=(docker compose -f "$WARILINK_COMPOSE_FILE" --env-file "$WARILINK_ENV_FILE" run --rm -T --no-deps api python)
  journal_set=0
  grep -q '^WARILINK_DELETION_JOURNAL=.' "$WARILINK_ENV_FILE" && journal_set=1
else
  PY=("${WARILINK_PYTHON:-../../.venv/bin/python}")
  journal_set=0
  [ -n "${WARILINK_DELETION_JOURNAL:-}" ] && journal_set=1
fi

purge_out=$("${PY[@]}" -m app.ocr.cli purge-images --json 2>&1); purge_rc=$?
check_out=$("${PY[@]}" -m app.ocr.cli check-storage 2>&1); check_rc=$?
echo "purge rc=$purge_rc $(echo "$purge_out" | tail -1)"
echo "check rc=$check_rc $(echo "$check_out" | tail -1)"

# 削除の記録。ジャーナルがあれば、未確定の削除を確定する(確定できないものが残れば失敗)
journal_rc=0
if [ "$journal_set" -eq 1 ]; then
  journal_out=$("${PY[@]}" -m app.modules.identity.retention journal-reconcile 2>&1); journal_rc=$?
  echo "journal rc=$journal_rc $(echo "$journal_out" | tail -1 | cut -c1-300)"
fi

# 旧方式: 削除済み一覧(退会・振込先の消去・グループの削除の ID)を DB の外へ追記する
tomb_rc=0
if [ -n "${WARILINK_TOMBSTONE_FILE:-}" ]; then
  tomb_out=$("${PY[@]}" -m app.modules.identity.retention export-tombstones --out "$WARILINK_TOMBSTONE_FILE" 2>&1); tomb_rc=$?
  echo "tombstones rc=$tomb_rc $(echo "$tomb_out" | tail -1)"
elif [ "$journal_set" -eq 1 ]; then
  echo "tombstones skipped: the deletion journal is the source of truth"
else
  echo "tombstones skipped: WARILINK_TOMBSTONE_FILE is not set" >&2
  tomb_rc=1
fi

if [ "$purge_rc" -ne 0 ] || [ "$check_rc" -ne 0 ] || [ "$tomb_rc" -ne 0 ] || [ "$journal_rc" -ne 0 ]; then
  summary="warilink purge-job failed: purge_rc=$purge_rc check_rc=$check_rc tombstones_rc=$tomb_rc journal_rc=$journal_rc"
  # 例外の詳細は最後の 1 行だけ(接続文字列などを含みうるトレースバック全体は送らない)
  [ "$purge_rc" -ne 0 ] && summary="$summary purge=$(echo "$purge_out" | tail -1 | cut -c1-300)"
  [ "$check_rc" -ne 0 ] && summary="$summary check=$(echo "$check_out" | tail -1 | cut -c1-300)"
  if [ -n "${WARILINK_ALERT_CMD:-}" ]; then
    echo "$summary" | sh -c "$WARILINK_ALERT_CMD" || echo "alert command failed" >&2
  else
    echo "$summary" >&2
  fi
  exit 1
fi
exit 0
