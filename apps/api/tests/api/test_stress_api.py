"""API と DB を通した大量計算(`make test-stress`)。支出 2,000 件のグループと、上限 300 品目のレシート。

保存した負担額から計算し直した残高と、API の精算案が一致することを確かめる。
"""

from __future__ import annotations

import random

import pytest

from ..stress.support import SEED, Timer, record
from .conftest import make_group

pytestmark = [pytest.mark.stress, pytest.mark.db]


def test_group_with_2000_expenses(user_factory):
    rng = random.Random(SEED + 20)
    owner = user_factory()
    g, ids = make_group(owner, names=[f"メンバー{i:02d}" for i in range(1, 50)], title="大量の支出")
    pid = g["public_id"]
    members = list(ids.values())
    assert len(members) == 50

    paid = dict.fromkeys(members, 0)
    owed = dict.fromkeys(members, 0)
    timer = Timer()
    for e in range(2_000):
        amount = rng.randint(1, 300_000)
        payer_ids = rng.sample(members, 1 if rng.random() < 0.8 else rng.randint(2, 4))
        first = rng.randint(0, amount)
        payer_amounts = [first, amount - first] if len(payer_ids) == 2 else [amount // len(payer_ids)] * len(payer_ids)
        payer_amounts[-1] = amount - sum(payer_amounts[:-1])
        sharers = rng.sample(members, rng.randint(1, 50))
        mode = rng.choice(["equal", "weight"])
        body = {
            "title": f"支出{e}",
            "amount_minor": amount,
            "payers": [{"member_id": p, "amount_minor": a} for p, a in zip(payer_ids, payer_amounts, strict=True)],
            "split": {"mode": mode, "shares": [{"member_id": m, "value": 1 if mode == "equal" else rng.randint(1, 4)}
                                               for m in sharers]},
        }
        with timer.measure():
            r = owner.post(f"/groups/{pid}/expenses", json=body)
        assert r.status_code == 201, (e, r.text)
        shares = r.json()["shares"]
        assert sum(shares.values()) == amount, e
        for p, a in zip(payer_ids, payer_amounts, strict=True):
            paid[p] += a
        for m, v in shares.items():
            owed[m] += v

    create = timer.summary()
    preview_timer = Timer()
    with preview_timer.measure():
        r = owner.post(f"/groups/{pid}/settlements/preview")
    assert r.status_code == 200, r.text
    plan = r.json()
    expected_net = {m: paid[m] - owed[m] for m in members}
    assert sum(expected_net.values()) == 0
    assert plan["balance_sum_minor"] == 0
    assert {b["member_id"]: b["net_minor"] for b in plan["balances"]} == expected_net
    rest = dict(expected_net)
    for t in plan["transfers"]:
        assert t["amount_minor"] > 0
        rest[t["from_member_id"]] += t["amount_minor"]
        rest[t["to_member_id"]] -= t["amount_minor"]
    assert all(v == 0 for v in rest.values())  # 精算案どおりに送金すると全員 0

    # 確定でもサーバーが再検証する
    r = owner.post(f"/groups/{pid}/settlement-batches", json={"expected_ledger_version": plan["ledger_version"]})
    assert r.status_code == 201, r.text
    preview = preview_timer.summary()
    assert preview["max_ms"] < 15_000, preview  # ゆるい上限
    record("api_group_2000_expenses", members=50, expenses=2_000, create_expense=create,
           preview_ms=preview["max_ms"], transfers=len(plan["transfers"]),
           method=plan["optimization"]["method"])


def test_itemized_expense_at_item_limit(user_factory):
    """API の上限(300 品目・調整 50 件)のレシートを登録でき、保存した負担額の合計が一致する。"""
    rng = random.Random(SEED + 21)
    owner = user_factory()
    g, ids = make_group(owner, names=[f"メンバー{i:02d}" for i in range(1, 30)], title="大きなレシート")
    pid = g["public_id"]
    members = list(ids.values())
    items = []
    for i in range(300):
        qty = rng.choice([1, 2, 3])
        picked = rng.sample(members, rng.randint(1, 10))
        items.append({"id": f"i{i}", "name": f"商品{i}", "line_minor": rng.randint(1, 3_000) * qty, "quantity": qty,
                      "tax_group": rng.choice(["8", "10"]),
                      "allocation": {"mode": "equal", "shares": [{"member_id": m, "value": 1} for m in picked]}})
    adjustments = [{"id": f"a{k}", "kind": "service_charge", "amount_minor": rng.randint(1, 500),
                    "rule": {"type": "equal"}} for k in range(50)]
    total = sum(i["line_minor"] for i in items) + sum(a["amount_minor"] for a in adjustments)
    body = {"title": "宴会", "amount_minor": total, "payers": [{"member_id": members[0], "amount_minor": total}],
            "split": {"mode": "itemized", "receipt": {"receipt_total_minor": total, "items": items,
                                                       "adjustments": adjustments}}}
    timer = Timer()
    with timer.measure():
        r = owner.post(f"/groups/{pid}/expenses", json=body)
    assert r.status_code == 201, r.text
    assert sum(r.json()["shares"].values()) == total
    record("api_itemized_300_items", items=300, adjustments=50, members=30, elapsed_ms=timer.summary()["max_ms"])
