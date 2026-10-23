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


def test_compare_rejects_regression_and_refuses_different_dataset():
    base = {"dataset_digest": "x", "metrics": {"line_recall": 0.9, "line_precision": 0.9, "receipt_exact_rate": 0.5,
                                               "total_correct_rate": 0.8, "amount_error_rate": 0.05}}
    worse = {"dataset_digest": "x", "metrics": {**base["metrics"], "receipt_exact_rate": 0.4}}
    better = {"dataset_digest": "x", "metrics": {**base["metrics"], "amount_error_rate": 0.01}}
    assert compare(worse, base)["decision"] == "rejected"
    assert compare(better, base)["decision"] == "accepted"
    assert compare({**better, "dataset_digest": "y"}, base)["decision"] == "incomparable"


def test_dataset_uses_only_verified_or_synthetic_labels(tmp_path):
    for name, status in [("a", "verified"), ("b", "synthetic"), ("c", "proposed"), ("d", None)]:
        (tmp_path / f"{name}.png").write_bytes(b"x")
        (tmp_path / f"{name}.json").write_text(json.dumps({"label_status": status, **LABEL}))
    (tmp_path / "e.png").write_bytes(b"x")  # ラベルなし
    samples, skipped = load_dataset(tmp_path)
    assert sorted(p.stem for p, _ in samples) == ["a", "b"]
    assert skipped == {"label_proposed": 1, "label_None": 1, "no_label": 1}


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
