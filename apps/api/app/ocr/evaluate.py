"""OCR(認識+解析)の評価。正解ラベルが verified(検証済み)か synthetic(合成)のものだけを使う。

利用者の訂正(label_status=proposed)は検証されるまで評価に使わない。
"""

from __future__ import annotations

import hashlib
import json
import statistics
import time
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

from app.ocr.parser import PARSER_VERSION, parse_receipt
from app.ocr.tokens import OcrAdapter, OcrToken

USABLE_LABELS = {"verified", "synthetic"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
# 大きいほど良い指標と、小さいほど良い指標
HIGHER_IS_BETTER = ["line_recall", "line_precision", "receipt_exact_rate", "total_correct_rate"]
LOWER_IS_BETTER = ["amount_error_rate"]


def _norm(name: str) -> str:
    return "".join(unicodedata.normalize("NFKC", name).split())


def _item_key(i: dict[str, Any]) -> tuple[str, int, int]:
    return (_norm(i["name"]), int(i.get("quantity", 1)), int(i["line_minor"]))


def score_sample(draft: dict[str, Any], label: dict[str, Any]) -> dict[str, Any]:
    want = Counter(_item_key(i) for i in label["items"])
    got = Counter(_item_key(i) for i in draft["items"])
    matched = sum((want & got).values())
    want_amounts = Counter(int(i["line_minor"]) for i in label["items"])
    got_amounts = Counter(int(i["line_minor"]) for i in draft["items"])
    wrong_amounts = sum((got_amounts - want_amounts).values())
    adj_ok = Counter(a["amount_minor"] for a in label.get("adjustments", [])) == Counter(
        a["amount_minor"] for a in draft.get("adjustments", []))
    total_ok = draft.get("total_minor") == label["total_minor"]
    return {
        "label_lines": sum(want.values()), "pred_lines": sum(got.values()), "matched_lines": matched,
        "wrong_amounts": wrong_amounts, "total_ok": total_ok,
        "exact": want == got and total_ok and adj_ok,
    }


def load_dataset(root: Path) -> tuple[list[tuple[Path, dict[str, Any]]], dict[str, int]]:
    samples, skipped = [], Counter[str]()
    for img in sorted(p for p in root.rglob("*") if p.suffix.lower() in IMAGE_EXT):
        lab = img.with_suffix(".json")
        if not lab.exists():
            skipped["no_label"] += 1
            continue
        label = json.loads(lab.read_text())
        if label.get("label_status") not in USABLE_LABELS:
            skipped[f"label_{label.get('label_status')}"] += 1
            continue
        samples.append((img, label))
    return samples, dict(skipped)


def dataset_digest(samples: list[tuple[Path, dict[str, Any]]]) -> str:
    h = hashlib.sha256()
    for img, label in samples:
        h.update(hashlib.sha256(img.read_bytes()).digest())
        h.update(json.dumps(label, sort_keys=True, ensure_ascii=False).encode())
    return h.hexdigest()[:16]


def evaluate(root: Path, adapter: OcrAdapter | None, *, low_confidence: float, reuse_tokens: bool = False,
             recognize_and_prepare: Any = None) -> dict[str, Any]:
    """recognize_and_prepare(path)->list[OcrToken] は前処理込みの認識関数(CLI から渡す)。"""
    samples, skipped = load_dataset(root)
    per: list[dict[str, Any]] = []
    latencies: list[float] = []
    info = adapter.model_info() if adapter else None
    cached_models: set[str] = set()
    for img, label in samples:
        # 認識結果のキャッシュ(パーサーだけを調整するときに OCR を再実行しないため)。モデル名を併記する
        cache = img.with_suffix(".tokens.json")
        if reuse_tokens and cache.exists():
            c = json.loads(cache.read_text())
            cached_models.add(c["model"])
            tokens = [OcrToken.from_json(t) for t in c["tokens"]]
        else:
            if recognize_and_prepare is None:
                raise RuntimeError(f"{cache.name} がありません。--reuse-tokens を外して OCR を実行してください")
            t = time.perf_counter()
            tokens = recognize_and_prepare(img)
            latencies.append((time.perf_counter() - t) * 1000)
            model = info.model_name if info else "unknown"
            cache.write_text(json.dumps({"model": model, "tokens": [t.to_json() for t in tokens]},
                                        ensure_ascii=False))
        draft = parse_receipt(tokens, low_confidence=low_confidence).to_json()
        r = score_sample(draft, label)
        r["sample"] = img.name
        per.append(r)
    n = len(per)
    label_lines = sum(r["label_lines"] for r in per)
    pred_lines = sum(r["pred_lines"] for r in per)
    matched = sum(r["matched_lines"] for r in per)
    metrics = {
        "samples": n,
        "line_recall": matched / label_lines if label_lines else None,
        "line_precision": matched / pred_lines if pred_lines else None,
        "receipt_exact_rate": sum(r["exact"] for r in per) / n if n else None,
        "total_correct_rate": sum(r["total_ok"] for r in per) / n if n else None,
        "amount_error_rate": sum(r["wrong_amounts"] for r in per) / pred_lines if pred_lines else None,
        "latency_ms_mean": statistics.fmean(latencies) if latencies else None,
        "latency_ms_p95": sorted(latencies)[int(0.95 * (len(latencies) - 1))] if latencies else None,
    }
    return {
        "metrics": metrics, "skipped": skipped, "dataset_digest": dataset_digest(samples),
        "parser_version": PARSER_VERSION,
        "model": None if info is None else {"adapter": info.adapter, "name": info.model_name,
                                             "version": info.model_version},
        "tokens_reused": reuse_tokens, "cached_token_models": sorted(cached_models), "samples": per,
        "note": "合成データ(synthetic)の結果は実レシートでの精度を表しません",
    }


def compare(candidate: dict[str, Any], baseline: dict[str, Any], tolerance: float = 0.0) -> dict[str, Any]:
    """候補が基準より悪化していれば rejected。データセットが違う場合は比較しない。"""
    if candidate["dataset_digest"] != baseline["dataset_digest"]:
        return {"decision": "incomparable", "reasons": ["評価データセットが基準と異なります"]}
    reasons = []
    c, b = candidate["metrics"], baseline["metrics"]
    for k in HIGHER_IS_BETTER:
        if c[k] is not None and b[k] is not None and c[k] < b[k] - tolerance:
            reasons.append(f"{k}: {b[k]:.4f} → {c[k]:.4f}")
    for k in LOWER_IS_BETTER:
        if c[k] is not None and b[k] is not None and c[k] > b[k] + tolerance:
            reasons.append(f"{k}: {b[k]:.4f} → {c[k]:.4f}")
    return {"decision": "rejected" if reasons else "accepted", "reasons": reasons}
