#!/usr/bin/env python3
"""バックアップ訓練(warilink_backup.py drill)用。使い捨て DB に API 経由で合成データを入れる / 復元後に読み戻す。

WARILINK_DATABASE_URL と WARILINK_RECEIPT_STORAGE_DIR は呼び出し側が使い捨ての DB・ディレクトリを指定する。
実在の個人・店舗・口座のデータは使わない。
"""

from __future__ import annotations

import io
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import get_engine
from app.main import app
from app.models.tables import Group, Identity, Membership, Receipt
from app.ocr import jobs

H = {"X-WariLink-CSRF": "1"}


def client() -> TestClient:
    c = TestClient(app, headers=H)
    assert c.post("/api/v1/identity/guest").status_code == 201
    return c


def seed() -> dict:
    with Session(get_engine()) as db:
        jobs.heartbeat(db, "drill-worker", "none", "0", datetime.now(UTC))
    c = client()
    g = c.post("/api/v1/groups", json={"title": "訓練", "my_name": "Aさん", "other_member_names": ["Bさん", "Cさん"]},
               headers={"Idempotency-Key": str(uuid.uuid4())})
    assert g.status_code == 201, g.text
    g = g.json()
    ids = {m["display_name"]: m["id"] for m in g["members"]}
    body = {"title": "合成の支出", "amount_minor": 10001,
            "payers": [{"member_id": ids["Aさん"], "amount_minor": 10001}],
            "split": {"mode": "equal", "shares": [{"member_id": m, "value": 1} for m in ids.values()]}}
    assert c.post(f"/api/v1/groups/{g['public_id']}/expenses", json=body,
                  headers={"Idempotency-Key": str(uuid.uuid4())}).status_code == 201
    buf = io.BytesIO()
    Image.new("RGB", (400, 600), (240, 240, 240)).save(buf, format="PNG")
    r = c.post(f"/api/v1/groups/{g['public_id']}/receipts", files={"file": ("r.png", buf.getvalue(), "image/png")},
               headers={"Idempotency-Key": str(uuid.uuid4())})
    assert r.status_code == 201, r.text
    # 2 人目(Bさん)が招待から参加する。訓練ではこの人がバックアップの後に退会する
    inv = c.post(f"/api/v1/groups/{g['public_id']}/invites", json={}, headers={"Idempotency-Key": str(uuid.uuid4())})
    assert inv.status_code == 201, inv.text
    token = inv.json()["token"]
    member = client()
    claim = next(m["id"] for m in member.get(f"/api/v1/invites/{token}").json()["unclaimed_members"]
                 if m["display_name"] == "Bさん")
    j = member.post(f"/api/v1/invites/{token}/join", json={"claim_member_id": claim},
                    headers={"Idempotency-Key": str(uuid.uuid4())})
    assert j.status_code == 200, j.text
    state = os.environ.get("WARILINK_DRILL_STATE")
    if state:
        # 使い捨ての訓練 DB のセッションだけ。作業ディレクトリ(訓練の後に削除)に権限 600 で置く
        p = Path(state)
        p.write_text(json.dumps({"cookies": dict(member.cookies)}))
        p.chmod(0o600)
    return {"seeded_group": g["public_id"]}


def delete_member() -> int:
    """seed で参加した Bさん が退会する(API 経由。削除済み一覧に identity が載る)。"""
    cookies = json.loads(Path(os.environ["WARILINK_DRILL_STATE"]).read_text())["cookies"]
    c = TestClient(app, headers=H, cookies=cookies)
    r = c.request("DELETE", "/api/v1/account", json={"confirm": True})
    print(json.dumps({"deleted_member": r.status_code == 200}))
    return 0 if r.status_code == 200 else 1


def verify() -> int:
    with Session(get_engine()) as db:
        group = db.execute(select(Group)).scalar_one()
        receipts = db.execute(select(func.count()).select_from(Receipt)).scalar_one()
        # 復元した DB をアプリの計算で読み戻す(残高の和が 0、支出の合計が一致)
        from app.modules.settlement import service

        ledger = service.load_ledger(db, group)
        net, rows = service.balance_rows(ledger)
        # 退会した Bさん: 名前だけのメンバー行が残り、本人との結び付き・identity は無効のまま(復元で戻っていない)
        member = db.execute(select(Membership).where(Membership.display_name == "Bさん")).scalar_one()
        disabled = db.execute(select(func.count()).select_from(Identity)
                              .where(Identity.disabled_at.is_not(None))).scalar_one()
    total = sum(r["paid_minor"] for r in rows)
    deletion_held = member.identity_id is None and disabled == 1
    ok = sum(net.values()) == 0 and total == 10001 and receipts == 1 and deletion_held
    print(json.dumps({"verify_app_readback": ok, "balance_sum": sum(net.values()), "paid_total": total,
                      "receipts": receipts, "deletion_held_after_restore": deletion_held}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    if "--verify" in sys.argv:
        sys.exit(verify())
    if "--delete-member" in sys.argv:
        sys.exit(delete_member())
    print(json.dumps(seed(), ensure_ascii=False))
