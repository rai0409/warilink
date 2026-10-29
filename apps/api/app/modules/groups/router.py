from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings, require_feature
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
    rate_limit,
    record_rate_event,
)
from app.models.tables import AuditEvent, Group, Identity, Invitation, Membership

from .lifecycle import mark_if_orphaned, release_owner, require_open, transfer_owner

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
    closed: bool = False  # 作成者が終了したグループ(閲覧のみ)
    members: list[MemberOut]


class GroupSummaryOut(BaseModel):
    public_id: str
    title: str
    member_count: int
    is_owner: bool
    closed: bool


class InviteStatusOut(BaseModel):
    active: bool  # 使える招待が 1 つ以上ある
    expires_at: str | None  # 使える招待のうち、いちばん遅い期限


class MemberRename(BaseModel):
    display_name: Name


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
        closed=group.closed_at is not None,
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
    require_feature("groups")
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


@router.get("/groups", response_model=list[GroupSummaryOut])
def my_groups(db: Session = Depends(get_db), ident: Identity = Depends(current_identity)):
    """自分が参加しているグループ(新しい順)。ホームの「最近のグループ」に使う。"""
    rows = db.execute(
        select(Group, Membership.role).join(Membership, Membership.group_id == Group.id)
        .where(Membership.identity_id == ident.id, Membership.status == "active")
        .order_by(Group.created_at.desc())
    ).all()
    counts = dict(db.execute(
        select(Membership.group_id, func.count()).where(
            Membership.group_id.in_([g.id for g, _ in rows]), Membership.status == "active")
        .group_by(Membership.group_id)).all()) if rows else {}
    return [GroupSummaryOut(public_id=g.public_id, title=g.title, member_count=counts.get(g.id, 0),
                            is_owner=role == "owner", closed=g.closed_at is not None) for g, role in rows]


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
    require_feature("groups")
    acc = load_group_access(db, public_id, ident, for_update=True)
    require_open(acc.group)
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
def create_invite(public_id: str, body: InviteCreate, request: Request, db: Session = Depends(get_db),
                  ident: Identity = Depends(current_identity)):
    require_feature("groups")
    rate_limit(request, "invite_create", identity=ident)
    acc = load_group_access(db, public_id, ident)
    require_open(acc.group)
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


class InviteRevokeOut(BaseModel):
    revoked: int


@router.post("/groups/{public_id}/invites/revoke", response_model=InviteRevokeOut)
def revoke_invites(public_id: str, db: Session = Depends(get_db), ident: Identity = Depends(current_identity)):
    """有効な招待 URL をすべて使えなくする(作成者だけ)。参加済みのメンバーはそのまま。"""
    require_feature("groups")
    acc = load_group_access(db, public_id, ident, for_update=True)
    if not acc.is_owner:
        raise ApiError(403, "ACCESS_DENIED", "招待URLを無効にできるのはグループ作成者だけです")
    ids = db.execute(update(Invitation).where(Invitation.group_id == acc.group.id, Invitation.revoked_at.is_(None))
                     .values(revoked_at=now()).returning(Invitation.id)).scalars().all()
    db.add(AuditEvent(actor_identity_id=ident.id, action="group.invites.revoke", target_type="group",
                      target_id=str(acc.group.id), safe_metadata={"revoked": len(ids)}))
    db.commit()
    return InviteRevokeOut(revoked=len(ids))


@router.post("/groups/{public_id}/leave", response_model=None, status_code=204)
def leave_group(public_id: str, db: Session = Depends(get_db), ident: Identity = Depends(current_identity)) -> None:
    """グループから抜ける。メンバー行は名前だけ残して本人との結び付きを外す(支出・精算の金額と履歴は変えない)。

    作成者が抜けるときは、参加の早い参加者へ自動で引き継ぐ。誰もいなければ参加者不在になり、一定期間後に完全に削除する。
    抜けた後は、このグループを見られない(404)。
    """
    acc = load_group_access(db, public_id, ident, for_update=True)
    if acc.is_owner:
        release_owner(db, acc.group, acc.member)
    acc.member.identity_id = None
    mark_if_orphaned(db, acc.group)
    db.add(AuditEvent(actor_identity_id=ident.id, action="group.leave", target_type="group",
                      target_id=str(acc.group.id), safe_metadata={}))
    db.commit()


@router.get("/groups/{public_id}/invites/status", response_model=InviteStatusOut)
def invite_status(public_id: str, db: Session = Depends(get_db), ident: Identity = Depends(current_identity)):
    """使える招待があるか(トークンは返さない。保存しているのはハッシュだけ)。"""
    acc = load_group_access(db, public_id, ident)
    latest = db.execute(select(func.max(Invitation.expires_at)).where(
        Invitation.group_id == acc.group.id, Invitation.revoked_at.is_(None), Invitation.expires_at > now(),
        Invitation.uses < Invitation.max_uses)).scalar_one()
    return InviteStatusOut(active=latest is not None, expires_at=latest.isoformat() if latest else None)


@router.patch("/groups/{public_id}/members/{member_id}", response_model=GroupOut)
def rename_member(public_id: str, member_id: str, body: MemberRename, db: Session = Depends(get_db),
                  ident: Identity = Depends(current_identity)):
    """表示名を変える。自分の名前と、作成者なら名前だけのメンバー(未参加)の名前を変えられる。"""
    require_feature("groups")
    acc = load_group_access(db, public_id, ident, for_update=True)
    require_open(acc.group)
    target = db.execute(select(Membership).where(
        Membership.id == parse_uuid(member_id), Membership.group_id == acc.group.id,
        Membership.status == "active")).scalar_one_or_none()
    if target is None:
        raise not_found()
    if not (target.id == acc.member.id or (acc.is_owner and target.identity_id is None)):
        raise ApiError(403, "ACCESS_DENIED", "変更できるのは自分の名前と、作成者による未参加のメンバーの名前だけです")
    target.display_name = body.display_name
    db.add(AuditEvent(actor_identity_id=ident.id, action="group.members.rename", target_type="group",
                      target_id=str(acc.group.id), safe_metadata={}))
    db.flush()
    out = group_out(db, acc.group, acc.member)
    db.commit()
    return out


class OwnerTransferIn(BaseModel):
    member_id: str


@router.post("/groups/{public_id}/owner", response_model=GroupOut)
def change_owner(public_id: str, body: OwnerTransferIn, db: Session = Depends(get_db),
                 ident: Identity = Depends(current_identity)):
    """作成者を、参加済みの別のメンバーへ引き継ぐ(作成者だけ)。"""
    acc = load_group_access(db, public_id, ident, for_update=True)
    if not acc.is_owner:
        raise ApiError(403, "ACCESS_DENIED", "引き継げるのはグループを作成した人だけです")
    target = db.execute(select(Membership).where(
        Membership.id == parse_uuid(body.member_id), Membership.group_id == acc.group.id,
        Membership.status == "active")).scalar_one_or_none()
    if target is None or target.identity_id is None or target.id == acc.member.id:
        raise ApiError(422, "VALIDATION_ERROR", "引き継ぐ相手には、参加済みの別のメンバーを選んでください")
    transfer_owner(db, acc.group, acc.member, target, reason="manual")
    db.flush()
    out = group_out(db, acc.group, acc.member)
    db.commit()
    return out


@router.post("/groups/{public_id}/close", response_model=GroupOut)
def close_group(public_id: str, db: Session = Depends(get_db), ident: Identity = Depends(current_identity)):
    """グループを終了する(作成者だけ)。以後は閲覧のみで、一定期間後に完全に削除する。招待はすべて失効する。"""
    acc = load_group_access(db, public_id, ident, for_update=True)
    if not acc.is_owner:
        raise ApiError(403, "ACCESS_DENIED", "終了できるのはグループを作成した人だけです")
    require_open(acc.group)
    acc.group.closed_at = now()
    db.execute(update(Invitation).where(Invitation.group_id == acc.group.id, Invitation.revoked_at.is_(None))
               .values(revoked_at=now()))
    db.add(AuditEvent(actor_identity_id=ident.id, action="group.close", target_type="group",
                      target_id=str(acc.group.id), safe_metadata={}))
    db.flush()
    out = group_out(db, acc.group, acc.member)
    db.commit()
    return out


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


def _invite_rate_limit(request: Request, ident: Identity, token: str) -> None:
    """招待の確認・参加の回数制限。token の有効・無効を調べる前に判定するので、429 から token の存在は分からない。"""
    rate_limit(request, "invite_access", identity=ident, token=token, check=("invite_miss",))


def _count_invite_miss(request: Request, e: ApiError) -> None:
    if e.code == "INVITE_INVALID":  # 無効・期限切れ・存在しない招待は推測の試行として数える
        record_rate_event(request, "invite_miss")


@router.get("/invites/{token}", response_model=InvitePreview)
def preview_invite(token: str, request: Request, db: Session = Depends(get_db),
                   ident: Identity = Depends(current_identity)):
    """参加前の確認。グループ名と、まだ誰も紐付けていない名前だけを返す(金額は返さない)。"""
    _invite_rate_limit(request, ident, token)
    try:
        inv = _valid_invite(db, token)
    except ApiError as e:
        _count_invite_miss(request, e)
        raise
    group = db.get(Group, inv.group_id)
    assert group is not None
    return InvitePreview(
        group_title=group.title,
        unclaimed_members=[MemberOut(id=str(m.id), display_name=m.display_name, role=m.role,
                                     claimed=False, is_me=False)
                           for m in _active_members(db, group) if m.identity_id is None],
    )


@router.post("/invites/{token}/join", response_model=GroupOut)
def join(token: str, body: JoinRequest, request: Request, db: Session = Depends(get_db),
         ident: Identity = Depends(current_identity)):
    require_feature("groups")
    _invite_rate_limit(request, ident, token)
    try:
        return _join(token, body, db, ident)
    except ApiError as e:
        db.rollback()
        _count_invite_miss(request, e)
        raise


def _join(token: str, body: JoinRequest, db: Session, ident: Identity):
    # 使用回数の判定は「参加済みかどうか」を見た後に行う(参加済みの本人の再アクセスは回数を消費しない)
    inv = _valid_invite(db, token, for_update=True, check_uses=False)
    group = db.execute(select(Group).where(Group.id == inv.group_id).with_for_update()).scalar_one()
    require_open(group)
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
    group.orphaned_at = None  # 参加者が戻ったので、参加者不在による削除は取りやめる
    db.add(AuditEvent(actor_identity_id=ident.id, action="group.join", target_type="group",
                      target_id=str(group.id)))
    db.flush()
    out = group_out(db, group, me)
    db.commit()
    return out


__all__ = ["router", "group_out", "not_found"]
