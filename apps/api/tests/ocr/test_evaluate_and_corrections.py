import json

from app.ocr.corrections import diff_draft
from app.ocr.evaluate import compare, load_dataset, score_sample

LABEL = {"items": [{"name": "生ビール", "quantity": 2, "line_minor": 1300}, {"name": "唐揚げ", "line_minor": 800}],
         "adjustments": [{"kind": "tax", "amount_minor": 210}], "total_minor": 2310}


def draft(items, total=2310, adjustments=({"kind": "tax", "amount_minor": 210},)):
    return {"items": items, "adjustments": list(adjustments), "total_minor": total}


def test_score_exact_and_amount_error():
    ok = score_sample(draft([{"name": "生 ビール", "quantity": 2, "line_minor": 1300},
                             {"name": "唐揚げ", "quantity": 1, "line_minor": 800}]), LABEL)
    assert ok["exact"] and ok["matched_lines"] == 2  # 空白の違いは同じ商品名とみなす
    bad = score_sample(draft([{"name": "生ビール", "quantity": 2, "line_minor": 1800},
                              {"name": "唐揚げ", "quantity": 1, "line_minor": 800}]), LABEL)
    assert not bad["exact"] and bad["wrong_amounts"] == 1 and bad["matched_lines"] == 1


def _result(digest="x", **metrics):
    base = {"line_recall": 0.9, "line_precision": 0.9, "receipt_exact_rate": 0.5, "structure_exact_rate": 0.5,
            "total_correct_rate": 0.8, "amount_error_rate": 0.05, "samples": 40}
    prov = {"eval_version": "eval-0.2.0", "parser_version": "p", "preprocess_version": "c", "model": {"name": "m"}}
    return {"status": "ok", "dataset_digest": digest, "metrics": {**base, **metrics}, "provenance": prov}


def test_compare_rejects_regression_and_refuses_different_dataset():
    base = _result()
    assert compare(_result(receipt_exact_rate=0.4), base)["decision"] == "rejected"
    assert compare(_result(amount_error_rate=0.01), base)["decision"] == "accepted"
    assert compare(_result(digest="y"), base)["decision"] == "incomparable"
    # P12: 形式の古い結果(status なし)・未測定の指標は accepted にしない
    legacy = {"dataset_digest": "x", "metrics": base["metrics"]}
    assert compare(legacy, base)["decision"] == "blocked"
    assert compare(_result(line_recall=None), base)["decision"] == "blocked"


def test_dataset_uses_only_verified_or_synthetic_labels(tmp_path):
    for name, status, source in [("a", "verified", "public"), ("b", "synthetic", None), ("c", "proposed", None),
                                 ("d", None, None), ("f", "verified", None)]:
        (tmp_path / f"{name}.png").write_bytes(b"x")
        (tmp_path / f"{name}.json").write_text(json.dumps({"label_status": status, **LABEL}
                                                          | ({"source": source} if source else {})))
    (tmp_path / "e.png").write_bytes(b"x")  # ラベルなし
    samples, skipped = load_dataset(tmp_path)
    assert sorted(p.stem for p, _ in samples) == ["a", "b"]
    # 出所(source)の無い verified は区分できないため使わない(P12)
    assert skipped == {"label_proposed": 1, "label_None": 1, "no_label": 1, "unknown_source": 1}


def test_corrections_are_classified():
    d = {"total_minor": 2300, "items": [
        {"id": "ocr-i1", "name": "生ビ一ル", "quantity": 2, "line_minor": 1300, "tax_group": None,
         "bbox": [0, 0, 1, 1]},
        {"id": "ocr-i2", "name": "小計", "quantity": 1, "line_minor": 2100, "tax_group": None, "bbox": [0, 2, 1, 3]},
    ], "adjustments": [{"id": "ocr-a1", "kind": "tax", "amount_minor": 210, "bbox": [0, 4, 1, 5]}]}
    confirmed = {"receipt_total_minor": 2310, "items": [
        {"id": "ocr-i1", "name": "生ビール", "quantity": 2, "line_minor": 1300, "tax_group": None},
        {"id": "new-1", "name": "唐揚げ", "quantity": 1, "line_minor": 800, "tax_group": None},
    ], "adjustments": [{"id": "ocr-a1", "kind": "tax", "amount_minor": 210}]}
    got = {(c["field_path"], c["error_class"]) for c in diff_draft(d, confirmed)}
    assert got == {("receipt_total_minor", "recognition"), ("items[ocr-i1].name", "recognition"),
                   ("items[ocr-i2]", "spurious"), ("items[new-1]", "missing")}
