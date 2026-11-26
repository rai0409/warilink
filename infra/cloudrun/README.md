# Cloud Run 用の隔離された入口

この設定は Cloud Run の `PORT` で HTTP を受け、同一インスタンスの Web sidecar
`127.0.0.1:3000` のみに中継します。TLS は Cloud Run で終端します。Web は別工程で
API を内部中継する必要があり、この設定は API を直接公開しません。VM 用の
`infra/proxy/Caddyfile` と Compose は変更しません。

受信した `X-Forwarded-For`、`X-Real-IP`、`Forwarded` は Web に転送しません。
Cloud Run の中継元を利用者の IP と見なしたり、別の IP を作ったりしません。
Next.js が後段に付けるヘッダーと実際のプロキシ経路を含め、Cloud Run の正しい
接続元 IP は未検証です。そのため **IP 別 rate limit の商用β受入は BLOCKED** です。
本番の rate limit は無効化しません。偽装不能な利用者 IP と後段の trusted proxies を
実環境で検証するまで、この構成を商用βに使用しないでください。

既定の `WARILINK_RATE_LIMIT_SOURCE=ip` は従来の IP 単位の回数制限を使用します。
`WARILINK_RATE_LIMIT_SOURCE=iap` では、API が IAP の署名と audience を検証し、
検証済みの subject を使って利用者単位の回数制限を適用します。WariLink のログインと
グループ認可は引き続き必要です。IAP モードの Cloud Run 実環境での動作は未検証です。

Caddy の HTTP アクセスログは設定していません。Caddy のエラーログ、Next.js と
FastAPI のログは別です。Cloud Run の自動リクエストログと IAP のログも別であり、
この設定はそれらの内容を制御しません。`/join/[token]` と
`/api/v1/invites/[token]` の URL が Cloud Logging に記録されないことは未検証です。
実環境のログを調査し、必要なら招待 token の URL 配置変更を別工程で行うまで
**Cloud Logging の秘密保護は BLOCKED** です。IAP と Cloud Run 自体も未検証です。

ローカルの Caddy 2.10 系を `PATH` に置くか `CADDY_BINARY` に指定し、API の依存を
用意して `cd apps/api && pytest tests/ops/test_cloudrun_ingress.py` を実行します。
テストは利用可能なポート 3000 に一時的なテスト専用 Web を起動します。既存サービスが
使用中の場合は停止せず、HTTP 検証を実行できません。
