"""新しい画面のための API: 自分のグループ一覧・招待の状態・表示名の変更・「まだ受け取っていない」。"""

from .conftest import equal_expense, join_as, make_group


def test_my_groups_lists_only_groups_i_belong_to(user_factory):
    a, b, outsider = user_factory(), user_factory(), user_factory()
    g1, _ = make_group(a, title="週末の京都旅行")
    g2, _ = make_group(b, names=("Cさん",), title="金曜のごはん")
    join_as(a, b, g2["public_id"], "Cさん")
    mine = a.get("/groups").json()
    assert [g["title"] for g in mine] == ["金曜のごはん", "週末の京都旅行"]  # 新しい順
    assert {g["title"]: (g["member_count"], g["is_owner"]) for g in mine} == {
        "週末の京都旅行": (4, True), "金曜のごはん": (2, False)}
    assert outsider.get("/groups").json() == []
    assert a.post(f"/groups/{g1['public_id']}/leave").status_code == 204
    assert [g["title"] for g in a.get("/groups").json()] == ["金曜のごはん"]  # 抜けたグループは出ない


def test_invite_status_without_revealing_tokens(user_factory):
    owner, member = user_factory(), user_factory()
    g, _ = make_group(owner)
    pid = g["public_id"]
    assert owner.get(f"/groups/{pid}/invites/status").json() == {"active": False, "expires_at": None}
    owner.post(f"/groups/{pid}/invites", json={})
    join_as(member, owner, pid, "Bさん")
    st = member.get(f"/groups/{pid}/invites/status").json()
    assert st["active"] is True and st["expires_at"] and "token" not in st
    owner.post(f"/groups/{pid}/invites/revoke")
    assert owner.get(f"/groups/{pid}/invites/status").json()["active"] is False
    assert user_factory().get(f"/groups/{pid}/invites/status").status_code == 404


def test_rename_own_name_and_owner_renames_name_only_members(user_factory):
    owner, member = user_factory(), user_factory()
    g, ids = make_group(owner)
    pid = g["public_id"]
    join_as(member, owner, pid, "Bさん")
    r = member.patch(f"/groups/{pid}/members/{ids['Bさん']}", json={"display_name": "ゆい"})
    assert r.status_code == 200
    assert member.patch(f"/groups/{pid}/members/{ids['Cさん']}", json={"display_name": "x"}).status_code == 403
    assert owner.patch(f"/groups/{pid}/members/{ids['Bさん']}", json={"display_name": "x"}).status_code == 403
    assert owner.patch(f"/groups/{pid}/members/{ids['Cさん']}", json={"display_name": "けん"}).status_code == 200
    assert owner.patch(f"/groups/{pid}/members/{ids['Cさん']}", json={"display_name": " "}).status_code == 422
    names = {m["id"]: m["display_name"] for m in owner.get(f"/groups/{pid}").json()["members"]}
    assert names[ids["Bさん"]] == "ゆい" and names[ids["Cさん"]] == "けん"
    assert user_factory().patch(f"/groups/{pid}/members/{ids['Dさん']}", json={"display_name": "x"}).status_code == 404


def test_recipient_can_mark_reported_transfer_as_not_received(user_factory):
    a, b = user_factory(), user_factory()
    g, ids = make_group(a, names=("Bさん",))
    pid = g["public_id"]
    join_as(b, a, pid, "Bさん")
    assert a.post(f"/groups/{pid}/expenses", json=equal_expense(ids["Aさん"], 2000, [ids["Aさん"], ids["Bさん"]])
                  ).status_code == 201
    v = a.get(f"/groups/{pid}").json()["ledger_version"]
    (t,) = a.post(f"/groups/{pid}/settlement-batches", json={"expected_ledger_version": v}).json()["transfers"]
    assert t["from_member_id"] == ids["Bさん"]
    assert b.post(f"/settlements/{t['id']}/not-received").status_code == 403  # 支払う側はできない
    assert a.post(f"/settlements/{t['id']}/not-received").json()["status"] == "unpaid"  # 未申告なら何もしない
    assert b.post(f"/settlements/{t['id']}/report-paid").json()["status"] == "reported_paid"
    r = a.post(f"/settlements/{t['id']}/not-received")
    assert r.status_code == 200 and r.json()["status"] == "unpaid"
    assert b.post(f"/settlements/{t['id']}/report-paid").json()["status"] == "reported_paid"  # もう一度申告できる
    assert a.post(f"/settlements/{t['id']}/confirm-received").json()["status"] == "recipient_confirmed"
    r = a.post(f"/settlements/{t['id']}/not-received")
    assert r.status_code == 409 and r.json()["code"] == "TRANSFER_NOT_REPORTED"


def test_revisions_include_the_changers_display_name(user_factory):
    a, b = user_factory(), user_factory()
    g, ids = make_group(a, names=("Bさん",))
    pid = g["public_id"]
    join_as(b, a, pid, "Bさん")
    e = a.post(f"/groups/{pid}/expenses", json=equal_expense(ids["Aさん"], 2000, [ids["Aさん"], ids["Bさん"]])).json()
    revs = b.get(f"/groups/{pid}/expenses/{e['id']}/revisions").json()
    assert revs[0]["changed_by_name"] == "Aさん" and revs[0]["changed_by_me"] is False
