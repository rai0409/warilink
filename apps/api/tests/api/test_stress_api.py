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


def test_settlement_confirmation_is_serialized_and_idempotent(user_factory):
    """同時に精算を確定しても 1 件だけ作られ、同じ Idempotency-Key の再送は同じ結果を返す(二重確定しない)。"""
    from concurrent.futures import ThreadPoolExecutor

    owner = user_factory()
    g, ids = make_group(owner, names=[f"メンバー{i}" for i in range(1, 6)], title="同時確定")
    pid = g["public_id"]
    members = list(ids.values())
    for e in range(30):
        r = owner.post(f"/groups/{pid}/expenses", json={
            "title": f"支出{e}", "amount_minor": 1_000 + e,
            "payers": [{"member_id": members[e % 6], "amount_minor": 1_000 + e}],
            "split": {"mode": "equal", "shares": [{"member_id": m, "value": 1} for m in members]}})
        assert r.status_code == 201
    v = owner.get(f"/groups/{pid}").json()["ledger_version"]
    body = {"expected_ledger_version": v}

    def confirm(key):
        return owner.post(f"/groups/{pid}/settlement-batches", json=body, key=key).status_code

    import uuid

    keys = [str(uuid.uuid4()) for _ in range(6)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = list(pool.map(confirm, keys))
    assert codes.count(201) == 1 and all(c in (201, 409) for c in codes), codes
    batches = owner.get(f"/groups/{pid}/settlement-batches").json()
    assert len([b for b in batches if b["state"] == "active"]) == 1
    # 同じキーでの再送は同じ応答(新しい確定を作らない)
    assert confirm(keys[0]) == codes[0]
    assert len(owner.get(f"/groups/{pid}/settlement-batches").json()) == len(batches)
    record("api_settlement_concurrency", requests=6, created=1, conflicts=codes.count(409))


def _all_ints(obj):
    if isinstance(obj, bool):
        return
    if isinstance(obj, int):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _all_ints(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _all_ints(v)


def test_amount_limit_and_js_safe_output(user_factory):
    """1 件の上限は 10^12 円(超えると 422)。上限の支出が複数あっても精算案は作れ、応答の整数は
    すべて JS の安全整数(2^53-1)に収まる。"""
    owner = user_factory()
    g, ids = make_group(owner, names=["Bさん", "Cさん"], title="上限")
    pid = g["public_id"]
    a, b, c = ids["Aさん"], ids["Bさん"], ids["Cさん"]

    def body(amount, payer, sharer):
        return {"title": "上限", "amount_minor": amount, "payers": [{"member_id": payer, "amount_minor": amount}],
                "split": {"mode": "equal", "shares": [{"member_id": sharer, "value": 1}]}}

    r = owner.post(f"/groups/{pid}/expenses", json=body(10**12 + 1, a, b))
    assert r.status_code == 422, r.text
    for _ in range(3):
        r = owner.post(f"/groups/{pid}/expenses", json=body(10**12, a, b))
        assert r.status_code == 201, r.text
    r = owner.post(f"/groups/{pid}/expenses", json=body(10**12, c, a))
    assert r.status_code == 201, r.text
    r = owner.post(f"/groups/{pid}/settlements/preview")
    assert r.status_code == 200, r.text
    plan = r.json()
    net = {x["member_id"]: x["net_minor"] for x in plan["balances"]}
    assert net == {a: 2 * 10**12, b: -3 * 10**12, c: 10**12}
    assert max(abs(v) for v in _all_ints(plan)) <= 2**53 - 1
    assert sorted((t["from_member_id"], t["to_member_id"], t["amount_minor"]) for t in plan["transfers"]) == sorted(
        [(b, a, 2 * 10**12), (b, c, 10**12)])
    record("api_amount_limit", per_expense_limit=10**12, max_balance=3 * 10**12, status=r.status_code)


@pytest.mark.parametrize("upper", [False, True])
def test_itemized_preview_matches_saved_shares(user_factory, upper):
    """プレビューで見せた負担額と、登録後に保存された負担額が 1 円単位で一致する(同順位の端数が多い入力)。
    画面は小文字の UUID を送るが、大文字で送られても同じ支出 ID として扱われる。"""
    import uuid

    owner = user_factory()
    g, ids = make_group(owner, names=["Bさん", "Cさん", "Dさん", "Eさん", "Fさん"], title="端数")
    pid = g["public_id"]
    members = list(ids.values())
    rng = random.Random(SEED + 22)
    items = [{"id": f"i{i}", "name": f"商品{i}", "line_minor": rng.choice([100, 101, 1_000, 1_001]), "quantity": 1,
              "tax_group": "10", "allocation": {"mode": "equal", "shares": [
                  {"member_id": m, "value": 1} for m in rng.sample(members, rng.randint(2, 6))]}}
             for i in range(20)]
    base = sum(i["line_minor"] for i in items)
    adjustments = [{"id": "t", "kind": "tax", "amount_minor": base // 10, "rule": {"type": "proportional",
                                                                                     "scope_tax_group": "10"}},
                   {"id": "d", "kind": "discount", "amount_minor": -7, "rule": {"type": "equal"}}]
    total = base + base // 10 - 7
    eid = str(uuid.uuid4())
    if upper:
        eid = eid.upper()
    spec = {"title": "端数", "amount_minor": total, "payers": [{"member_id": members[0], "amount_minor": total}],
            "split": {"mode": "itemized", "receipt": {"receipt_total_minor": total, "items": items,
                                                       "adjustments": adjustments}}}
    r = owner.post(f"/groups/{pid}/expenses/preview", json={**spec, "expense_id": eid})
    assert r.status_code == 200, r.text
    preview = r.json()["breakdown"]
    assert preview["ok"], preview["issues"]
    r = owner.post(f"/groups/{pid}/expenses", json={**spec, "client_expense_id": eid})
    assert r.status_code == 201, r.text
    saved = r.json()
    assert saved["shares"] == preview["shares"]
    assert saved["breakdown"]["adjustment_allocations"] == preview["adjustment_allocations"]
