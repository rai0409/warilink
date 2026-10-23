"""ゲストセッション・CSRF・アクセス制御の共通処理。

- セッショントークンは 256bit 乱数。DB には SHA-256 ハッシュのみ保存する。
- Cookie は httpOnly / SameSite=Lax / (本番) Secure。
- 状態変更リクエストには独自ヘッダー `X-WariLink-CSRF` を必須にする(クロスサイトの
  単純リクエストでは付与できないため、CORS 許可オリジン以外からの CSRF を防ぐ)。
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.tables import Group, Identity, Membership, SessionRow

from .config import get_settings
from .db import get_db
from .errors import ApiError, not_found

CSRF_HEADER = "x-warilink-csrf"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def now() -> datetime:
    return datetime.now(UTC)


def create_guest_session(db: Session, response: Response) -> Identity:
    s = get_settings()
    ident = Identity(kind="guest")
    db.add(ident)
    db.flush()
    token = new_token()
    db.add(SessionRow(token_hash=hash_token(token), identity_id=ident.id, assurance_level="guest",
                      expires_at=now() + timedelta(days=s.session_ttl_days)))
    response.set_cookie(
        s.session_cookie_name, token, max_age=s.session_ttl_days * 86400, httponly=True,
        secure=s.cookie_secure, samesite="lax", path="/",
    )
    return ident


def check_csrf(request: Request) -> None:
    if request.method in UNSAFE_METHODS and request.headers.get(CSRF_HEADER) != "1":
        raise ApiError(403, "CSRF_REJECTED", "不正なリクエストのため拒否しました。ページを再読み込みしてください")
    origin = request.headers.get("origin")
    if request.method in UNSAFE_METHODS and origin and origin not in get_settings().allowed_origins:
        raise ApiError(403, "CSRF_REJECTED", "許可されていない送信元からのリクエストです")


def optional_identity(request: Request, db: Session = Depends(get_db)) -> Identity | None:
    token = request.cookies.get(get_settings().session_cookie_name)
    if not token:
        return None
    row = db.execute(
        select(SessionRow).where(SessionRow.token_hash == hash_token(token))
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None or row.expires_at <= now():
        return None
    ident = db.get(Identity, row.identity_id)
    if ident is None or ident.disabled_at is not None:
        return None
    return ident


def current_identity(request: Request, ident: Identity | None = Depends(optional_identity)) -> Identity:
    check_csrf(request)
    if ident is None:
        raise ApiError(401, "UNAUTHENTICATED", "セッションが見つかりません。最初からやり直してください")
    return ident


@dataclass
class GroupAccess:
    group: Group
    member: Membership

    @property
    def is_owner(self) -> bool:
        return self.member.role == "owner"


def load_group_access(db: Session, public_id: str, ident: Identity, *, for_update: bool = False) -> GroupAccess:
    """グループのメンバーでなければ 404(存在を隠す)。"""
    q = select(Group).where(Group.public_id == public_id)
    if for_update:
        q = q.with_for_update()
    group = db.execute(q).scalar_one_or_none()
    if group is None:
        raise not_found()
    member = db.execute(
        select(Membership).where(
            Membership.group_id == group.id,
            Membership.identity_id == ident.id,
            Membership.status == "active",
        )
    ).scalar_one_or_none()
    if member is None:
        raise not_found()
    return GroupAccess(group, member)


def parse_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as e:
        raise not_found() from e
