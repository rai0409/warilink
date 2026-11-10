# 無料版(手入力)の公開前ゲート

対象は初回の無料公開だけ: **登録不要・JPY の手入力・招待したグループ・整数円の精算案・送金の申告と受取人の手動確認・無料**。
レシート読み取り(OCR)・Passkey・受取口座・銀行連携・広告・課金は既定で OFF(`WARILINK_FEATURE_*`)。本番設定の検査は、
銀行連携・広告・課金を有効にした状態の起動を拒否する(`apps/api/app/core/config.py`、`apps/api/tests/test_production_config.py`)。

判定は PASS / FAIL / BLOCKED / UNEXECUTED のどれか。**未実施を PASS にしない**。証拠が手元の実行だけのものは「ローカル」と書く。
ここにある PASS は、開発環境でのテストの結果です。本番で動いていることを示すものではありません。

**現在の判定: `FREE_RELEASE=NO_GO`**(2026-11-10、ブランチ `feat/warilink-step04-ops-validation-20261104`)

| ゲート | 判定 | 証拠・残っていること |
|---|---|---|
| 数値: 独立オラクルとストレスの実測 | PASS(ローカル、2026-11-03、macOS arm64・Python 3.13、コミット 1458a13・独立レビュー PASS) | `docs/TEST_AND_DELIVERY.md`「数値の独立オラクルと反例の修正」。見つかった反例 3 件を修正済み。CI のストレス(`.github/workflows/stress.yml`、種 20261030 / 11 / 4242)を手動で実行し成功(2026-11-03、`51ba5d8`、https://github.com/rai0409/warilink/actions/runs/41602276943)。週 1 回の定期実行の初回も成功(2026-11-08、event=schedule、main `19d8e37`、https://github.com/rai0409/warilink/actions/runs/42528666176) |
| main の CI | PASS(main `ca186dd`、2026-11-03) | https://github.com/rai0409/warilink/actions/runs/41602748349(api / web / e2e が成功。#17 のマージ後) |
| CSRF・Origin・回数制限 | PASS(ローカル、2026-11-03、コミット 1458a13 で API テスト 374 件が成功、使い捨て DB) | `apps/api/tests/api/test_flow.py`(CSRF・Origin)、`apps/api/tests/api/test_rate_limits.py` |
| 端末での手動確認(iPhone・Android・読み上げ) | UNEXECUTED | `docs/QA_DEVICE_CHECKLIST.md`(`DEVICE_MANUAL_QA=UNEXECUTED`) |
| 本番の構成ファイル・送信元 IP の受け渡し・DB のロール分離・マイグレーションの分離・起動前検査(コード) | PASS(ローカル、2026-11-08、STEP 03。一時コンテナで Caddy・Next.js の実物と使い捨ての PostgreSQL を使った検証) | `docs/TEST_AND_DELIVERY.md`「STEP 03」、`infra/ops/production-runbook.md`。実際の VM・DNS・証明書での確認ではない(次の行) |
| 本番の HTTPS・送信元 IP・専用 DB・DB の権限 | BLOCKED | 構成・ロール・起動前検査はある(上の行)。VM・DNS・証明書が未契約で、実稼働での確認(`scripts/warilink_preflight.py --check-db` が本番で PASS すること)はしていない |
| 定期ジョブ・監視・復元の検証・負荷の確認(コードとユニットのひな形) | PASS(ローカル、2026-11-10、STEP 04。一時コンテナで復元の検証と負荷を実測、systemd-analyze verify) | `docs/TEST_AND_DELIVERY.md`「STEP 04」。timer は登録していない(次の行) |
| 保存期限の自動削除ジョブ | BLOCKED | `RETENTION_RUNTIME=BLOCKED`。ユニット(`infra/systemd/warilink-purge.*`)はあるが、導入先で timer を登録していない。画面はジョブの実績が無い間は自動削除を約束しない |
| 暗号化バックアップと削除の再適用 | PASS(ローカル、2026-10-31、PR C #14) | `ENCRYPTED_BACKUP_TEST=PASS`、`RESTORE_DRILL=PASS`(隔離した一時コンテナ)。記録は `docs/TEST_AND_DELIVERY.md`「PR C」 |
| 削除の記録(DB の外の削除ジャーナル・削除の前後の記録・復元時の検査) | PASS(ローカル、2026-11-04、STEP 01、使い捨て DB で API テスト 416 件・隔離した一時コンテナで復元訓練) | `docs/TEST_AND_DELIVERY.md`「STEP 01」。ローカルディレクトリの実装だけで、外部保管ではない(次の行)。このブランチの CI は未実行(push していない) |
| S3・KMS の外部保管のコード(偽の S3・KMS での検証) | PASS(ローカル、2026-11-06、STEP 02。AWS へは接続していない) | `docs/TEST_AND_DELIVERY.md`「STEP 02」。偽物での成功は実際の外部保管の証拠ではない(次の行)。このブランチの CI は未実行 |
| バックアップ・削除履歴の外部保管、本番相当の復元 | BLOCKED | `OFFSITE_STORAGE_LIVE` / `KMS_LIVE` / `RPO_RTO_PROVEN` が BLOCKED(実 AWS の承認・予算・検証用の資格情報が無い。バケット・鍵・IAM は未作成) |
| 通知・監視 | BLOCKED | `MONITOR_ALERT_LIVE=BLOCKED`(通知先が未選定。監視は本番で未構成を異常として扱い、ユニットはあるが未登録。実際の通知の到達は未確認) |
| 利用規約・プライバシーポリシー・問い合わせ先 | BLOCKED | 画面は「準備中」。下書きは `docs/legal/`、決める項目は `docs/legal/README.md` の `OWNER_APPROVAL_REQUIRED` |
| LICENSE(商用で使う権利) | BLOCKED | リポジトリは独自の非商用ライセンス。権利者の判断が必要 |
| ロールバック計画(リリースの戻し方・DB の移行の戻し方) | BLOCKED | 手順の文書はある(`infra/ops/production-runbook.md`「戻し方」、STEP 03)。本番の DB の切り替えは VM・保管先が決まってから実地で作り、訓練していない |
| 画面の提供範囲の表示(止めている機能を使えるように見せない、送金しないこと、入金を保証しないこと、ゲストの記録を失う場合があること) | PASS(ローカル、2026-11-03、E2E の実行コミットは `docs/TEST_AND_DELIVERY.md` の P05 節に記録) | `apps/web/e2e/policy.spec.ts`、ホーム・料金プラン画面 |

## OCR を有効にする場合だけのゲート(無料版では対象外)

| ゲート | 判定 | 内容 |
|---|---|---|
| `OCR_REAL_HOLDOUT` | BLOCKED | 本人の同意と利用権限のある実レシート・確認済みの正解ラベル・独立した holdout が未準備 |
| `OCR_RELEASE` | BLOCKED | 精度の基準値は責任者が決める。測定していないので PASS にしない |

`DEFERRED_OCR`(P03・P04)は、上の条件がそろうまで実施しない。

## 判定を変えるとき

- 判定を PASS にするときは、日付・実行した環境・証拠(ログ・run の URL・コミット)を同じ行に書く。
- 1 つでも BLOCKED・UNEXECUTED・FAIL が残っている間は `FREE_RELEASE=NO_GO`。
