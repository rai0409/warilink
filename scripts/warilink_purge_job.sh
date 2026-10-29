#!/usr/bin/env bash
# 保存期間を過ぎたデータ(レシート画像・一時データ・監査ログ・グループ)の削除、DB・画像の整合性検査、
# 削除済み一覧の書き出しを 1 回行う(定期実行用)。
# 失敗・不一致があれば WARILINK_ALERT_CMD(任意のコマンド)に短い要約を標準入力で渡す。
# 要約には件数とレシート ID だけを含め、画像の中身・パス・接続文字列は含めない。
#
# 例: WARILINK_ALERT_CMD='logger -t warilink-alert' scripts/warilink_purge_job.sh
# このスクリプトはどこにも自動登録しない。スケジュールは infra/ops/README.md を見て導入先で設定する。
set -u
cd "$(dirname "$0")/../apps/api" || exit 1
PY="${WARILINK_PYTHON:-../../.venv/bin/python}"

purge_out=$("$PY" -m app.ocr.cli purge-images --json 2>&1); purge_rc=$?
check_out=$("$PY" -m app.ocr.cli check-storage 2>&1); check_rc=$?
echo "purge rc=$purge_rc $(echo "$purge_out" | tail -1)"
echo "check rc=$check_rc $(echo "$check_out" | tail -1)"

# 削除済み一覧(退会・振込先の消去・グループの削除の ID)を DB の外へ追記する。バックアップから戻した後に
# apply-tombstones で削除を再適用するため。置き場所はバックアップと同じ保管先にする(infra/ops/README.md)。
tomb_rc=0
if [ -n "${WARILINK_TOMBSTONE_FILE:-}" ]; then
  tomb_out=$("$PY" -m app.modules.identity.retention export-tombstones --out "$WARILINK_TOMBSTONE_FILE" 2>&1); tomb_rc=$?
  echo "tombstones rc=$tomb_rc $(echo "$tomb_out" | tail -1)"
else
  echo "tombstones skipped: WARILINK_TOMBSTONE_FILE is not set" >&2
  tomb_rc=1
fi

if [ "$purge_rc" -ne 0 ] || [ "$check_rc" -ne 0 ] || [ "$tomb_rc" -ne 0 ]; then
  summary="warilink purge-job failed: purge_rc=$purge_rc check_rc=$check_rc tombstones_rc=$tomb_rc"
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
