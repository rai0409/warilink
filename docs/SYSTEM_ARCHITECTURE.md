# 技術構成と実装方針

## 0. 判断

MVPは**独自Next.js UI + FastAPIが業務正本 + PostgreSQL 18 + 分離OCR worker**。Spliit/ SplitProをアプリごとフォークし、Prisma/tRPCとFastAPIを二重運用しない。独立した支出配分・精算アルゴリズムを仕様移植する。

## 1. コンポーネント

```text
Mobile browser / Desktop browser
    |
    | HTTPS / secure cookie / optional passkey
    v
Next.js 16 App Router (TypeScript, Tailwind, shadcn/ui)
    | REST JSON / OpenAPI-generated TS client
    v
FastAPI 0.143-class (Python 3.12+, Pydantic 2)
    |-- Guest identity / Passkey authentication
    |-- Group membership, expenses, allocations
    |-- Settlement ledger, references, status/audit
    |-- Payment destination scope
    |-- OCR quota / rewarded credits / consent
    |-- Bank provider abstraction (mock ONLY until approved)
    |
    +--> PostgreSQL 18 (transactional system of record)
    +--> Private Object Storage (receipts/images; signed upload)
    +--> Durable OCR Jobs -> Worker (PaddleOCR 3.x + OpenCV + parser)
    +--> Notification adapter (in-app; optional push)
```

### コンポーネントの配置

- Next.js: UI、PWA、LP、SEO。サーバーが金融データを保持する責務はない。銀行データをNext.js render cacheへ載せない。
- FastAPI: 唯一のAPIとドメインロジック。銀行情報・課金枠・精算状態の正式な変更を統制。
- PostgreSQL 18: SQLAlchemy 2 / Alembic; unique/foreign key/transaction/RLSが必要なら個別検証。API側の厳格な権限検証が基本。
- OCR Worker: CPUベンチマークでPP-OCRv6 small/medium、日本語実レシートの完全一致率を比較。サーバーのCPU/RAMやモデルの正式な互換性は実測する。
- ジョブ: `FOR UPDATE SKIP LOCKED`ベースのDBジョブキューを小規模で採用。lease・heartbeat・retry・dead-letterを実装。スケール時のみRedis/Celery等へ移行。
- ストレージ: レシート原本とOCR rawは暗号化private bucket。寿命の短い署名付きURL。削除ポリシーをジョブで運用。
- 認証:ゲストはhttpOnly Secure SameSite Cookieによるセッション、参加は権限付き招待。登録者はWebAuthn/Passkeys; 本番認証のWebAuthn安全要件を完全実装できない場合、信頼できる認証サービス導入を検討。自作簡易Passkeyは不可。

## 2. ディレクトリ推奨

```text
apps/
  web/
    src/app/(marketing)/
    src/app/(app)/g/[publicId]/
    src/app/(app)/payments/[transferId]/
    src/components/{layout,groups,receipts,allocations,settlements,account}/
    src/lib/api/
  api/
    app/main.py
    app/core/{config,security,db}.py
    app/modules/{identity,groups,expenses,receipts,allocation,settlement,payment_destinations,bank_matching,quota,consent,ads}/
    app/models/
    alembic/
  ocr-worker/
    worker/{main,preprocess,ocr,receipt_parser,reconcile}.py
packages/
  contracts/               # OpenAPI生成client / TS types
infra/
  compose.yaml
  .env.example
scripts/
tests/
  api/
  ocr/
  e2e/
prototype/                    # 見た目のみ。APIへの直接流用禁止
```

## 3. 技術詳細

| 領域 | 技術 | 要点 |
|---|---|---|
| Web | Next.js 16 stable + React + TS | Mobile-first, App Router, noindex for private data |
| UI | Tailwind CSS + shadcn/ui + Lucide | コンポーネントの権利とアクセシビリティ |
| Data fetching | TanStack Query | API状態更新、更新競合対策 |
| Backend | FastAPI + Pydantic v2 | OpenAPI、厳格バリデーション |
| DB | PostgreSQL 18 + SQLAlchemy 2 + Alembic | 取引履歴の正本、ロック、監査 |
| OCR | PaddleOCR v3.7系 + OpenCV | 非同期ジョブで画像処理 |
| 検査 | pytest + Hypothesis + Playwright | 完全金額・権限・状態機械の網羅 |
| Docs | OpenAPI + Markdown | API契約一元管理 |
| Secrets | マネージドシークレット / 暗号鍵管理 | ソースにキーを書かない |

依存バージョンは**実際のlockfileとCI**で固定する。将来、最新バージョンが更新されても自動で互換性が保たれるわけではない。

## 4. 配分アルゴリズム仕様

- 保存額 = 通貨の最小単位で整数。JPYでは1円が1unit。
- `BY_EQUAL`, `BY_WEIGHT`, `BY_PERCENTAGE`, `BY_EXACT`, `BY_ITEM`。
- 端数はHamilton / largest remainder。重みは整数比、浮動小数点でmoneyを保持しない。
- 同順位の端数は「expense ID + participant ID」の安定的な決定論的タイブレーク（別のリクエストでブレない）で分配。
- 負数（払戻/訂正）をサポートする場合は負数専用のテストを先に作成し、符号とsumを壊さない。
- 調整額（税・割引・サービス料）は独立したadjustment record。一般的な割引は負値、税は正値。税込明細を二重計上しない。行によって8%/10%混合の可能性を保持する。
- 精算アルゴリズム(2026-10-18 更新): 非ゼロ残高が上限(既定20人)以下なら部分集合DPで**厳密な最小送金件数**を求め「最小送金回数」と表示。上限・時間制限超過時は貪欲法へフォールバックし「送金回数を削減」と表示(最小保証なし)。前提条件・計算方法・厳密解か否かをAPIで返す。詳細は `docs/SETTLEMENT_ENGINE.md`。
- 確定済み(送金申告・受取確認・照合済み)の送金は再最適化で変更せず、残差残高のみ再計算する。
- Group closure後の支出訂正は新規修正イベントか新settlement batch。過去のbank_matchを再度未照合扱いにしてはならない。

## 5. OCRパイプライン

1. file safety: MIME/magic bytes, size, decompression-bomb, EXIF位置情報除去の検証、リサイズ、保存期間。
2. deskew + blur detection + orientation; OpenCV。
3. OCR output `tokens[{text,bbox,score}]`、モデルversion、画像hash。
4. layout parser: item/quantity/unit_price/line_total, subtotal/discount/tax/tip/total/date/merchant; raw text完全保存ではなく必要範囲。
5. confidence and monetary reconciliation; ambiguous lines → needs_review; OCR textと確定値の差分を保持。
6. user review and confirm; expense commit transactional。
7. 将来の改善: 同意済み+利用目的に合う修正のみデータセット化、店舗・レイアウト分割の評価、モデル昇格gate。

OCRは文字認識だけでは商品明細の整合を保証しない。再解析APIを使う場合も費用/画像のプライバシーを別評価。MVPは外部OCR API必須にしない。

## 6. BankDataProvider (契約前はmock専用)

```python
from typing import Protocol, Sequence
from dataclasses import dataclass
from datetime import datetime

@dataclass(frozen=True)
class BankTransaction:
    provider_transaction_id: str
    account_connection_id: str
    amount_minor: int            # 受取口座への入金を正として正規化
    currency: str
    booked_at: datetime
    description_raw: str | None
    counterparty_name: str | None
    provider_updated_at: datetime | None

class BankDataProvider(Protocol):
    async def get_credit_transactions(
        self, *, connection_id: str, since: datetime
    ) -> Sequence[BankTransaction]: ...
```

- 認可・口座接続は正式プロバイダ契約後のみ。読み取りスコープ限定、ユーザー撤回、同期最終時刻。
- `provider_transaction_id`が不変・一意とは限らない。プロバイダ更新・摘要変更・重複イベントに備え同一性管理を設計。
- 銀行によって振込依頼人名・振込日・送金メッセージが摘要へ到着しない。銀行別 capability matrix をデータ管理。
- PayPay送金やことら送金は基本、手動の精算フロー。bank matchingを誤適用しない。

## 7. 開発・環境

- Docker ComposeでDB/API/worker/webを起動できる。
- `.env.example`にはダミー名だけ。秘密鍵は実ファイルにもGitにも記録しない。
- Postgres migrationをCI、テストはDBごと分離。安全なfixturesのみ。
- lint/format/type-check/test e2eをCIに設置。
- 本番銀行・決済はFEATURE_FLAGで初期OFF、debug UIでも実口座は絶対に接続しない。

## 8. 運用と復旧(STEP5、2026-10-29)

| 項目 | 目標・現状 |
|---|---|
| RPO(失ってよいデータの最大量) | 24 時間(バックアップは 1 日 1 回)。本番の保管先が決まるまで**運用は未開始**(手順のみ) |
| RTO(復旧までの目標時間) | 4 時間(新しい DB への restore → 検証 → 削除済み一覧の再適用 → 切り替え)。訓練(合成データ 15 行・画像 1 枚)は 4 秒で完了。本番規模の計測は未実施 |
| バックアップの保持 | 30 日(`scripts/warilink_backup.py prune`)。暗号化・外部保管は保管先が決まるまで**未実施** |
| 復元後の削除の再適用 | `python -m app.modules.identity.retention apply-tombstones`(必須。`infra/ops/README.md`) |
| 監視 | 定期ジョブの失敗(`WARILINK_ALERT_CMD`)、`GET /api/v1/health`、OCR ワーカーの heartbeat(`/plans.ocr_available`)。通知先・外形監視は**未設定** |

- API サーバーは `uvicorn --no-proxy-headers --no-access-log` で起動する。uvicorn は既定で 127.0.0.1 からの `X-Forwarded-For` を
  信じて接続元を書き換えるため、回数制限の判定をアプリ側の `WARILINK_TRUSTED_PROXIES` だけに任せる。アクセスログには招待トークンを含む
  URL が残るため出さない(`infra/compose.yaml`、`apps/web/playwright.config.ts`)。`apps/api/Dockerfile` の既定の起動コマンドは
  まだ旧設定で、本番イメージでは同じ引数で起動する(Dockerfile の修正は STEP5 の変更範囲外のため別途)。
