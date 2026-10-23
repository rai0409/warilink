"""支出のカテゴリ・検索・集計(P09)。グループの外には何も見せない。"""

import base64
import json

from .conftest import equal_expense, join_as, make_group


def add(u, pid, payer, amount, members, title, category=None, occurred_on=None):
    body = equal_expense(payer, amount, members, title=title)
    if category:
        body["category"] = category
    if occurred_on:
        body["occurred_on"] = occurred_on
    r = u.post(f"/groups/{pid}/expenses", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_category_is_optional_and_validated(user_factory):
    a = user_factory()
    g, ids = make_group(a)
    pid = g["public_id"]
    m = list(ids.values())
    plain = add(a, pid, ids["Aさん"], 1000, m, "未分類の支出")
    assert plain["category"] is None
    food = add(a, pid, ids["Aさん"], 1001, m, "ランチ", category="food")
    assert food["category"] == "food" and sum(food["shares"].values()) == 1001  # 金額・端数は従来どおり
    bad = equal_expense(ids["Aさん"], 100, m) | {"category": "casino"}
    assert a.post(f"/groups/{pid}/expenses", json=bad).status_code == 422
    # 訂正でカテゴリを変えられる(新しい版)
    body = equal_expense(ids["Aさん"], 1000, m, title="未分類の支出") | {"category": "daily"}
    r = a.put(f"/groups/{pid}/expenses/{plain['id']}", json=body, headers={"If-Match": str(plain["version"])})
    assert r.status_code == 200 and r.json()["category"] == "daily"
    # 既存の一覧 API の形は変わらない(category が増えただけ)
    assert {e["category"] for e in a.get(f"/groups/{pid}/expenses").json()} == {"daily", "food"}


def test_search_filters_sort_and_pagination(user_factory):
    a, b = user_factory(), user_factory()
    g, ids = make_group(a)
    pid = g["public_id"]
    join_as(b, a, pid, "Bさん")
    m = list(ids.values())
    add(a, pid, ids["Aさん"], 3000, m, "居酒屋 100%生", "drinks", "2026-09-01")
    add(a, pid, ids["Aさん"], 1200, m, "タクシー", "transport", "2026-09-15")
    add(b, pid, ids["Bさん"], 8000, m, "ホテル", "lodging", "2026-10-01")
    add(b, pid, ids["Bさん"], 500, m, "コンビニ", None, "2026-10-02")
    gone = add(a, pid, ids["Aさん"], 999, m, "取り消した支出", "food", "2026-10-03")
    a.delete(f"/groups/{pid}/expenses/{gone['id']}", headers={"If-Match": str(gone["version"])})

    def s(**params):
        r = a.get(f"/groups/{pid}/expenses/search", params=params)
        assert r.status_code == 200, r.text
        return r.json()

    assert s()["total_count"] == 4  # 削除済みは含めない
    assert [e["title"] for e in s(q="100%")["items"]] == ["居酒屋 100%生"]  # % はそのままの文字として扱う
    assert s(q="_")["total_count"] == 0
    assert [e["title"] for e in s(category="lodging")["items"]] == ["ホテル"]
    assert [e["title"] for e in s(uncategorized=True)["items"]] == ["コンビニ"]
    r = s(date_from="2026-09-10", date_to="2026-10-01")
    assert [e["title"] for e in r["items"]] == ["ホテル", "タクシー"] and r["total_amount_minor"] == 9200
    assert {e["title"] for e in s(payer_member_id=ids["Bさん"])["items"]} == {"ホテル", "コンビニ"}
    assert {e["title"] for e in s(created_by_me=True)["items"]} == {"居酒屋 100%生", "タクシー"}
    assert [e["amount_minor"] for e in s(sort="amount_desc")["items"]] == [8000, 3000, 1200, 500]
    assert [e["title"] for e in s(sort="date_asc")["items"]][0] == "居酒屋 100%生"

    # ページ分割: 重複・欠落なし
    seen, cursor = [], None
    while True:
        page = s(limit=3, **({"cursor": cursor} if cursor else {}))
        seen += [e["id"] for e in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 4


def test_search_rejects_bad_paging_and_hides_other_groups(user_factory):
    a, outsider = user_factory(), user_factory()
    g, ids = make_group(a)
    pid = g["public_id"]
    add(a, pid, ids["Aさん"], 1000, list(ids.values()), "秘密の支出")
    g2, ids2 = make_group(outsider)
    add(outsider, g2["public_id"], ids2["Aさん"], 1000, list(ids2.values()), "別グループ")
    for bad in ("not-base64!!", base64.urlsafe_b64encode(json.dumps({"o": -1}).encode()).decode(),
                base64.urlsafe_b64encode(json.dumps({"o": 10**9}).encode()).decode()):
        r = a.get(f"/groups/{pid}/expenses/search", params={"cursor": bad})
        assert r.status_code == 422 and r.json()["code"] == "INVALID_CURSOR"
    assert a.get(f"/groups/{pid}/expenses/search", params={"limit": 101}).status_code == 422
    assert outsider.get(f"/groups/{pid}/expenses/search").status_code == 404
    assert outsider.get(f"/groups/{pid}/expenses/stats").status_code == 404
    # 他のグループのメンバー ID で絞っても、他のグループの支出は出てこない
    r = outsider.get(f"/groups/{g2['public_id']}/expenses/search", params={"payer_member_id": ids["Aさん"]}).json()
    assert r["total_count"] == 0
    assert [e["title"] for e in a.get(f"/groups/{pid}/expenses/search", params={"q": "支出"}).json()["items"]] == [
        "秘密の支出"]


def test_many_expenses_paginate(user_factory):
    a = user_factory()
    g, ids = make_group(a, names=())
    pid = g["public_id"]
    for i in range(105):
        add(a, pid, ids["Aさん"], 100 + i, [ids["Aさん"]], f"支出{i:03d}")
    first = a.get(f"/groups/{pid}/expenses/search", params={"limit": 100}).json()
    assert len(first["items"]) == 100 and first["total_count"] == 105 and first["next_cursor"]
    rest = a.get(f"/groups/{pid}/expenses/search", params={"limit": 100, "cursor": first["next_cursor"]}).json()
    assert len(rest["items"]) == 5 and rest["next_cursor"] is None


def test_stats_separate_paid_share_and_unconfirmed(user_factory):
    a, b = user_factory(), user_factory()
    g, ids = make_group(a, names=("Bさん", "Cさん"))
    pid = g["public_id"]
    join_as(b, a, pid, "Bさん")
    empty = a.get(f"/groups/{pid}/expenses/stats").json()
    assert empty["expense_count"] == 0 and empty["by_category"] == [] and empty["total_minor"] == 0
    m = list(ids.values())
    add(a, pid, ids["Aさん"], 10001, m, "宿", "lodging", "2026-09-30")  # 3人で割る: 端数 1円
    add(b, pid, ids["Bさん"], 3000, m, "食材", None, "2026-10-05")
    st = a.get(f"/groups/{pid}/expenses/stats").json()
    assert st["expense_count"] == 2 and st["total_minor"] == 13001
    assert {(c["category"], c["total_minor"]) for c in st["by_category"]} == {("lodging", 10001), (None, 3000)}
    assert [(x["month"], x["total_minor"]) for x in st["by_month"]] == [("2026-09", 10001), ("2026-10", 3000)]
    by = {x["member_id"]: x for x in st["by_member"]}
    assert by[ids["Aさん"]]["paid_minor"] == 10001 and by[ids["Bさん"]]["paid_minor"] == 3000
    assert sum(x["paid_minor"] for x in by.values()) == sum(x["share_minor"] for x in by.values()) == 13001
    assert by[ids["Cさん"]]["paid_minor"] == 0 and by[ids["Cさん"]]["share_minor"] in (4333, 4334)
    assert st["unconfirmed_transfer_count"] == 0

    v = a.get(f"/groups/{pid}").json()["ledger_version"]
    batch = a.post(f"/groups/{pid}/settlement-batches", json={"expected_ledger_version": v}).json()
    t_b = next(t for t in batch["transfers"] if t["from_member_id"] == ids["Bさん"])
    b.post(f"/settlements/{t_b['id']}/report-paid")
    st = a.get(f"/groups/{pid}/expenses/stats").json()
    assert st["unconfirmed_transfer_count"] == 1 and st["unconfirmed_transfer_minor"] == t_b["amount_minor"]
    sep = a.get(f"/groups/{pid}/expenses/stats", params={"date_from": "2026-10-01"}).json()
    assert sep["total_minor"] == 3000 and len(sep["notes"]) == 3
