# GitHub / OSS再利用と権利確認

確認済み候補に基づく技術計画。採用時には具体的なコミットSHA・lockfile・ライセンス・依存脆弱性を再確認。

## 1. 外部OSS

| Repo | License | 対象コード/方針 | 注意 |
|---|---|---|---|
| https://github.com/spliit-app/spliit | MIT | `src/lib/shares.ts`, `src/lib/balances.ts`, `e2e`の金額計算・残高・UI構成を参考 | AppRouter/Prisma/tRPC全体はフォークしない。改変コードのMIT notice保持 |
| https://github.com/oss-apps/split-pro | MIT | `src/lib/simplify.ts`, `src/lib/shareSplit.ts`のBigIntや相殺ロジックを比較 | ソート時のbigint→number等は境界条件のテストが必要。厳密な最少送金件数保証ではない |
| https://github.com/PaddlePaddle/PaddleOCR | Apache 2.0 | 日本語OCR/位置/信頼度。3.x APIに適合 | OCRモデルの個別weight・依存ライセンス確認 |
| https://github.com/opencv/opencv | Apache 2.0 | 画像補正 | CPU性能ベンチマーク |
| https://github.com/fastapi/fastapi | MIT | APIフレームワーク | 互換性をlockfile検証 |
| https://github.com/shadcn-ui/ui | MIT | UIパーツ | 同梱第三者依存/アセットを監査 |
| https://github.com/TanStack/query | MIT | クライアントサーバー状態 | SSR cacheの機微データ漏洩対策 |
| https://github.com/dinerojs/dinero.js | MIT | 金額表現の参考 | backendに計算を一元化するので導入は必須でない |

### 特定のSpliitの実装確認事項

- `src/lib/shares.ts`: Hamilton法、費用IDによる決定論的タイブレーク、整数minor unitsの分配。
- `src/lib/balances.ts`: ネット残高・返済候補。貪欲法は送金件数の厳密最小保証ではない。
- `src/app/groups/[groupId]/expenses/create-from-receipt-button-actions.ts`: 画像→金額/日付/名称/カテゴリーの抽出で、レシート**明細商品ごとの自動割当**は追加実装が必要。
- Spliitの既存OCRは外部AI APIを使用する経路がある。今回はPaddleOCR localを採用し別実装する。

## 2. ユーザー保有Github（個別の著作権確認が必要）

| Repo | reuse candidate | 限界 |
|---|---|---|
| `rai0409/contract-ingest` | `contract_ingest/extract/ocr_paddle.py`: PaddleOCR+BBox+confidence adapter | 旧PaddleOCR API。日本語レシート明細の完成品ではない |
| `rai0409/denchobox` | `denchobox/extraction.py`, `denchobox/extract.py`, `denchobox/tax_breakdown.py`: draft/verify/retry/review/tax | レシート商品行OCR未実装。現状外部AI中心。精度実測は合成データ中心 |
| `rai0409/personal-finance-ingest` | `payroll/reconcile.py`, audit/hash & bookkeeping patterns | リポジトリはprivate、用途が異なる。直接取り込み要審査 |
| `rai0409/document-preprocessing-core` | 正規化と数字の差異保護 | レシート専用ではない |
| `rai0409/booking_zoom_connect` | auth/rate-limit/idempotency/response logging patterns | 全体を移植しない |
| `rai0409/financial-grade-billing-engine` | 金額・請求関係のlegacy patterns | 初期採用せず別途コード監査 |

複数のユーザーrepoは `RAI NON-COMMERCIAL SOURCE-AVAILABLE LICENSE` で公開されており、MITではない。著作権を単独で持つ本人の使用は別途判断可能だが、寄稿者・第三者要素・雇用契約等の権利を確認する。ライセンス文面が非商用でも**権利者本人による商用利用を一律禁止するものではない**。未確認の第三者権利を侵害してコピーしない。

## 3. 取り込みルール

1. `third_party_notices/NOTICE.md`を生成し、ソースURL・commit SHA・license・変更内容・依存ライブラリの権利を一覧化。
2. MITコードをコピー・改変するならCopyright/permission noticeを保持。Apache-2.0はLICENSE/NOTICE等の条件に従う。
3. 比較参照のみなら再実装した関数に「どのロジックと対照したか」をコメントし、コピー元を明示（必要に応じ）。
4. OSS製品のブランドやロゴを自社製品のように流用しない。仮称`WariLink`は商標確認のうえ確定。
5. ユーザーrepoをクローンする場合も読み取り・特定ファイル参照から始め、秘密情報・本番DB・私的サンプルを取り込まない。


## OCR 評価用の公開データ(2026-10-21、P12)

| データ | 内容 | 状況 |
|---|---|---|
| CORD(https://github.com/clovaai/cord) | インドネシアのレシート、項目の注釈 | **未取り込み**。利用条件(ライセンス表記)の確認が必要。日本語の精度には算入しない |
| SROIE(https://github.com/jsdnrs/ICDAR2019-SROIE 等の再配布) | 英語のレシート、主要項目 | **未取り込み**。原典の配布条件が明確でないため、使う前に権利を確認する |

評価器(`app/ocr/evaluate.py`)は `source: public` として別に集計できるが、データ本体・重み・注釈はリポジトリに入れない。
