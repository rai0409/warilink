"""PR D: 実レシートの精度の判定(real_ja の holdout だけ)と、分割の汚染の検出。"""

import json

import pytest

from app.ocr.evaluate import EvalBlocked, evaluate, release_gates


def _result(holdout_n=None, **metrics):
    by = {"synthetic": {"samples": 60, "receipt_exact_rate": 1.0}}
    if holdout_n is not None:
        by["real_ja_holdout"] = {"samples": holdout_n, **metrics}
    return {"by_source": by}


def test_synthetic_results_never_pass_the_release_gate():
    g = release_gates(_result())
    assert g["OCR_REAL_HOLDOUT"] == "BLOCKED" and g["OCR_RELEASE"] == "BLOCKED"  # 合成 60 枚の 1.0 は使わない
    assert release_gates(_result(12, receipt_exact_rate=1.0))["OCR_REAL_HOLDOUT"] == "INSUFFICIENT"
    g = release_gates(_result(40, receipt_exact_rate=0.95))
    assert g["OCR_REAL_HOLDOUT"] == "MEASURED" and g["OCR_RELEASE"] == "BLOCKED"  # 閾値が未決定
    assert "OWNER_APPROVAL_REQUIRED" in g["reasons"][0]


def test_thresholds_when_decided():
    th = {"receipt_exact_rate": 0.9, "structure_exact_rate": 0.8}
    assert release_gates(_result(40, receipt_exact_rate=0.95, structure_exact_rate=0.85), thresholds=th)[
        "OCR_RELEASE"] == "PASS"
    g = release_gates(_result(40, receipt_exact_rate=0.95, structure_exact_rate=0.7), thresholds=th)
    assert g["OCR_RELEASE"] == "FAIL" and "structure_exact_rate" in g["reasons"][0]


def _sample(d, name, split, png=b"\x89PNG same image"):
    (d / f"{name}.png").write_bytes(png)
    (d / f"{name}.json").write_text(json.dumps({
        "label_status": "verified", "source": "real_ja", "consent_eval": True, "split": split,
        "items": [], "total_minor": 0}))


def test_same_receipt_in_tune_and_holdout_is_blocked(tmp_path):
    _sample(tmp_path, "a", "tune")
    _sample(tmp_path, "b", "holdout")  # 同じ画像
    with pytest.raises(EvalBlocked, match="汚染"):
        evaluate(tmp_path, None, low_confidence=0.9, recognize_and_prepare=lambda p: [])


def test_holdout_without_consent_or_split_is_not_used(tmp_path):
    _sample(tmp_path, "a", "holdout", png=b"img-a")
    (tmp_path / "b.png").write_bytes(b"img-b")
    (tmp_path / "b.json").write_text(json.dumps({"label_status": "verified", "source": "real_ja", "split": "holdout",
                                                 "items": [], "total_minor": 0}))  # 同意なし
    r = evaluate(tmp_path, None, low_confidence=0.9, recognize_and_prepare=lambda p: [], min_samples=1)
    assert r["by_source"]["real_ja_holdout"]["samples"] == 1 and r["skipped"] == {"real_ja_no_consent": 1}
    assert r["release_gates"]["OCR_REAL_HOLDOUT"] == "MEASURED"
