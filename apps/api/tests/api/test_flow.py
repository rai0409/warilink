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


def test_reported_and_confirmed_are_distinct(user_factory):
    """送金申告(支払者の自己申告)と受取確認(受取人の手動確認)を API 上で取り違えない。"""
    a, b, c = user_factory(), user_factory(), user_factory()
    g, ids = make_group(a)
    pid = g["public_id"]
    join_as(b, a, pid, "Bさん")
    join_as(c, a, pid, "Cさん")
    # A が 3000 円を A・B・C で割る → B→A 1000, C→A 1000
    abc = [ids["Aさん"], ids["Bさん"], ids["Cさん"]]
    a.post(f"/groups/{pid}/expenses", json=equal_expense(ids["Aさん"], 3000, abc))
    v = a.get(f"/groups/{pid}").json()["ledger_version"]
    batch = a.post(f"/groups/{pid}/settlement-batches", json={"expected_ledger_version": v}).json()
    assert batch["progress"] == {"unpaid_count": 2, "unpaid_minor": 2000, "reported_unconfirmed_count": 0,
                                 "reported_unconfirmed_minor": 0, "recipient_confirmed_count": 0,
                                 "recipient_confirmed_minor": 0}
    t_b = next(t for t in batch["transfers"] if t["from_member_id"] == ids["Bさん"])
    t_c = next(t for t in batch["transfers"] if t["from_member_id"] == ids["Cさん"])
    assert t_b["to_member_id"] == ids["Aさん"] and not t_b["is_recipient_confirmed"]

    # 受取人は申告できない / 支払者は受取確認できない / 他の当事者でない人は存在も見えない
    assert a.post(f"/settlements/{t_b['id']}/report-paid").status_code == 403
    assert c.post(f"/settlements/{t_b['id']}/report-paid").status_code == 404
    assert c.post(f"/settlements/{t_b['id']}/confirm-received").status_code == 404
    assert a.post(f"/settlements/{uuid.uuid4()}/confirm-received").status_code == 404

    r = b.post(f"/settlements/{t_b['id']}/report-paid").json()
    assert r["status"] == "reported_paid" and r["is_recipient_confirmed"] is False
    assert "未確認" in r["status_label"]
    assert b.post(f"/settlements/{t_b['id']}/confirm-received").status_code == 403

    # 残高: 申告済みは再計算上は支払い済み(ロック)だが、未確認額として別に示す
    rows = {r["member_id"]: r for r in a.get(f"/groups/{pid}/balances").json()["balances"]}
    assert rows[ids["Bさん"]]["settled_out_minor"] == 1000
    assert rows[ids["Bさん"]]["reported_unconfirmed_out_minor"] == 1000
    assert rows[ids["Aさん"]]["reported_unconfirmed_in_minor"] == 1000
    assert sum(r["net_minor"] for r in rows.values()) == 0
    prev = a.post(f"/groups/{pid}/settlements/preview").json()
    assert prev["locked_progress"]["reported_unconfirmed_count"] == 1
    assert prev["locked_progress"]["recipient_confirmed_count"] == 0
    cur = a.get(f"/groups/{pid}/settlement-batches/current").json()
    assert cur["progress"]["reported_unconfirmed_minor"] == 1000 and cur["progress"]["unpaid_minor"] == 1000

    # 受取確認後: 未確認額は 0、確認済みとして数える。申告し直しても確認済みから戻らない
    r = a.post(f"/settlements/{t_b['id']}/confirm-received").json()
    assert r["status"] == "recipient_confirmed" and r["is_recipient_confirmed"] is True
    assert b.post(f"/settlements/{t_b['id']}/report-paid").json()["status"] == "recipient_confirmed"
    assert a.post(f"/settlements/{t_b['id']}/confirm-received").json()["version"] == r["version"]  # 冪等
    rows = {r["member_id"]: r for r in a.get(f"/groups/{pid}/balances").json()["balances"]}
    assert rows[ids["Bさん"]]["reported_unconfirmed_out_minor"] == 0
    assert rows[ids["Bさん"]]["settled_out_minor"] == 1000
    cur = a.get(f"/groups/{pid}/settlement-batches/current").json()
    assert cur["progress"]["recipient_confirmed_count"] == 1 and cur["progress"]["reported_unconfirmed_count"] == 0

    # 受取人は申告を待たずに受け取りを確認できる(申告は必須ではない)
    r = a.post(f"/settlements/{t_c['id']}/confirm-received").json()
    assert r["status"] == "recipient_confirmed"
    assert c.post(f"/settlements/{t_c['id']}/report-paid").json()["status"] == "recipient_confirmed"


def test_permission_matrix_owner_member_outsider_anonymous(user_factory):
    """権限表(docs/API_CONTRACT.md「権限表」)を主体ごとに確かめる。非メンバーは 404、未認証は 401。"""
    from fastapi.testclient import TestClient

    from app.main import app

    owner, member, outsider = user_factory(), user_factory(), user_factory()
    anon = TestClient(app, headers={"X-WariLink-CSRF": "1"})
    g, ids = make_group(owner)
    pid = g["public_id"]
    join_as(member, owner, pid, "Bさん")
    exp = owner.post(f"/groups/{pid}/expenses", json=equal_expense(ids["Aさん"], 1200, list(ids.values()))).json()
    eid = exp["id"]
    g2, ids2 = make_group(outsider)

    reads = [f"/groups/{pid}", f"/groups/{pid}/members", f"/groups/{pid}/expenses", f"/groups/{pid}/expenses/{eid}",
             f"/groups/{pid}/expenses/{eid}/revisions", f"/groups/{pid}/balances",
             f"/groups/{pid}/settlement-batches", f"/groups/{pid}/settlement-batches/current"]
    for path in reads:
        assert owner.get(path).status_code == 200, path
        assert member.get(path).status_code == 200, path
        assert outsider.get(path).status_code == 404, path
        assert anon.get(f"/api/v1{path}").status_code == 401, path

    # 書き込み: 非メンバーは存在を知らされない(404)。メンバーの追加・支出・精算はメンバー全員に許可
    writes = [(f"/groups/{pid}/members", {"names": ["Eさん"]}),
              (f"/groups/{pid}/expenses", equal_expense(ids["Aさん"], 300, [ids["Aさん"]])),
              (f"/groups/{pid}/settlements/preview", None),
              (f"/groups/{pid}/invites", {})]
    for path, body in writes:
        assert outsider.post(path, json=body).status_code == 404, path
        assert anon.post(f"/api/v1{path}", json=body).status_code == 401, path
    assert member.post(f"/groups/{pid}/members", json={"names": ["Eさん"]}).status_code == 201
    r = member.post(f"/groups/{pid}/expenses", json=equal_expense(ids["Bさん"], 300, [ids["Bさん"]]))
    assert r.status_code == 201
    # 招待URLの発行はオーナーだけ(メンバーは 403、非メンバーは 404)
    assert member.post(f"/groups/{pid}/invites", json={}).status_code == 403
    assert owner.post(f"/groups/{pid}/invites", json={}).status_code == 201

    # IDOR: 自分のグループの URL に他グループの支出 ID を入れても見えない・変えられない
    assert outsider.get(f"/groups/{g2['public_id']}/expenses/{eid}").status_code == 404
    assert outsider.delete(f"/groups/{g2['public_id']}/expenses/{eid}",
                           headers={"If-Match": str(exp["version"])}).status_code == 404
    assert outsider.put(f"/groups/{g2['public_id']}/expenses/{eid}", headers={"If-Match": str(exp["version"])},
                        json=equal_expense(ids2["Aさん"], 1, [ids2["Aさん"]])).status_code == 404
    assert owner.get(f"/groups/{pid}/expenses/{eid}").json()["amount_minor"] == 1200
    # 他グループの名前行を招待で横取りできない
    inv2 = outsider.post(f"/groups/{g2['public_id']}/invites", json={}).json()
    r = member.post(f"/invites/{inv2['token']}/join", json={"claim_member_id": ids["Cさん"]})
    assert r.status_code == 409


def test_invite_expired_exhausted_and_revoked_are_rejected(user_factory):
    from sqlalchemy import update as sa_update

    from app.core.db import get_engine
    from app.core.security import hash_token
    from app.models.tables import Invitation

    a = user_factory()
    g, ids = make_group(a)
    pid = g["public_id"]
    one = a.post(f"/groups/{pid}/invites", json={"max_uses": 1}).json()
    u1, u2 = user_factory(), user_factory()
    assert u1.post(f"/invites/{one['token']}/join", json={"claim_member_id": ids["Bさん"]}).status_code == 200
    # 同じ人の再参加は冪等(回数を消費しない)、別の人は回数切れで 404
    assert u1.post(f"/invites/{one['token']}/join", json={"claim_member_id": ids["Bさん"]}).status_code == 200
    r = u2.post(f"/invites/{one['token']}/join", json={"claim_member_id": ids["Cさん"]})
    assert r.status_code == 404 and r.json()["code"] == "INVITE_INVALID"
    assert u2.get(f"/invites/{one['token']}").status_code == 404

    expired = a.post(f"/groups/{pid}/invites", json={}).json()
    revoked = a.post(f"/groups/{pid}/invites", json={}).json()
    with get_engine().begin() as conn:
        conn.execute(sa_update(Invitation).where(Invitation.token_hash == hash_token(expired["token"]))
                     .values(expires_at=func_now_minus_minute()))
        conn.execute(sa_update(Invitation).where(Invitation.token_hash == hash_token(revoked["token"]))
                     .values(revoked_at=func_now_minus_minute()))
    for tok in (expired["token"], revoked["token"]):
        assert u2.get(f"/invites/{tok}").status_code == 404
        assert u2.post(f"/invites/{tok}/join", json={"claim_member_id": ids["Cさん"]}).status_code == 404
    assert u2.get(f"/groups/{pid}").status_code == 404
    # 発行の上限値を超える指定は 422
    assert a.post(f"/groups/{pid}/invites", json={"max_uses": 501}).status_code == 422
    assert a.post(f"/groups/{pid}/invites", json={"ttl_hours": 24 * 31}).status_code == 422


def func_now_minus_minute():
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) - timedelta(minutes=1)
