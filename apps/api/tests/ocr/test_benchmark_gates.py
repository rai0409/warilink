"""P12: 評価の再現性と昇格停止ゲート。不正な基準・古いキャッシュ・件数不足を「accepted」にしない。"""

import json
import random

import pytest

from app.ocr.evaluate import EvalBlocked, compare, evaluate, load_dataset, score_sample, wilson
from app.ocr.synth import make_sample
from app.ocr.tokens import ModelInfo

from .helpers import tokens_from_lines


def write_dataset(root, n, seed=1, **extra):
    """合成ラベルと、中身が決まったダミー画像を書く(認識は fake で行うので画素は使わない)。"""
    rng = random.Random(seed)
    lines = {}
    for i in range(n):
        s = make_sample(rng)
        (root / f"s{i:03d}.png").write_bytes(f"img-{seed}-{i}".encode())
        (root / f"s{i:03d}.json").write_text(json.dumps({**s.label, **extra}, ensure_ascii=False))
        lines[f"s{i:03d}"] = s.lines
    return lines


class FakeAdapter:
    def __init__(self, name="fake", version="1", weights="w1"):
        self.info = ModelInfo("fake", name, version, {"weights_sha256": weights})

    def model_info(self):
        return self.info


def recognizer(lines, drop_first_item=False):
    def rec(path):
        ls = list(lines[path.stem])
        if drop_first_item:
            ls = [ls[0], *ls[4:]]  # 店名・日付・空行の後の 1 行目の商品を落とす(認識の劣化を模擬)
        return tokens_from_lines(ls)
    return rec


def run(root, lines, adapter=None, drop=False, **kw):
    return evaluate(root, adapter or FakeAdapter(), low_confidence=0.9, recognize_and_prepare=recognizer(lines, drop),
                    **kw)


def test_zero_samples_are_blocked_not_accepted(tmp_path):
    r = evaluate(tmp_path, FakeAdapter(), low_confidence=0.9, recognize_and_prepare=lambda p: [], min_samples=1)
    assert r["status"] == "blocked" and r["metrics"]["receipt_exact_rate"] is None
    assert compare(r, r)["decision"] == "blocked"


def test_too_few_samples_are_insufficient(tmp_path):
    lines = write_dataset(tmp_path, 3)
    r = run(tmp_path, lines, min_samples=30)
    assert r["status"] == "insufficient"
    assert compare(r, r)["decision"] == "insufficient"


def test_same_amount_with_different_kind_is_not_exact():
    label = {"items": [{"name": "A", "line_minor": 1000}], "adjustments": [{"kind": "coupon", "amount_minor": -100}],
             "total_minor": 900}
    d = {"items": [{"name": "A", "quantity": 1, "line_minor": 1000}],
         "adjustments": [{"kind": "discount", "amount_minor": -100}], "total_minor": 900}
    r = score_sample(d, label)
    assert not r["exact"] and not r["adjustments_ok"]


def test_tax_group_mismatch_breaks_structure_but_not_amounts():
    label = {"items": [{"name": "A", "line_minor": 1000, "tax_group": "8"}], "adjustments": [], "total_minor": 1000}
    d = {"items": [{"name": "A", "quantity": 1, "line_minor": 1000, "tax_group": "10"}], "adjustments": [],
         "total_minor": 1000}
    r = score_sample(d, label)
    assert r["exact"] and not r["structure_exact"]


def test_same_data_before_after_accepted_and_regression_rejected(tmp_path):
    lines = write_dataset(tmp_path, 12)
    base = run(tmp_path, lines, min_samples=10)
    same = run(tmp_path, lines, min_samples=10)
    assert base["status"] == "ok" and compare(same, base)["decision"] == "accepted"
    worse = run(tmp_path, lines, min_samples=10, drop=True)
    c = compare(worse, base)
    assert c["decision"] == "rejected" and c["reasons"]


def test_changed_dataset_or_label_is_incomparable(tmp_path):
    lines = write_dataset(tmp_path, 12)
    base = run(tmp_path, lines, min_samples=10)
    lab = tmp_path / "s000.json"
    d = json.loads(lab.read_text())
    d["total_minor"] += 1  # ラベルの変更
    lab.write_text(json.dumps(d, ensure_ascii=False))
    assert compare(run(tmp_path, lines, min_samples=10), base)["decision"] == "incomparable"


def test_model_and_parser_changed_together_is_incomparable(tmp_path):
    lines = write_dataset(tmp_path, 12)
    base = run(tmp_path, lines, min_samples=10)
    cand = run(tmp_path, lines, min_samples=10, adapter=FakeAdapter(name="other"))
    assert compare(cand, base)["decision"] == "accepted"  # モデルだけの変更は比べられる
    cand["provenance"]["parser_version"] = "rule-ja-9.9.9"
    c = compare(cand, base)
    assert c["decision"] == "incomparable" and "同時" in c["reasons"][0]


def test_token_cache_must_match_model_preprocess_and_image(tmp_path):
    lines = write_dataset(tmp_path, 12)
    run(tmp_path, lines, min_samples=10)  # キャッシュを書く
    ok = evaluate(tmp_path, None, low_confidence=0.9, reuse_tokens=True, min_samples=10)
    assert ok["status"] == "ok" and ok["provenance"]["tokens_reused"]
    # 古い形式のキャッシュ
    c0 = tmp_path / "s000.tokens.json"
    old = json.loads(c0.read_text())
    c0.write_text(json.dumps({"model": "fake", "tokens": old["tokens"]}))
    with pytest.raises(EvalBlocked, match="古い"):
        evaluate(tmp_path, None, low_confidence=0.9, reuse_tokens=True, min_samples=10)
    # 別のモデルのキャッシュが混ざっている
    c0.write_text(json.dumps({**old, "model": {**old["model"], "name": "other"}}))
    with pytest.raises(EvalBlocked, match="混在"):
        evaluate(tmp_path, None, low_confidence=0.9, reuse_tokens=True, min_samples=10)
    # 画像が変わった
    c0.write_text(json.dumps(old))
    (tmp_path / "s000.png").write_bytes(b"changed")
    with pytest.raises(EvalBlocked, match="画像"):
        evaluate(tmp_path, None, low_confidence=0.9, reuse_tokens=True, min_samples=10)


def test_sources_are_reported_separately_and_real_receipts_need_consent_and_split(tmp_path):
    write_dataset(tmp_path, 2, seed=1)
    real = make_sample(random.Random(9)).label | {"label_status": "verified", "source": "real_ja"}
    for name, extra in [("r_ok", {"consent_eval": True, "split": "holdout"}), ("r_noconsent", {"split": "holdout"}),
                        ("r_nosplit", {"consent_eval": True}),
                        ("r_unverified", {"consent_eval": True, "split": "tune", "label_status": "proposed"})]:
        (tmp_path / f"{name}.png").write_bytes(name.encode())
        (tmp_path / f"{name}.json").write_text(json.dumps({**real, **extra}, ensure_ascii=False))
    samples, skipped = load_dataset(tmp_path)
    assert sorted(p.stem for p, _ in samples) == ["r_ok", "s000", "s001"]
    assert skipped == {"real_ja_no_consent": 1, "real_ja_no_split": 1, "label_proposed": 1}


def test_wilson_interval():
    assert wilson(0, 0) is None
    lo, hi = wilson(10, 10)
    assert 0.72 < lo < 0.73 and hi == 1.0
    lo, hi = wilson(5, 10)
    assert 0.23 < lo < 0.24 and 0.76 < hi < 0.77


def test_cli_exit_codes_for_blocked_and_insufficient(tmp_path, capsys):
    from app.ocr.cli import main as cli

    # 0 件 → blocked(3)。キャッシュの無い状態で再利用を指定 → blocked(3)
    assert cli(["evaluate", str(tmp_path), "--reuse-tokens"]) == 3
    lines = write_dataset(tmp_path, 3)
    assert cli(["evaluate", str(tmp_path), "--reuse-tokens"]) == 3  # キャッシュなし
    run(tmp_path, lines, min_samples=30)  # キャッシュを書く
    out = tmp_path / "base.json"
    assert cli(["evaluate", str(tmp_path), "--reuse-tokens", "--out", str(out)]) == 0
    # 件数不足の基準との比較は採用しない
    assert cli(["evaluate", str(tmp_path), "--reuse-tokens", "--baseline", str(out)]) == 3
    assert cli(["evaluate", str(tmp_path), "--reuse-tokens", "--baseline", str(out), "--min-samples", "3"]) == 3
    # 件数を満たす基準どうしなら accepted(0)
    assert cli(["evaluate", str(tmp_path), "--reuse-tokens", "--min-samples", "3", "--out", str(out)]) == 0
    assert cli(["evaluate", str(tmp_path), "--reuse-tokens", "--baseline", str(out), "--min-samples", "3"]) == 0
