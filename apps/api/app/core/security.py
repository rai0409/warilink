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

from app.models.tables import Group, Identity, Membership, SessionRow, SettlementTransfer

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


def issue_session(db: Session, response: Response, ident: Identity, assurance_level: str) -> SessionRow:
    """新しいセッションを発行して Cookie に入れる。

    認証状態が変わるときは必ず新しいトークンにする(セッション固定の防止)。
    """
    s = get_settings()
    token = new_token()
    row = SessionRow(token_hash=hash_token(token), identity_id=ident.id, assurance_level=assurance_level,
                     expires_at=now() + timedelta(days=s.session_ttl_days))
    db.add(row)
    response.set_cookie(
        s.session_cookie_name, token, max_age=s.session_ttl_days * 86400, httponly=True,
        secure=s.cookie_secure, samesite="lax", path="/",
    )
    return row


def create_guest_session(db: Session, response: Response) -> Identity:
    ident = Identity(kind="guest")
    db.add(ident)
    db.flush()
    issue_session(db, response, ident, "guest")
    return ident


def current_session(request: Request, db: Session) -> SessionRow | None:
    """有効なセッション行(無効・期限切れ・失効なら None)。"""
    token = request.cookies.get(get_settings().session_cookie_name)
    if not token:
        return None
    row = db.execute(select(SessionRow).where(SessionRow.token_hash == hash_token(token))).scalar_one_or_none()
    if row is None or row.revoked_at is not None or row.expires_at <= now():
        return None
    return row


def clear_session_cookie(response: Response) -> None:
    s = get_settings()
    response.delete_cookie(s.session_cookie_name, path="/", secure=s.cookie_secure, httponly=True, samesite="lax")


def check_csrf(request: Request) -> None:
    if request.method in UNSAFE_METHODS and request.headers.get(CSRF_HEADER) != "1":
        raise ApiError(403, "CSRF_REJECTED", "不正なリクエストのため拒否しました。ページを再読み込みしてください")
    if request.method not in UNSAFE_METHODS:
        return
    s = get_settings()
    origin = request.headers.get("origin")
    if origin is None:
        # ブラウザは状態変更の fetch に Origin を付ける。本番では無い要求も拒否する(fail-closed)
        if s.environment == "production":
            raise ApiError(403, "CSRF_REJECTED", "送信元を確認できないため拒否しました。ページを再読み込みしてください")
        return
    if origin not in s.allowed_origins:
        raise ApiError(403, "CSRF_REJECTED", "許可されていない送信元からのリクエストです")


def optional_identity(request: Request, db: Session = Depends(get_db)) -> Identity | None:
    row = current_session(request, db)
    if row is None:
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


def require_step_up(request: Request, db: Session) -> SessionRow:
    """本人の再確認(step-up)。直近 `step_up_max_age_seconds` 以内に Passkey で確認したセッションだけを通す。"""
    sess = current_session(request, db)
    max_age = timedelta(seconds=get_settings().step_up_max_age_seconds)
    if sess is None or sess.assurance_level != "passkey" or sess.created_at < now() - max_age:
        raise ApiError(401, "STEP_UP_REQUIRED", "本人確認のため、もう一度 Passkey で確認してください")
    return sess


def load_transfer_party(db: Session, transfer_id: str, ident: Identity, *,
                        for_update: bool = False) -> tuple[SettlementTransfer, Membership]:
    """送金 1 件と、その当事者(支払者または受取人)としての自分のメンバー行。当事者以外には存在を隠す(404)。"""
    q = select(SettlementTransfer).where(SettlementTransfer.id == parse_uuid(transfer_id))
    if for_update:
        q = q.with_for_update()
    t = db.execute(q).scalar_one_or_none()
    if t is None:
        raise not_found()
    me = db.execute(select(Membership).where(
        Membership.group_id == t.group_id, Membership.identity_id == ident.id,
        Membership.status == "active")).scalar_one_or_none()
    if me is None or me.id not in (t.from_member_id, t.to_member_id):
        raise not_found()
    return t, me
