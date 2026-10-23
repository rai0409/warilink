#!/usr/bin/env python3
"""バックアップ訓練(warilink_backup.py drill)用。使い捨て DB に API 経由で合成データを入れる / 復元後に読み戻す。

WARILINK_DATABASE_URL と WARILINK_RECEIPT_STORAGE_DIR は呼び出し側が使い捨ての DB・ディレクトリを指定する。
実在の個人・店舗・口座のデータは使わない。
"""

from __future__ import annotations

import io
import json
import sys
import uuid
from datetime import UTC, datetime

from app.core.db import get_engine
from app.main import app
from app.models.tables import Group, Receipt
from app.ocr import jobs
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.orm import Session

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
    return {"seeded_group": g["public_id"]}


def verify() -> int:
    with Session(get_engine()) as db:
        group = db.execute(select(Group)).scalar_one()
        receipts = db.execute(select(func.count()).select_from(Receipt)).scalar_one()
        # 復元した DB をアプリの計算で読み戻す(残高の和が 0、支出の合計が一致)
        from app.modules.settlement import service

        ledger = service.load_ledger(db, group)
        net, rows = service.balance_rows(ledger)
    total = sum(r["paid_minor"] for r in rows)
    ok = sum(net.values()) == 0 and total == 10001 and receipts == 1
    print(json.dumps({"verify_app_readback": ok, "balance_sum": sum(net.values()), "paid_total": total,
                      "receipts": receipts}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    if "--verify" in sys.argv:
        sys.exit(verify())
    print(json.dumps(seed(), ensure_ascii=False))
