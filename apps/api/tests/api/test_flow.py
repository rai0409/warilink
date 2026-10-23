"""E2E 相当の API フロー・認可・冪等性・同時実行。"""

import threading
import uuid

from .conftest import equal_expense, join_as, make_group


def test_guest_group_expense_settlement_flow(user_factory):
    a, b = user_factory(), user_factory()
    g, ids = make_group(a)
    pid = g["public_id"]
    join_as(b, a, pid, "Bさん")
    members = list(ids.values())

    r = a.post(f"/groups/{pid}/expenses", json=equal_expense(ids["Aさん"], 4252, members))
    assert r.status_code == 201, r.text
    shares = r.json()["shares"]
    assert sum(shares.values()) == 4252 and sorted(shares.values()) == [1063, 1063, 1063, 1063]

    r = b.post(f"/groups/{pid}/expenses", json=equal_expense(ids["Bさん"], 1001, [ids["Bさん"], ids["Cさん"]]))
    assert r.status_code == 201

    bal = a.get(f"/groups/{pid}/balances").json()
    assert bal["balance_sum_minor"] == 0

    prev = a.post(f"/groups/{pid}/settlements/preview").json()
    opt = prev["optimization"]
    assert opt["is_exact_minimum"] and opt["display_label"] == "最小送金回数"
    assert opt["assumptions"]["transfer_fees_considered"] is False
    total_out = {}
    for t in prev["transfers"]:
        total_out[t["from_member_id"]] = total_out.get(t["from_member_id"], 0) - t["amount_minor"]
        total_out[t["to_member_id"]] = total_out.get(t["to_member_id"], 0) + t["amount_minor"]
    for row in prev["balances"]:
        assert total_out.get(row["member_id"], 0) == row["net_minor"]

    batch = a.post(f"/groups/{pid}/settlement-batches", json={"expected_ledger_version": prev["ledger_version"]})
    assert batch.status_code == 201, batch.text
    transfers = batch.json()["transfers"]
    t_b = next(t for t in transfers if t["from_member_id"] == ids["Bさん"])

    # 支払者 B だけが送金申告できる
    assert a.post(f"/settlements/{t_b['id']}/report-paid").status_code in (403, 404)
    r = b.post(f"/settlements/{t_b['id']}/report-paid")
    assert r.status_code == 200 and r.json()["status"] == "reported_paid"
    # 二度押しは冪等
    assert b.post(f"/settlements/{t_b['id']}/report-paid").json()["status"] == "reported_paid"
    # 受取人だけが受取確認できる(B は不可)
    assert b.post(f"/settlements/{t_b['id']}/confirm-received").status_code == 403
    if t_b["to_member_id"] == ids["Aさん"]:
        r = a.post(f"/settlements/{t_b['id']}/confirm-received")
        assert r.status_code == 200 and r.json()["status"] == "recipient_confirmed"
    detail = b.get(f"/settlements/{t_b['id']}").json()
    assert detail["bank_evidence_state"] == "not_connected"


def test_group_isolation_and_hidden_existence(user_factory):
    a, outsider = user_factory(), user_factory()
    g, ids = make_group(a)
    pid = g["public_id"]
    a.post(f"/groups/{pid}/expenses", json=equal_expense(ids["Aさん"], 1000, list(ids.values())))
    for path in (f"/groups/{pid}", f"/groups/{pid}/expenses", f"/groups/{pid}/balances"):
        assert outsider.get(path).status_code == 404
    assert outsider.post(f"/groups/{pid}/settlements/preview").status_code == 404
    r = outsider.post(f"/groups/{pid}/expenses", json=equal_expense(ids["Aさん"], 1, [ids["Aさん"]]))
    assert r.status_code == 404
    # 別グループのメンバーIDを混ぜた支出は拒否
    g2, ids2 = make_group(outsider)
    r = outsider.post(f"/groups/{g2['public_id']}/expenses",
                      json=equal_expense(ids2["Aさん"], 1000, [ids2["Aさん"], ids["Bさん"]]))
    assert r.status_code == 422 and r.json()["code"] == "UNKNOWN_MEMBER"


def test_transfer_visible_only_to_parties(user_factory):
    a, b, c = user_factory(), user_factory(), user_factory()
    g, ids = make_group(a)
    pid = g["public_id"]
    join_as(b, a, pid, "Bさん")
    join_as(c, a, pid, "Cさん")
    a.post(f"/groups/{pid}/expenses", json=equal_expense(ids["Aさん"], 2000, [ids["Aさん"], ids["Bさん"]]))
    v = a.get(f"/groups/{pid}").json()["ledger_version"]
    t = a.post(f"/groups/{pid}/settlement-batches", json={"expected_ledger_version": v}).json()["transfers"][0]
    assert b.get(f"/settlements/{t['id']}").status_code == 200
    assert a.get(f"/settlements/{t['id']}").status_code == 200
    assert c.get(f"/settlements/{t['id']}").status_code == 404  # 同じグループでも当事者以外は不可
    assert c.post(f"/settlements/{t['id']}/report-paid").status_code == 404


def test_unauthenticated_and_csrf(user_factory):
    from fastapi.testclient import TestClient

    from app.main import app

    anon = TestClient(app)
    assert anon.get("/api/v1/groups/x").status_code == 401
    a = user_factory()
    r = a.c.post("/api/v1/groups", json={"title": "x", "my_name": "a"},
                 headers={"X-WariLink-CSRF": "0", "Idempotency-Key": str(uuid.uuid4())})
    assert r.status_code == 403 and r.json()["code"] == "CSRF_REJECTED"
    r = a.c.post("/api/v1/groups", json={"title": "x", "my_name": "a"},
                 headers={"Origin": "https://evil.example", "Idempotency-Key": str(uuid.uuid4())})
    assert r.status_code == 403


def test_idempotent_create_and_conflict(user_factory):
    a = user_factory()
    g, ids = make_group(a)
    pid = g["public_id"]
    body = equal_expense(ids["Aさん"], 3000, list(ids.values()))
    r1 = a.post(f"/groups/{pid}/expenses", json=body, key="k-expense-0001")
    r2 = a.post(f"/groups/{pid}/expenses", json=body, key="k-expense-0001")
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"] and r2.headers.get("idempotent-replayed") == "true"
    assert len(a.get(f"/groups/{pid}/expenses").json()) == 1
    r3 = a.post(f"/groups/{pid}/expenses", json={**body, "amount_minor": 3001,
                                                  "payers": [{"member_id": ids["Aさん"], "amount_minor": 3001}]},
                key="k-expense-0001")
    assert r3.status_code == 409 and r3.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert a.c.post(f"/api/v1/groups/{pid}/expenses", json=body).status_code == 400  # キー無し


def test_concurrent_duplicate_posts_create_once(user_factory):
    a = user_factory()
    g, ids = make_group(a)
    pid = g["public_id"]
    body = equal_expense(ids["Aさん"], 5000, list(ids.values()))
    results = []

    def go():
        results.append(a.post(f"/groups/{pid}/expenses", json=body, key="same-key-concurrent"))

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert all(r.status_code == 201 for r in results), [r.text for r in results]
    assert len({r.json()["id"] for r in results}) == 1
    assert len(a.get(f"/groups/{pid}/expenses").json()) == 1


def test_optimistic_lock_on_concurrent_edit(user_factory):
    a, b = user_factory(), user_factory()
    g, ids = make_group(a)
    pid = g["public_id"]
    join_as(b, a, pid, "Bさん")
    e = a.post(f"/groups/{pid}/expenses", json=equal_expense(ids["Aさん"], 1000, list(ids.values()))).json()
    upd = equal_expense(ids["Aさん"], 1200, list(ids.values()))
    r1 = a.put(f"/groups/{pid}/expenses/{e['id']}", json=upd, headers={"If-Match": str(e["version"])})
    r2 = b.put(f"/groups/{pid}/expenses/{e['id']}", json=upd, headers={"If-Match": str(e["version"])})
    assert r1.status_code == 200 and r1.json()["version"] == 2
    assert r2.status_code == 409 and r2.json()["code"] == "REVISION_CONFLICT"
    assert a.put(f"/groups/{pid}/expenses/{e['id']}", json=upd).status_code == 428
    revs = a.get(f"/groups/{pid}/expenses/{e['id']}/revisions").json()
    assert [r["change_kind"] for r in revs] == ["create", "update"]
    assert revs[0]["spec"]["amount_minor"] == 1000 and revs[1]["spec"]["amount_minor"] == 1200
    d = a.delete(f"/groups/{pid}/expenses/{e['id']}", headers={"If-Match": "2"})
    assert d.status_code == 200 and d.json()["state"] == "deleted"
    assert a.get(f"/groups/{pid}/balances").json()["balances"][0]["net_minor"] == 0


def test_settlement_batch_double_confirm_and_stale_version(user_factory):
    a = user_factory()
    g, ids = make_group(a)
    pid = g["public_id"]
    a.post(f"/groups/{pid}/expenses", json=equal_expense(ids["Aさん"], 4000, list(ids.values())))
    v = a.get(f"/groups/{pid}").json()["ledger_version"]
    r1 = a.post(f"/groups/{pid}/settlement-batches", json={"expected_ledger_version": v})
    assert r1.status_code == 201
    r2 = a.post(f"/groups/{pid}/settlement-batches", json={"expected_ledger_version": v})
    assert r2.status_code == 409 and r2.json()["code"] == "SETTLEMENT_ALREADY_CONFIRMED"
    r3 = a.post(f"/groups/{pid}/settlement-batches", json={"expected_ledger_version": v - 1})
    assert r3.status_code == 409 and r3.json()["code"] == "REVISION_CONFLICT"


def test_concurrent_batch_confirm_only_one_wins(user_factory):
    a = user_factory()
    g, ids = make_group(a)
    pid = g["public_id"]
    a.post(f"/groups/{pid}/expenses", json=equal_expense(ids["Aさん"], 4000, list(ids.values())))
    v = a.get(f"/groups/{pid}").json()["ledger_version"]
    results = []

    def go():
        results.append(a.post(f"/groups/{pid}/settlement-batches", json={"expected_ledger_version": v}))

    threads = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    codes = sorted(r.status_code for r in results)
    assert codes.count(201) == 1 and codes.count(409) == 5, codes
    assert len(a.get(f"/groups/{pid}/settlement-batches").json()) == 1


def test_reoptimization_preserves_locked_transfers(user_factory):
    a, b = user_factory(), user_factory()
    g, ids = make_group(a)
    pid = g["public_id"]
    join_as(b, a, pid, "Bさん")
    all_ids = list(ids.values())
    a.post(f"/groups/{pid}/expenses", json=equal_expense(ids["Aさん"], 4000, all_ids))
    v = a.get(f"/groups/{pid}").json()["ledger_version"]
    batch1 = a.post(f"/groups/{pid}/settlement-batches", json={"expected_ledger_version": v}).json()
    t_b = next(t for t in batch1["transfers"] if t["from_member_id"] == ids["Bさん"])
    assert b.post(f"/settlements/{t_b['id']}/report-paid").json()["status"] == "reported_paid"

    # 追加の支出 → 再確定
    b.post(f"/groups/{pid}/expenses", json=equal_expense(ids["Bさん"], 2000, all_ids))
    v2 = a.get(f"/groups/{pid}").json()["ledger_version"]
    batch2 = a.post(f"/groups/{pid}/settlement-batches", json={"expected_ledger_version": v2})
    assert batch2.status_code == 201
    hist = a.get(f"/groups/{pid}/settlement-batches").json()
    old = next(h for h in hist if h["version"] == 1)
    statuses = {t["id"]: t["status"] for t in old["transfers"]}
    assert statuses[t_b["id"]] == "reported_paid"  # ロックされた送金は変更されない
    assert all(s in ("superseded", "reported_paid") for s in statuses.values())
    # 置換済みの送金には申告できない
    other = next((t for t in old["transfers"] if t["id"] != t_b["id"]), None)
    if other and other["from_member_id"] == ids["Aさん"]:
        assert a.post(f"/settlements/{other['id']}/report-paid").status_code == 409
    # ロック済み送金 + 新しい計画で全員の残高が0
    rest = dict(batch2.json()["balances"])
    for t in batch2.json()["transfers"]:
        rest[t["from_member_id"]] += t["amount_minor"]
        rest[t["to_member_id"]] -= t["amount_minor"]
    assert all(x == 0 for x in rest.values())


def test_invite_claim_race_and_expiry(user_factory):
    a = user_factory()
    g, ids = make_group(a)
    pid = g["public_id"]
    inv = a.post(f"/groups/{pid}/invites", json={"max_uses": 10}).json()
    users = [user_factory() for _ in range(5)]
    results = []

    def go(u):
        results.append((u, u.post(f"/invites/{inv['token']}/join", json={"claim_member_id": ids["Cさん"]})))

    threads = [threading.Thread(target=go, args=(u,)) for u in users]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(r.status_code for _, r in results) == [200, 409, 409, 409, 409]
    assert users[0].get("/invites/not-a-real-token").status_code == 404
    # 招待URL発行はオーナーのみ
    joined = next(u for u, r in results if r.status_code == 200)
    assert joined.post(f"/groups/{pid}/invites", json={}).status_code == 403
