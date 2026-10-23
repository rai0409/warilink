"""Idempotency-Key による二重実行防止。

同一 identity + key の 2 回目以降は、保存済みのレスポンスを返す。内容(hash)が異なれば 409。
同時に同じキーで来た場合、後続は INSERT の一意制約で先行トランザクションの確定を待ち、
確定後のレスポンスを返す(READ COMMITTED)。エラー時は行ごとロールバックされ再試行可能。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.tables import IdempotencyKey, Identity

from .errors import ApiError


def require_key(request: Request) -> str:
    key = request.headers.get("idempotency-key")
    if not key or not (8 <= len(key) <= 128):
        raise ApiError(400, "IDEMPOTENCY_KEY_REQUIRED", "リクエストの識別キーがありません。再読み込みしてください")
    return key


def run_idempotent(
    db: Session, request: Request, ident: Identity, scope: str, payload: object,
    fn: Callable[[], object], status_code: int = 200,
) -> JSONResponse:
    key = require_key(request)
    req_hash = hashlib.sha256(
        json.dumps({"scope": scope, "payload": jsonable_encoder(payload)}, sort_keys=True).encode()
    ).hexdigest()
    inserted = db.execute(
        insert(IdempotencyKey)
        .values(identity_id=ident.id, key=key, scope=scope, request_hash=req_hash)
        .on_conflict_do_nothing()
        .returning(IdempotencyKey.key)
    ).first()
    if inserted is None:
        row = db.execute(
            select(IdempotencyKey).where(IdempotencyKey.identity_id == ident.id, IdempotencyKey.key == key)
        ).scalar_one()
        if row.request_hash != req_hash:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "同じ識別キーで異なる内容が送信されました")
        return JSONResponse(row.response, status_code=row.status_code or 200,
                            headers={"Idempotent-Replayed": "true", "Cache-Control": "no-store"})
    body = jsonable_encoder(fn())
    db.execute(
        update(IdempotencyKey)
        .where(IdempotencyKey.identity_id == ident.id, IdempotencyKey.key == key)
        .values(status_code=status_code, response=body)
    )
    db.commit()
    return JSONResponse(body, status_code=status_code, headers={"Cache-Control": "no-store"})
