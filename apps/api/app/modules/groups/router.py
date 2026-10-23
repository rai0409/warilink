from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import ApiError, not_found
from app.core.idempotency import run_idempotent
from app.core.security import (
    current_identity,
    hash_token,
    load_group_access,
    new_token,
    now,
    parse_uuid,
)
from app.models.tables import AuditEvent, Group, Identity, Invitation, Membership

router = APIRouter(tags=["groups"])

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class MemberOut(BaseModel):
    id: str
    display_name: str
    role: str
    claimed: bool
    is_me: bool


class GroupOut(BaseModel):
    public_id: str
    title: str
    currency: str
    ledger_version: int
    my_member_id: str
    is_owner: bool
    members: list[MemberOut]


class GroupCreate(BaseModel):
    title: Title
    my_name: Name
    other_member_names: list[Name] = Field(default_factory=list, max_length=99)


class MembersAdd(BaseModel):
    names: list[Name] = Field(min_length=1, max_length=50)


class InviteCreate(BaseModel):
    ttl_hours: int | None = Field(default=None, ge=1, le=24 * 30)
    max_uses: int | None = Field(default=None, ge=1, le=500)


class InviteOut(BaseModel):
    token: str  # 生トークンは発行時の一度だけ返す。DB にはハッシュのみ。
    expires_at: str
    max_uses: int


class InvitePreview(BaseModel):
    group_title: str
    unclaimed_members: list[MemberOut]


class JoinRequest(BaseModel):
    claim_member_id: str | None = None
    new_member_name: Name | None = None


def _active_members(db: Session, group: Group) -> list[Membership]:
    return list(db.execute(
        select(Membership).where(Membership.group_id == group.id, Membership.status == "active")
        .order_by(Membership.position)
    ).scalars())


def group_out(db: Session, group: Group, me: Membership) -> GroupOut:
    return GroupOut(
        public_id=group.public_id, title=group.title, currency=group.currency,
        ledger_version=group.ledger_version, my_member_id=str(me.id), is_owner=me.role == "owner",
        members=[MemberOut(id=str(m.id), display_name=m.display_name, role=m.role,
                           claimed=m.identity_id is not None, is_me=m.id == me.id)
                 for m in _active_members(db, group)],
    )


def _next_position(db: Session, group: Group) -> int:
    # 呼び出し側でグループ行をロックしている前提(同時追加で position が衝突しない)
    return db.execute(select(func.coalesce(func.max(Membership.position), -1) + 1)
                      .where(Membership.group_id == group.id)).scalar_one()


def _ensure_member_capacity(db: Session, group: Group, adding: int) -> None:
    count = db.execute(select(func.count()).select_from(Membership).where(
        Membership.group_id == group.id, Membership.status == "active")).scalar_one()
    if count + adding > 100:
        raise ApiError(422, "TOO_MANY_MEMBERS", "1グループのメンバーは100人までです")


@router.post("/groups", status_code=201, response_model=GroupOut)
def create_group(body: GroupCreate, request: Request, db: Session = Depends(get_db),
                 ident: Identity = Depends(current_identity)):
    def do():
        group = Group(public_id=secrets.token_urlsafe(12), owner_identity_id=ident.id, title=body.title)
        db.add(group)
        db.flush()
        me = Membership(group_id=group.id, identity_id=ident.id, display_name=body.my_name, role="owner",
                        position=0)
        db.add(me)
        for i, n in enumerate(body.other_member_names, start=1):
            db.add(Membership(group_id=group.id, display_name=n, role="member", position=i))
        db.add(AuditEvent(actor_identity_id=ident.id, action="group.create", target_type="group",
                          target_id=str(group.id)))
        db.flush()
        return group_out(db, group, me)

    return run_idempotent(db, request, ident, "group.create", body, do, status_code=201)


@router.get("/groups/{public_id}", response_model=GroupOut)
def get_group(public_id: str, db: Session = Depends(get_db), ident: Identity = Depends(current_identity)):
    acc = load_group_access(db, public_id, ident)
    return group_out(db, acc.group, acc.member)


@router.get("/groups/{public_id}/members", response_model=list[MemberOut])
def list_members(public_id: str, db: Session = Depends(get_db), ident: Identity = Depends(current_identity)):
    acc = load_group_access(db, public_id, ident)
    return group_out(db, acc.group, acc.member).members


@router.post("/groups/{public_id}/members", response_model=GroupOut, status_code=201)
def add_members(public_id: str, body: MembersAdd, db: Session = Depends(get_db),
                ident: Identity = Depends(current_identity)):
    """名前だけのメンバーを追加(アプリを使わない人も精算対象にできる)。"""
    acc = load_group_access(db, public_id, ident, for_update=True)
    _ensure_member_capacity(db, acc.group, len(body.names))
    start = _next_position(db, acc.group)
    for i, n in enumerate(body.names):
        db.add(Membership(group_id=acc.group.id, display_name=n, role="member", position=start + i))
    db.add(AuditEvent(actor_identity_id=ident.id, action="group.members.add", target_type="group",
                      target_id=str(acc.group.id), safe_metadata={"count": len(body.names)}))
    db.flush()
    out = group_out(db, acc.group, acc.member)
    db.commit()
    return out


@router.post("/groups/{public_id}/invites", response_model=InviteOut, status_code=201)
def create_invite(public_id: str, body: InviteCreate, db: Session = Depends(get_db),
                  ident: Identity = Depends(current_identity)):
    acc = load_group_access(db, public_id, ident)
    if not acc.is_owner:
        raise ApiError(403, "ACCESS_DENIED", "招待URLを発行できるのはグループ作成者だけです")
    s = get_settings()
    token = new_token()
    inv = Invitation(
        group_id=acc.group.id, token_hash=hash_token(token), created_by=ident.id,
        expires_at=now() + timedelta(hours=body.ttl_hours or s.invite_default_ttl_hours),
        max_uses=body.max_uses or s.invite_default_max_uses,
    )
    db.add(inv)
    db.commit()
    return InviteOut(token=token, expires_at=inv.expires_at.isoformat(), max_uses=inv.max_uses)


def _invite_error() -> ApiError:
    return ApiError(404, "INVITE_INVALID", "招待URLが無効か、有効期限が切れています")


def _valid_invite(db: Session, token: str, *, for_update: bool = False, check_uses: bool = True) -> Invitation:
    q = select(Invitation).where(Invitation.token_hash == hash_token(token))
    if for_update:
        q = q.with_for_update()
    inv = db.execute(q).scalar_one_or_none()
    if inv is None or inv.revoked_at is not None or inv.expires_at <= now():
        raise _invite_error()
    if check_uses and inv.uses >= inv.max_uses:
        raise _invite_error()
    return inv


@router.get("/invites/{token}", response_model=InvitePreview)
def preview_invite(token: str, db: Session = Depends(get_db), ident: Identity = Depends(current_identity)):
    """参加前の確認。グループ名と、まだ誰も紐付けていない名前だけを返す(金額は返さない)。"""
    inv = _valid_invite(db, token)
    group = db.get(Group, inv.group_id)
    assert group is not None
    return InvitePreview(
        group_title=group.title,
        unclaimed_members=[MemberOut(id=str(m.id), display_name=m.display_name, role=m.role,
                                     claimed=False, is_me=False)
                           for m in _active_members(db, group) if m.identity_id is None],
    )


@router.post("/invites/{token}/join", response_model=GroupOut)
def join(token: str, body: JoinRequest, db: Session = Depends(get_db),
         ident: Identity = Depends(current_identity)):
    # 使用回数の判定は「参加済みかどうか」を見た後に行う(参加済みの本人の再アクセスは回数を消費しない)
    inv = _valid_invite(db, token, for_update=True, check_uses=False)
    group = db.execute(select(Group).where(Group.id == inv.group_id).with_for_update()).scalar_one()
    existing = db.execute(select(Membership).where(
        Membership.group_id == group.id, Membership.identity_id == ident.id)).scalar_one_or_none()
    if existing is not None:
        return group_out(db, group, existing)  # 参加済みなら冪等に返す(使用回数は増やさない)
    if inv.uses >= inv.max_uses:
        raise _invite_error()

    if (body.claim_member_id is None) == (body.new_member_name is None):
        raise ApiError(422, "VALIDATION_ERROR", "参加する名前を1つ選ぶか、新しい名前を入力してください")
    if body.claim_member_id is not None:
        mid = parse_uuid(body.claim_member_id)
        # 未紐付けの行だけを条件付き UPDATE で確保(同時に同じ名前を選んでも1人だけ成功)
        res = db.execute(
            update(Membership)
            .where(Membership.id == mid, Membership.group_id == group.id,
                   Membership.identity_id.is_(None), Membership.status == "active")
            .values(identity_id=ident.id)
        )
        if res.rowcount != 1:
            raise ApiError(409, "MEMBER_ALREADY_CLAIMED", "その名前は既に他の人が選択しています")
        me = db.get(Membership, mid)
        assert me is not None
    else:
        _ensure_member_capacity(db, group, 1)
        me = Membership(group_id=group.id, identity_id=ident.id, display_name=body.new_member_name,
                        position=_next_position(db, group))
        db.add(me)
    inv.uses += 1
    db.add(AuditEvent(actor_identity_id=ident.id, action="group.join", target_type="group",
                      target_id=str(group.id)))
    db.flush()
    out = group_out(db, group, me)
    db.commit()
    return out


__all__ = ["router", "group_out", "not_found"]
