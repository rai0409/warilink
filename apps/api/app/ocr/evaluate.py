"""OCR(認識+解析)の評価と、改善の採否を決めるゲート。

- 使うラベル: 合成(synthetic)、検証済み(verified)。利用者の訂正(proposed)は検証されるまで使わない。
- 出所(source)ごとに集計する: synthetic / public(CORD・SROIE 等。日本語の精度には算入しない)/
  real_ja(日本語の実レシート)。
  real_ja は本人の同意(consent_eval)・verified・分割(split: tune / holdout)がそろったものだけを使う。
- 比較は「同じデータセット・同じ評価版・変えた要素が 1 つだけ」のときだけ。件数不足・0 件・未測定は accepted にしない。
- トークンのキャッシュは、モデル・重み・前処理・画像が一致するときだけ再利用する(古い・混在は止める)。
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import time
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

from app.ocr.parser import PARSER_VERSION, parse_receipt
from app.ocr.tokens import OcrAdapter, OcrToken

EVAL_VERSION = "eval-0.2.0"
TOKEN_CACHE_VERSION = 2
DEFAULT_MIN_SAMPLES = 30
USABLE_LABELS = {"verified", "synthetic"}
SOURCES = ("synthetic", "public", "real_ja")
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
# 大きいほど良い指標と、小さいほど良い指標
HIGHER_IS_BETTER = ["line_recall", "line_precision", "receipt_exact_rate", "structure_exact_rate",
                    "total_correct_rate"]
LOWER_IS_BETTER = ["amount_error_rate"]
# 比較のときに「変えた要素」として数えるもの(2 つ以上同時に変わっていたら原因を切り分けられない)
COMPONENTS = ("model", "preprocess_version", "parser_version")


class EvalBlocked(Exception):
    """評価を実行できない(古い・混在したキャッシュ、画像の不一致など)。結果を出さずに止める。"""


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
    # 調整行は「種類+金額」で比べる(金額が同じでも割引とクーポンの取り違えは不一致)
    adj_ok = Counter((a["kind"], int(a["amount_minor"])) for a in label.get("adjustments", [])) == Counter(
        (a["kind"], int(a["amount_minor"])) for a in draft.get("adjustments", []))
    total_ok = draft.get("total_minor") == label["total_minor"]
    exact = want == got and total_ok and adj_ok
    # 構造: 税区分(8% / 10%)まで一致したか(ラベルに税区分がある商品だけを比べる)
    want_tax = Counter((*_item_key(i), i.get("tax_group")) for i in label["items"] if "tax_group" in i)
    got_tax = Counter((*_item_key(i), i.get("tax_group")) for i in draft["items"])
    tax_ok = not want_tax or not (want_tax - got_tax)
    return {
        "label_lines": sum(want.values()), "pred_lines": sum(got.values()), "matched_lines": matched,
        "wrong_amounts": wrong_amounts, "total_ok": total_ok, "adjustments_ok": adj_ok, "tax_groups_ok": tax_ok,
        "exact": exact, "structure_exact": exact and tax_ok,
    }


def _label_source(label: dict[str, Any]) -> str:
    return label.get("source") or ("synthetic" if label.get("label_status") == "synthetic" else "unknown")


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
        source = _label_source(label)
        if source not in SOURCES:
            skipped["unknown_source"] += 1
            continue
        if source == "real_ja":
            # 実レシートは本人の同意・検証済みラベル・調整用 / 最終評価用の分割がそろったものだけ
            if label.get("consent_eval") is not True:
                skipped["real_ja_no_consent"] += 1
                continue
            if label.get("split") not in ("tune", "holdout"):
                skipped["real_ja_no_split"] += 1
                continue
        samples.append((img, label))
    return samples, dict(skipped)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def dataset_digest(samples: list[tuple[Path, dict[str, Any]]]) -> str:
    h = hashlib.sha256()
    for img, label in samples:
        h.update(hashlib.sha256(img.read_bytes()).digest())
        h.update(json.dumps(label, sort_keys=True, ensure_ascii=False).encode())
    return h.hexdigest()[:16]


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """二項比率の 95% 信頼区間(Wilson)。n=0 なら None。"""
    if n == 0:
        return None
    p = successes / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return round(max(0.0, c - m), 4), round(min(1.0, c + m), 4)


def _metrics(per: list[dict[str, Any]], latencies: list[float], cpu: list[float]) -> dict[str, Any]:
    n = len(per)
    label_lines = sum(r["label_lines"] for r in per)
    pred_lines = sum(r["pred_lines"] for r in per)
    matched = sum(r["matched_lines"] for r in per)
    exact = sum(r["exact"] for r in per)
    structure = sum(r["structure_exact"] for r in per)
    total_ok = sum(r["total_ok"] for r in per)
    return {
        "samples": n,
        "line_recall": matched / label_lines if label_lines else None,
        "line_precision": matched / pred_lines if pred_lines else None,
        "receipt_exact_rate": exact / n if n else None,
        "receipt_exact_ci95": wilson(exact, n),
        "structure_exact_rate": structure / n if n else None,
        "structure_exact_ci95": wilson(structure, n),
        "total_correct_rate": total_ok / n if n else None,
        "total_correct_ci95": wilson(total_ok, n),
        "amount_error_rate": sum(r["wrong_amounts"] for r in per) / pred_lines if pred_lines else None,
        "latency_ms_mean": statistics.fmean(latencies) if latencies else None,
        "latency_ms_p95": sorted(latencies)[int(0.95 * (len(latencies) - 1))] if latencies else None,
        "cpu_seconds_per_receipt": statistics.fmean(cpu) if cpu else None,  # 原価の目安(このプロセスの CPU 時間)
    }


def _model_identity(info: Any) -> dict[str, Any]:
    params = getattr(info, "params", {}) or {}
    return {"adapter": info.adapter, "name": info.model_name, "version": info.model_version,
            # PaddleOcrAdapter は weights_sha256_16(モデル名 → 重みの SHA-256 先頭 16 桁)を返す
            "weights": params.get("weights_sha256_16") or params.get("weights_sha256") or params.get("weights")}


def _read_cache(cache: Path, image_sha: str) -> tuple[dict[str, Any], list[OcrToken]]:
    if not cache.exists():
        raise EvalBlocked(f"{cache.name} がありません。--reuse-tokens を外して OCR を実行してください")
    c = json.loads(cache.read_text())
    if c.get("cache_version") != TOKEN_CACHE_VERSION:
        raise EvalBlocked(f"{cache.name} は古い形式のキャッシュです。OCR を実行し直してください")
    if c.get("image_sha256") != image_sha:
        raise EvalBlocked(f"{cache.name} は別の画像の認識結果です(画像が変わっています)")
    return c, [OcrToken.from_json(t) for t in c["tokens"]]


def evaluate(root: Path, adapter: OcrAdapter | None, *, low_confidence: float, reuse_tokens: bool = False,
             recognize_and_prepare: Any = None, min_samples: int = DEFAULT_MIN_SAMPLES,
             preprocess_version: str | None = None) -> dict[str, Any]:
    """recognize_and_prepare(path)->list[OcrToken] は前処理込みの認識関数(CLI から渡す)。"""
    samples, skipped = load_dataset(root)
    per: list[dict[str, Any]] = []
    latencies: list[float] = []
    cpu: list[float] = []
    model = _model_identity(adapter.model_info()) if adapter else None
    cache_keys: set[str] = set()
    image_hashes = []
    for img, label in samples:
        image_sha = _sha(img.read_bytes())
        image_hashes.append(image_sha)
        cache = img.with_suffix(".tokens.json")
        if reuse_tokens:
            c, tokens = _read_cache(cache, image_sha)
            cache_keys.add(json.dumps({"model": c.get("model"), "pre": c.get("preprocess_version")}, sort_keys=True))
            model, preprocess_version = c.get("model"), c.get("preprocess_version")
        else:
            if recognize_and_prepare is None:
                raise EvalBlocked("認識関数がありません")
            t, p = time.perf_counter(), time.process_time()
            tokens = recognize_and_prepare(img)
            latencies.append((time.perf_counter() - t) * 1000)
            cpu.append(time.process_time() - p)
            cache.write_text(json.dumps({
                "cache_version": TOKEN_CACHE_VERSION, "model": model, "preprocess_version": preprocess_version,
                "image_sha256": image_sha, "tokens": [x.to_json() for x in tokens]}, ensure_ascii=False))
        draft = parse_receipt(tokens, low_confidence=low_confidence).to_json()
        r = score_sample(draft, label)
        r["sample"] = img.name
        r["source"] = _label_source(label)
        r["split"] = label.get("split")
        per.append(r)
    if len(cache_keys) > 1:
        raise EvalBlocked("異なるモデル・前処理のキャッシュが混在しています。OCR を実行し直してください")

    by_source = {s: _metrics([r for r in per if r["source"] == s], [], []) for s in SOURCES
                 if any(r["source"] == s for r in per)}
    if any(r["source"] == "real_ja" for r in per):
        by_source["real_ja_holdout"] = _metrics([r for r in per if r["source"] == "real_ja"
                                                 and r["split"] == "holdout"], [], [])
    n = len(per)
    status = "blocked" if n == 0 else "insufficient" if n < min_samples else "ok"
    label_versions = Counter(str(lbl.get("generator") or lbl.get("label_version") or "unknown") for _, lbl in samples)
    return {
        "status": status,
        "min_samples": min_samples,
        "metrics": _metrics(per, latencies, cpu),
        "by_source": by_source,
        "skipped": skipped,
        "dataset_digest": dataset_digest(samples),
        "provenance": {
            "eval_version": EVAL_VERSION,
            "dataset_digest": dataset_digest(samples),
            "images_digest": _sha("".join(image_hashes).encode())[:16],
            "label_versions": dict(label_versions),
            "parser_version": PARSER_VERSION,
            "preprocess_version": preprocess_version,
            "model": model,
            "token_cache_version": TOKEN_CACHE_VERSION,
            "tokens_reused": reuse_tokens,
        },
        "parser_version": PARSER_VERSION,
        "model": model,
        "samples": per,
        "note": ("合成データ(synthetic)・公開データ(public)の結果は日本語の実レシートでの精度を表しません。"
                 "実レシートの精度は real_ja の holdout だけで判断します"),
    }


def compare(candidate: dict[str, Any], baseline: dict[str, Any], tolerance: float = 0.0) -> dict[str, Any]:
    """候補を基準と比べる。

    accepted は「同じデータ・変えた要素が 1 つ以下・十分な件数・どの指標も悪化なし」のときだけ。
    """
    for r, who in ((candidate, "候補"), (baseline, "基準")):
        status = r.get("status")
        if status in ("blocked", "insufficient"):
            return {"decision": status, "reasons": [f"{who}の評価が {status} です(件数 {r['metrics'].get('samples')})"]}
        if status != "ok":
            return {"decision": "blocked", "reasons": [f"{who}の評価結果の形式が古いか不明です"]}
    if candidate["dataset_digest"] != baseline["dataset_digest"]:
        return {"decision": "incomparable", "reasons": ["評価データセット(画像・ラベル)が基準と異なります"]}
    cp, bp = candidate["provenance"], baseline["provenance"]
    if cp.get("eval_version") != bp.get("eval_version"):
        return {"decision": "incomparable", "reasons": ["評価の版が基準と異なります"]}
    changed = [k for k in COMPONENTS if cp.get(k) != bp.get(k)]
    if len(changed) > 1:
        return {"decision": "incomparable",
                "reasons": [f"複数の要素が同時に変わっています({', '.join(changed)})。1 つずつ比べてください"]}
    c, b = candidate["metrics"], baseline["metrics"]
    if any(c.get(k) is None or b.get(k) is None for k in HIGHER_IS_BETTER + LOWER_IS_BETTER):
        return {"decision": "blocked", "reasons": ["未測定の指標があります"]}
    reasons = []
    for k in HIGHER_IS_BETTER:
        if c[k] < b[k] - tolerance:
            reasons.append(f"{k}: {b[k]:.4f} → {c[k]:.4f}")
    for k in LOWER_IS_BETTER:
        if c[k] > b[k] + tolerance:
            reasons.append(f"{k}: {b[k]:.4f} → {c[k]:.4f}")
    return {"decision": "rejected" if reasons else "accepted", "reasons": reasons, "changed": changed}
