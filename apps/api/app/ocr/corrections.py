"""読み取り結果(draft)と利用者が確定した明細の差分を、誤りの種類ごとに分類する。

分類は評価の集計用で、利用者の訂正を正解として自動採用するものではない(label_status=proposed)。
"""

from __future__ import annotations

from typing import Any


def diff_draft(draft: dict[str, Any], receipt_spec: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def add(path: str, before: Any, after: Any, cls: str, bbox: Any = None) -> None:
        out.append({"field_path": path, "before": None if before is None else {"v": before},
                    "after": None if after is None else {"v": after}, "error_class": cls, "bbox": bbox})

    if draft.get("total_minor") != receipt_spec.get("receipt_total_minor"):
        add("receipt_total_minor", draft.get("total_minor"), receipt_spec.get("receipt_total_minor"),
            "missing" if draft.get("total_minor") is None else "recognition")

    confirmed = {i["id"]: i for i in receipt_spec.get("items", [])}
    for d in draft.get("items", []):
        c = confirmed.pop(d["id"], None)
        if c is None:
            add(f"items[{d['id']}]", {"name": d["name"], "line_minor": d["line_minor"]}, None, "spurious", d["bbox"])
            continue
        for f in ("name", "line_minor", "quantity"):
            if d.get(f) != c.get(f):
                add(f"items[{d['id']}].{f}", d.get(f), c.get(f), "recognition", d["bbox"])
        if (d.get("tax_group") or None) != (c.get("tax_group") or None):
            add(f"items[{d['id']}].tax_group", d.get("tax_group"), c.get("tax_group"), "structure", d["bbox"])
    for c in confirmed.values():
        add(f"items[{c['id']}]", None, {"name": c.get("name"), "line_minor": c.get("line_minor")}, "missing")

    confirmed_adj = {a["id"]: a for a in receipt_spec.get("adjustments", [])}
    for d in draft.get("adjustments", []):
        c = confirmed_adj.pop(d["id"], None)
        if c is None:
            add(f"adjustments[{d['id']}]", {"kind": d["kind"], "amount_minor": d["amount_minor"]}, None, "spurious",
                d["bbox"])
            continue
        if d["kind"] != c.get("kind"):
            add(f"adjustments[{d['id']}].kind", d["kind"], c.get("kind"), "structure", d["bbox"])
        if d["amount_minor"] != c.get("amount_minor"):
            add(f"adjustments[{d['id']}].amount_minor", d["amount_minor"], c.get("amount_minor"), "recognition",
                d["bbox"])
    for c in confirmed_adj.values():
        # 利用者が明示的に足した差額調整行も「読み落とし」として記録する(実際の原因は検証時に判断)
        add(f"adjustments[{c['id']}]", None, {"kind": c.get("kind"), "amount_minor": c.get("amount_minor")}, "missing")
    return out
