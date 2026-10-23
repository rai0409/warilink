"""商品別割当・数量・税・不一致時の確定拒否・入力検証。"""

import uuid

from .conftest import make_group


def itemized(ids, total=None, adjustments=None, items=None):
    A, B, C = ids["Aさん"], ids["Bさん"], ids["Cさん"]
    items = items or [
        {"id": "beer", "name": "生ビール", "line_minor": 1650, "quantity": 3, "tax_group": "10",
         "allocation": {"mode": "units", "shares": [{"member_id": A, "value": 2}, {"member_id": B, "value": 1}]}},
        {"id": "edamame", "name": "枝豆", "line_minor": 480, "quantity": 1, "tax_group": "10",
         "allocation": {"mode": "equal", "shares": [{"member_id": m, "value": 1} for m in (A, B, C)]}},
    ]
    adjustments = adjustments if adjustments is not None else [
        {"id": "tax10", "kind": "tax", "label": "外税10%", "amount_minor": 213,
         "rule": {"type": "proportional", "scope_tax_group": "10"}},
        {"id": "cpn", "kind": "coupon", "label": "クーポン", "amount_minor": -100, "rule": {"type": "equal"}},
    ]
    if total is None:
        total = sum(i["line_minor"] for i in items) + sum(a["amount_minor"] for a in adjustments)
    return {
        "title": "居酒屋",
        "amount_minor": total,
        "payers": [{"member_id": A, "amount_minor": total}],
        "split": {"mode": "itemized", "receipt": {"receipt_total_minor": total, "items": items,
                                                  "adjustments": adjustments}},
    }


def test_itemized_preview_and_create_match(user_factory):
    a = user_factory()
    g, ids = make_group(a)
    pid = g["public_id"]
    body = itemized(ids)
    eid = str(uuid.uuid4())
    p = a.post(f"/groups/{pid}/expenses/preview", json={**body, "expense_id": eid}).json()
    bd = p["breakdown"]
    assert bd["ok"] and sum(bd["shares"].values()) == body["amount_minor"]
    beer = next(x for x in bd["item_allocations"] if x["id"] == "beer")["allocations"]
    assert beer == {ids["Aさん"]: 1100, ids["Bさん"]: 550}
    assert p["projected_settlement"]["balance_sum_minor"] == 0

    r = a.post(f"/groups/{pid}/expenses", json={**body, "client_expense_id": eid})
    assert r.status_code == 201, r.text
    assert r.json()["shares"] == bd["shares"]  # プレビューと確定で端数配分が一致


def test_mismatch_blocks_confirmation(user_factory):
    a = user_factory()
    g, ids = make_group(a)
    pid = g["public_id"]
    body = itemized(ids)
    body["amount_minor"] += 7
    body["payers"][0]["amount_minor"] += 7
    body["split"]["receipt"]["receipt_total_minor"] += 7
    p = a.post(f"/groups/{pid}/expenses/preview", json=body).json()
    assert not p["breakdown"]["ok"] and p["projected_settlement"] is None
    mismatch = next(i for i in p["breakdown"]["issues"] if i["code"] == "RECEIPT_TOTAL_MISMATCH")
    assert mismatch["detail"]["difference"] == 7
    r = a.post(f"/groups/{pid}/expenses", json=body)
    assert r.status_code == 422 and r.json()["code"] == "RECEIPT_TOTAL_MISMATCH"
    assert a.get(f"/groups/{pid}/expenses").json() == []


def test_unallocated_and_units_mismatch_flagged(user_factory):
    a = user_factory()
    g, ids = make_group(a)
    pid = g["public_id"]
    body = itemized(ids, adjustments=[])
    body["split"]["receipt"]["items"][1]["allocation"] = None
    body["split"]["receipt"]["items"][0]["allocation"]["shares"][0]["value"] = 1  # 2杯→1杯で数量不一致
    issues = a.post(f"/groups/{pid}/expenses/preview", json=body).json()["breakdown"]["issues"]
    assert {(i["code"], i["target"]) for i in issues} >= {("ITEM_UNALLOCATED", "edamame"), ("UNITS_MISMATCH", "beer")}


def test_strict_integer_amounts(user_factory):
    a = user_factory()
    g, ids = make_group(a)
    pid = g["public_id"]
    body = {"title": "x", "amount_minor": 100.5, "payers": [{"member_id": ids["Aさん"], "amount_minor": 100}],
            "split": {"mode": "equal", "shares": [{"member_id": ids["Aさん"], "value": 1}]}}
    r = a.post(f"/groups/{pid}/expenses", json=body)
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR"
    body["amount_minor"] = "100"
    assert a.post(f"/groups/{pid}/expenses", json=body).status_code == 422
    body["amount_minor"] = 10**13
    assert a.post(f"/groups/{pid}/expenses", json=body).status_code == 422


def test_percentage_and_exact_and_multi_payer(user_factory):
    a = user_factory()
    g, ids = make_group(a, names=("Bさん", "Cさん"))
    pid = g["public_id"]
    A, B, C = ids["Aさん"], ids["Bさん"], ids["Cさん"]
    r = a.post(f"/groups/{pid}/expenses", json={
        "title": "宿", "amount_minor": 10001,
        "payers": [{"member_id": A, "amount_minor": 6001}, {"member_id": B, "amount_minor": 4000}],
        "split": {"mode": "percentage", "shares": [{"member_id": A, "value": 5000}, {"member_id": B, "value": 3000},
                                                   {"member_id": C, "value": 2000}]}})
    assert r.status_code == 201 and sum(r.json()["shares"].values()) == 10001
    r = a.post(f"/groups/{pid}/expenses", json={
        "title": "x", "amount_minor": 100, "payers": [{"member_id": A, "amount_minor": 100}],
        "split": {"mode": "exact", "shares": [{"member_id": A, "value": 30}, {"member_id": B, "value": 60}]}})
    assert r.status_code == 422 and r.json()["code"] == "SHARE_SUM_MISMATCH"
    r = a.post(f"/groups/{pid}/expenses", json={
        "title": "x", "amount_minor": 100, "payers": [{"member_id": A, "amount_minor": 100}],
        "split": {"mode": "equal", "shares": [{"member_id": A, "value": 1}, {"member_id": A, "value": 1}]}})
    assert r.status_code == 422 and r.json()["code"] == "DUPLICATE_MEMBER"
    bal = a.get(f"/groups/{pid}/balances").json()
    assert bal["balance_sum_minor"] == 0


def test_large_group_settlement_fallback_label(user_factory):
    a = user_factory()
    names = [f"m{i}" for i in range(29)]
    g, ids = make_group(a, names=names)
    pid = g["public_id"]
    all_ids = list(ids.values())
    # 立替者をばらけさせて非ゼロ残高を 16 人超にする
    for i, payer in enumerate(all_ids[:20]):
        amount = 10_000 + i * 137
        a.post(f"/groups/{pid}/expenses", json={
            "title": f"e{i}", "amount_minor": amount, "payers": [{"member_id": payer, "amount_minor": amount}],
            "split": {"mode": "equal", "shares": [{"member_id": m, "value": 1} for m in all_ids]}})
    p = a.post(f"/groups/{pid}/settlements/preview").json()
    opt = p["optimization"]
    assert opt["is_exact_minimum"] is False and opt["display_label"] == "送金回数を削減"
    assert opt["minimum_transfer_count"] is None and opt["fallback_reason"] in ("participant_limit", "time_limit")
    assert p["balance_sum_minor"] == 0
