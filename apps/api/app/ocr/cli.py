"""OCR の手元検証・評価用コマンド。

  python -m app.ocr.cli recognize 画像 [--json]          1枚を読み取って下書きを表示(DB 不要)
  python -m app.ocr.cli synth --out DIR --count 50       合成レシートと正解ラベルを生成
  python -m app.ocr.cli evaluate DIR [--baseline F]      評価(verified/synthetic ラベルのみ。終了コード 0/2/3)
  python -m app.ocr.cli purge-images [--dry-run] [--json]  保存期間を過ぎた画像を削除(失敗があれば終了コード 1)
  python -m app.ocr.cli check-storage                    DB と画像ファイルの不一致を検査(不一致は終了コード 2)
  python -m app.ocr.cli stats                            読み取りの訂正件数・確認時間の集計(DB)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _adapter():
    from app.core.config import get_settings  # noqa: PLC0415
    from app.ocr.paddle_adapter import PaddleOcrAdapter  # noqa: PLC0415

    s = get_settings()
    return PaddleOcrAdapter(det_model=s.ocr_det_model, rec_model=s.ocr_rec_model, cpu_threads=s.ocr_cpu_threads)


def cmd_recognize(a: argparse.Namespace) -> int:
    from app.core.config import get_settings  # noqa: PLC0415
    from app.ocr.worker import ImageRejected, run_pipeline  # noqa: PLC0415

    adapter = _adapter()
    try:
        out = run_pipeline(adapter, Path(a.image).read_bytes(), low_confidence=get_settings().ocr_low_confidence)
    except ImageRejected as e:
        print(json.dumps({"error": e.code}, ensure_ascii=False))
        return 1
    out["model"] = adapter.model_info().__dict__
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    d = out["draft"]
    print(f"店名: {d['store_name']}  日付: {d['purchased_on']}  税: {d['tax_mode']}")
    for i in d["items"]:
        flag = " [要確認: " + ",".join(i["reasons"]) + "]" if i["needs_review"] else ""
        print(f"  {i['name']:<20} x{i['quantity']:<3} {i['line_minor']:>8,}円  ({i['confidence']:.2f}){flag}")
    for adj in d["adjustments"]:
        print(f"  [{adj['kind']}] {adj['label']:<16} {adj['amount_minor']:>8,}円")
    print(f"合計: {d['total_minor']}  差額: {d['difference_minor']}")
    for w in d["warnings"]:
        print(f"  ! {w['message']}")
    print(f"認識 {out['recognize_ms']:.0f} ms / 前処理 {out['preprocess_ms']:.0f} ms")
    return 0


def cmd_synth(a: argparse.Namespace) -> int:
    from app.ocr.synth import generate  # noqa: PLC0415

    paths = generate(Path(a.out), a.count, a.seed, a.font, a.difficulty)
    print(f"{len(paths)} 枚を {a.out} に生成しました(label_status=synthetic)")
    return 0


def cmd_evaluate(a: argparse.Namespace) -> int:
    """終了コード: 0 = 評価できた(基準があれば accepted)/ 2 = rejected(悪化)/
    3 = incomparable・blocked・insufficient(比較できない・件数不足・未測定。採用しない)。
    """
    from app.core.config import get_settings  # noqa: PLC0415
    from app.ocr.evaluate import EvalBlocked, compare, evaluate  # noqa: PLC0415

    adapter = None if a.reuse_tokens else _adapter()
    preprocess_version = None

    def recognize(path: Path):
        from app.ocr.preprocess import decode, prepare  # noqa: PLC0415

        img = decode(path.read_bytes())
        if img is None:
            return []
        return adapter.recognize(prepare(img).image) if adapter else []

    if adapter is not None:
        from app.ocr.worker import PREPROCESS_VERSION  # noqa: PLC0415

        preprocess_version = PREPROCESS_VERSION
    try:
        result = evaluate(Path(a.dataset), adapter, low_confidence=get_settings().ocr_low_confidence,
                          reuse_tokens=a.reuse_tokens, recognize_and_prepare=None if a.reuse_tokens else recognize,
                          min_samples=a.min_samples, preprocess_version=preprocess_version)
    except EvalBlocked as e:
        print(json.dumps({"status": "blocked", "reason": str(e)}, ensure_ascii=False))
        return 3
    if a.baseline:
        result["comparison"] = compare(result, json.loads(Path(a.baseline).read_text()), a.tolerance)
    text = json.dumps(result, ensure_ascii=False, indent=1)
    if a.out:
        Path(a.out).write_text(text)
    keys = ("status", "metrics", "by_source", "skipped", "dataset_digest", "provenance", "note")
    print(json.dumps({k: result[k] for k in keys} | ({"comparison": result["comparison"]} if a.baseline else {}),
                     ensure_ascii=False, indent=1))
    if a.baseline:
        decision = result["comparison"]["decision"]
        return {"accepted": 0, "rejected": 2}.get(decision, 3)
    return 3 if result["status"] == "blocked" else 0


def cmd_purge(a: argparse.Namespace) -> int:
    """終了コード: 0 = 成功、1 = 削除できなかった画像がある(監視で通知する。次回の実行で再試行)。"""
    from sqlalchemy.orm import Session  # noqa: PLC0415

    from app.core.db import get_engine  # noqa: PLC0415
    from app.modules.identity.retention import purge_retention  # noqa: PLC0415
    from app.ocr.images import purge_expired  # noqa: PLC0415

    with Session(get_engine()) as db:
        res = purge_expired(db, dry_run=a.dry_run)
        # 保持期限を過ぎた一時データ・監査ログ・グループも同じジョブで消す(docs/DATA_MODEL.md §11)
        retention = {} if a.dry_run else purge_retention(db)
    challenges = retention.get("webauthn_challenges", 0)
    if a.json:
        print(json.dumps({**res.to_json(), "webauthn_challenges_deleted": challenges, "retention": retention},
                         ensure_ascii=False))
    else:
        verb = "削除対象" if a.dry_run else "削除"
        print(f"{verb}: {res.candidates if a.dry_run else res.deleted} 枚(保存期間 {res.retention_days} 日)"
              + (f" / 失敗 {len(res.failed)} 枚" if res.failed else "")
              + ("" if a.dry_run else f" / 期限切れの認証用データ {challenges} 件"))
    return 1 if res.failed else 0


def cmd_check_storage(a: argparse.Namespace) -> int:
    """終了コード: 0 = DB とファイルが一致、2 = 不一致あり(読み取り専用。何も変更しない)。"""
    from sqlalchemy.orm import Session  # noqa: PLC0415

    from app.core.db import get_engine  # noqa: PLC0415
    from app.ocr.images import check_storage  # noqa: PLC0415

    with Session(get_engine()) as db:
        rep = check_storage(db)
    print(json.dumps(rep.to_json(), ensure_ascii=False))
    return 0 if rep.consistent else 2


def cmd_stats(a: argparse.Namespace) -> int:
    from sqlalchemy import func, select  # noqa: PLC0415
    from sqlalchemy.orm import Session  # noqa: PLC0415

    from app.core.db import get_engine  # noqa: PLC0415
    from app.models.tables import OcrCorrection, OcrRun, Receipt  # noqa: PLC0415

    with Session(get_engine()) as db:
        by_class = dict(db.execute(select(OcrCorrection.error_class, func.count()).group_by(
            OcrCorrection.error_class)).all())
        confirmed = db.execute(select(func.count(), func.avg(Receipt.review_elapsed_ms))
                               .where(Receipt.state == "confirmed")).one()
        runs = db.execute(select(OcrRun.model_name, func.count(), func.avg(OcrRun.elapsed_ms),
                                 func.count().filter(OcrRun.error_code.is_not(None)))
                          .group_by(OcrRun.model_name)).all()
    print(json.dumps({
        "confirmed_receipts": confirmed[0], "mean_review_ms": None if confirmed[1] is None else float(confirmed[1]),
        "corrections_by_class": by_class,
        "runs": [{"model": m, "count": c, "mean_ms": float(t or 0), "errors": e} for m, c, t, e in runs],
        "note": "訂正は未検証(proposed)。正解ラベルとしては扱わない",
    }, ensure_ascii=False, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.ocr.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("recognize")
    r.add_argument("image")
    r.add_argument("--json", action="store_true")
    s = sub.add_parser("synth")
    s.add_argument("--out", required=True)
    s.add_argument("--count", type=int, default=30)
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--font")
    s.add_argument("--difficulty", choices=["easy", "hard"], default="easy")
    e = sub.add_parser("evaluate")
    e.add_argument("dataset")
    e.add_argument("--baseline")
    e.add_argument("--tolerance", type=float, default=0.0)
    e.add_argument("--out")
    e.add_argument("--reuse-tokens", action="store_true", help="保存済みの認識結果でパーサーだけを評価")
    e.add_argument("--min-samples", type=int, default=30, help="これ未満の件数では採否を判定しない(insufficient)")
    g = sub.add_parser("purge-images")
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--json", action="store_true")
    sub.add_parser("check-storage")
    sub.add_parser("stats")
    a = p.parse_args(argv)
    return {"recognize": cmd_recognize, "synth": cmd_synth, "evaluate": cmd_evaluate, "purge-images": cmd_purge,
            "check-storage": cmd_check_storage, "stats": cmd_stats}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
