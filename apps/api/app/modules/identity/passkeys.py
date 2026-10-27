"""Passkey(WebAuthn)による任意の登録・ログイン。ゲストのままでも全機能(振込先の保存を除く)を使える。

- challenge はサーバーで作り、1 回だけ・短時間だけ有効。登録は開始したセッションに結びつける。
- RP ID と Origin は設定値(`rp_id`、`allowed_origins`)で検証する。ユーザー検証(生体認証・PIN)を必須にする。
- 登録・ログインのたびにセッションを作り直す(セッション固定の防止)。
- メール・SMS などの弱い本人確認でアカウントを復元しない。Passkey を失った場合の復元は保証しない。
"""

from __future__ import annotations

import base64
import json
import secrets
from datetime import timedelta
from typing import Any, Literal

import webauthn
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from webauthn.helpers import exceptions as wa_exc
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.core.config import get_settings, require_feature
from app.core.db import get_db
from app.core.errors import ApiError, not_found
from app.core.security import (
    check_csrf,
    clear_session_cookie,
    current_identity,
    current_session,
    hash_token,
    issue_session,
    now,
    optional_identity,
    parse_uuid,
)
from app.models.tables import AuditEvent, Identity, Membership, PasskeyCredential, SessionRow, WebauthnChallenge

router = APIRouter(prefix="/auth", tags=["auth"])

Nickname = Field(default=None, min_length=1, max_length=40)


class OptionsOut(BaseModel):
    flow_id: str
    options: dict[str, Any]  # PublicKeyCredentialCreationOptionsJSON / RequestOptionsJSON


class RegisterOptionsIn(BaseModel):
    nickname: str | None = Nickname


class RegisterVerifyIn(BaseModel):
    flow_id: str = Field(min_length=10, max_length=100)
    credential: dict[str, Any]
    nickname: str | None = Nickname


class LoginVerifyIn(BaseModel):
    flow_id: str = Field(min_length=10, max_length=100)
    credential: dict[str, Any]
    # この端末のゲストとして参加していたグループを、ログインするアカウントへ移す(本人が選んだときだけ)
    merge_current_guest: bool = False


class PasskeyOut(BaseModel):
    id: str
    nickname: str
    created_at: str
    last_used_at: str | None
    device_type: Literal["single_device", "multi_device"]
    backed_up: bool


class SessionStateOut(BaseModel):
    identity_id: str
    kind: str
    assurance_level: str
    passkey_count: int
    merged_groups: int = 0
    unmerged_groups: int = 0


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _passkeys(db: Session, ident: Identity) -> list[PasskeyCredential]:
    return list(db.execute(select(PasskeyCredential).where(
        PasskeyCredential.identity_id == ident.id, PasskeyCredential.revoked_at.is_(None))
        .order_by(PasskeyCredential.created_at)).scalars())


def state_out(db: Session, ident: Identity, assurance: str, **extra: int) -> SessionStateOut:
    return SessionStateOut(identity_id=str(ident.id), kind=ident.kind, assurance_level=assurance,
                           passkey_count=len(_passkeys(db, ident)), **extra)


def _new_flow(db: Session, purpose: str, challenge: bytes, ident: Identity | None,
              sess: SessionRow | None) -> str:
    s = get_settings()
    flow = secrets.token_urlsafe(32)
    db.add(WebauthnChallenge(flow_hash=hash_token(flow), purpose=purpose, challenge=challenge,
                             identity_id=ident.id if ident else None, session_id=sess.id if sess else None,
                             rp_id=s.rp_id, expires_at=now() + timedelta(seconds=s.webauthn_challenge_ttl_seconds)))
    return flow


def _consume_flow(db: Session, flow_id: str, purpose: str) -> WebauthnChallenge:
    """challenge を 1 回だけ使う。期限切れ・使用済み・目的違いは同じ 400 にする(どれかを教えない)。"""
    row = db.execute(update(WebauthnChallenge)
                     .where(WebauthnChallenge.flow_hash == hash_token(flow_id), WebauthnChallenge.purpose == purpose,
                            WebauthnChallenge.used_at.is_(None), WebauthnChallenge.expires_at > now())
                     .values(used_at=now()).returning(WebauthnChallenge)).scalar_one_or_none()
    db.commit()  # 検証に失敗しても使用済みのまま(再利用させない)
    if row is None:
        raise ApiError(400, "PASSKEY_CHALLENGE_INVALID", "認証の有効期限が切れました。もう一度やり直してください")
    return row


def _fail(e: Exception) -> ApiError:
    # 検証失敗の詳細(どの項目が違ったか)は利用者に出さない
    return ApiError(400, "PASSKEY_VERIFICATION_FAILED", "Passkey を確認できませんでした。もう一度お試しください")


def _audit(db: Session, ident: Identity, action: str, **meta: Any) -> None:
    db.add(AuditEvent(actor_identity_id=ident.id, action=action, target_type="identity", target_id=str(ident.id),
                      safe_metadata=meta))


# ---------------------------------------------------------------- 登録(ゲスト → 登録済みへ昇格。追加の Passkey も同じ)


@router.post("/passkeys/register/options", response_model=OptionsOut)
def register_options(body: RegisterOptionsIn, request: Request, db: Session = Depends(get_db),
                     ident: Identity = Depends(current_identity)) -> OptionsOut:
    require_feature("passkeys")
    s = get_settings()
    sess = current_session(request, db)
    if ident.webauthn_user_handle is None:
        ident.webauthn_user_handle = secrets.token_bytes(32)
    existing = _passkeys(db, ident)
    opts = webauthn.generate_registration_options(
        rp_id=s.rp_id, rp_name=s.webauthn_rp_name,
        # 名前は個人情報を含めない(Passkey の一覧で見分けるための表示だけ)
        user_name=body.nickname or "WariLink", user_display_name=body.nickname or "WariLink",
        user_id=ident.webauthn_user_handle,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED, user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=c.credential_id) for c in existing],
        timeout=s.webauthn_challenge_ttl_seconds * 1000,
    )
    flow = _new_flow(db, "register", opts.challenge, ident, sess)
    db.commit()
    return OptionsOut(flow_id=flow, options=json.loads(webauthn.options_to_json(opts)))


@router.post("/passkeys/register/verify", response_model=SessionStateOut)
def register_verify(body: RegisterVerifyIn, request: Request, response: Response, db: Session = Depends(get_db),
                    ident: Identity = Depends(current_identity)) -> SessionStateOut:
    require_feature("passkeys")
    s = get_settings()
    sess = current_session(request, db)
    ch = _consume_flow(db, body.flow_id, "register")
    # 登録を始めた本人・同じセッションでなければ受け付けない(他人の challenge の流用を防ぐ)
    if ch.identity_id != ident.id or sess is None or ch.session_id != sess.id or ch.rp_id != s.rp_id:
        raise ApiError(400, "PASSKEY_CHALLENGE_INVALID", "認証の有効期限が切れました。もう一度やり直してください")
    try:
        v = webauthn.verify_registration_response(
            credential=body.credential, expected_challenge=ch.challenge, expected_rp_id=s.rp_id,
            expected_origin=s.allowed_origins, require_user_verification=True)
    except (wa_exc.InvalidRegistrationResponse, wa_exc.InvalidJSONStructure, wa_exc.InvalidCBORData,
            ValueError, KeyError, TypeError) as e:
        raise _fail(e) from None
    if db.execute(select(PasskeyCredential.id).where(PasskeyCredential.credential_id == v.credential_id)).first():
        raise ApiError(409, "PASSKEY_ALREADY_REGISTERED", "この Passkey は既に登録されています")
    transports = body.credential.get("response", {}).get("transports") or []
    count = len(_passkeys(db, ident))
    db.add(PasskeyCredential(
        identity_id=ident.id, credential_id=v.credential_id, public_key=v.credential_public_key,
        sign_count=v.sign_count, transports=[t for t in transports if isinstance(t, str)][:8], aaguid=v.aaguid,
        device_type=v.credential_device_type.value, backed_up=v.credential_backed_up,
        nickname=body.nickname or f"Passkey {count + 1}"))
    upgraded = ident.kind == "guest"
    if upgraded:
        # 同じ identity のまま昇格するので、参加グループ・支出・精算はそのまま引き継がれる
        ident.kind = "registered"
        ident.registered_at = now()
    # 昇格・追加のたびにセッションを作り直し、古いトークンは失効させる
    sess.revoked_at = now()
    issue_session(db, response, ident, "passkey")
    _audit(db, ident, "passkey.register", upgraded=upgraded)
    db.commit()
    return state_out(db, ident, "passkey")


# ---------------------------------------------------------------- ログイン(別の端末・Cookie を消した後)


@router.post("/passkeys/login/options", response_model=OptionsOut)
def login_options(request: Request, db: Session = Depends(get_db),
                  ident: Identity | None = Depends(optional_identity)) -> OptionsOut:
    require_feature("passkeys")
    check_csrf(request)
    s = get_settings()
    opts = webauthn.generate_authentication_options(
        rp_id=s.rp_id, user_verification=UserVerificationRequirement.REQUIRED,
        timeout=s.webauthn_challenge_ttl_seconds * 1000)  # allow_credentials は空(端末に保存された Passkey から選ぶ)
    flow = _new_flow(db, "authenticate", opts.challenge, None, current_session(request, db))
    db.commit()
    return OptionsOut(flow_id=flow, options=json.loads(webauthn.options_to_json(opts)))


def _decode_b64url(v: Any) -> bytes:
    if not isinstance(v, str):
        raise ValueError("not a string")
    return base64.urlsafe_b64decode(v + "=" * (-len(v) % 4))


@router.post("/passkeys/login/verify", response_model=SessionStateOut)
def login_verify(body: LoginVerifyIn, request: Request, response: Response, db: Session = Depends(get_db),
                 caller: Identity | None = Depends(optional_identity)) -> SessionStateOut:
    require_feature("passkeys")
    check_csrf(request)
    s = get_settings()
    sess = current_session(request, db)
    ch = _consume_flow(db, body.flow_id, "authenticate")
    if ch.rp_id != s.rp_id or ch.session_id != (sess.id if sess else None):
        raise ApiError(400, "PASSKEY_CHALLENGE_INVALID", "認証の有効期限が切れました。もう一度やり直してください")
    try:
        raw_id = _decode_b64url(body.credential.get("rawId") or body.credential.get("id"))
        user_handle = _decode_b64url(body.credential.get("response", {}).get("userHandle"))
    except (ValueError, TypeError):
        raise _fail(ValueError()) from None
    cred = db.execute(select(PasskeyCredential).where(PasskeyCredential.credential_id == raw_id,
                                                      PasskeyCredential.revoked_at.is_(None))
                      .with_for_update()).scalar_one_or_none()
    owner = db.get(Identity, cred.identity_id) if cred else None
    # 未登録・失効済み・本人の user handle と一致しない Passkey は同じ応答で拒否する
    if cred is None or owner is None or owner.disabled_at is not None or owner.webauthn_user_handle != user_handle:
        raise _fail(ValueError())
    try:
        v = webauthn.verify_authentication_response(
            credential=body.credential, expected_challenge=ch.challenge, expected_rp_id=s.rp_id,
            expected_origin=s.allowed_origins, credential_public_key=cred.public_key,
            credential_current_sign_count=cred.sign_count, require_user_verification=True)
    except (wa_exc.InvalidAuthenticationResponse, wa_exc.InvalidJSONStructure, wa_exc.InvalidCBORData,
            ValueError, KeyError, TypeError) as e:
        # 署名回数が戻った(複製の疑い)場合もここで拒否される
        raise _fail(e) from None
    cred.sign_count = v.new_sign_count
    cred.last_used_at = now()
    cred.backed_up = v.credential_backed_up

    merged = unmerged = 0
    if body.merge_current_guest and caller is not None and caller.kind == "guest" and caller.id != owner.id:
        merged, unmerged = _merge_guest(db, caller, owner)
    if sess is not None:
        sess.revoked_at = now()  # この端末の以前のセッション(ゲストを含む)は失効させる
    issue_session(db, response, owner, "passkey")
    _audit(db, owner, "passkey.login", merged_groups=merged)
    db.commit()
    return state_out(db, owner, "passkey", merged_groups=merged, unmerged_groups=unmerged)


def _merge_guest(db: Session, guest: Identity, owner: Identity) -> tuple[int, int]:
    """ゲストとして参加していたグループを登録済みアカウントへ移す。両方が同じグループにいる場合は移さない。"""
    owner_groups = select(Membership.group_id).where(Membership.identity_id == owner.id)
    moved = db.execute(update(Membership).where(Membership.identity_id == guest.id,
                                                Membership.group_id.not_in(owner_groups))
                       .values(identity_id=owner.id).returning(Membership.id)).scalars().all()
    left = db.execute(select(func.count()).select_from(Membership)
                      .where(Membership.identity_id == guest.id)).scalar_one()
    _audit(db, guest, "identity.merge_guest", moved=len(moved), left=left)
    return len(moved), int(left)


# ---------------------------------------------------------------- 管理


@router.get("/passkeys", response_model=list[PasskeyOut])
def list_passkeys(db: Session = Depends(get_db), ident: Identity = Depends(current_identity)) -> list[PasskeyOut]:
    return [PasskeyOut(id=str(c.id), nickname=c.nickname, created_at=c.created_at.isoformat(),
                       last_used_at=c.last_used_at.isoformat() if c.last_used_at else None,
                       device_type=c.device_type, backed_up=c.backed_up)  # type: ignore[arg-type]
            for c in _passkeys(db, ident)]


@router.delete("/passkeys/{passkey_id}", response_model=SessionStateOut)
def revoke_passkey(passkey_id: str, request: Request, db: Session = Depends(get_db),
                   ident: Identity = Depends(current_identity)) -> SessionStateOut:
    sess = current_session(request, db)
    c = db.execute(select(PasskeyCredential).where(PasskeyCredential.id == parse_uuid(passkey_id),
                                                   PasskeyCredential.identity_id == ident.id,
                                                   PasskeyCredential.revoked_at.is_(None))).scalar_one_or_none()
    if c is None:
        raise not_found()  # 他人の Passkey は存在も知らせない
    if len(_passkeys(db, ident)) <= 1:
        raise ApiError(409, "LAST_PASSKEY",
                       "最後の Passkey は削除できません。別の Passkey を追加してから削除してください")
    c.revoked_at = now()
    _audit(db, ident, "passkey.revoke")
    db.commit()
    return state_out(db, ident, sess.assurance_level if sess else "guest")


@router.post("/logout", response_model=None, status_code=204)
def logout(request: Request, response: Response, db: Session = Depends(get_db),
           ident: Identity = Depends(current_identity)) -> Response:
    if ident.kind == "guest":
        # ゲストはログアウトすると二度と戻れない(Passkey が無い)。誤操作を防ぐため拒否する
        raise ApiError(409, "GUEST_CANNOT_LOGOUT",
                       "ゲストのままログアウトすると、このグループに戻れなくなります。先に Passkey を登録してください")
    sess = current_session(request, db)
    if sess is not None:
        sess.revoked_at = now()
    _audit(db, ident, "session.logout")
    db.commit()
    out = Response(status_code=204)
    clear_session_cookie(out)
    return out


@router.post("/sessions/revoke-others", response_model=SessionStateOut)
def revoke_other_sessions(request: Request, db: Session = Depends(get_db),
                          ident: Identity = Depends(current_identity)) -> SessionStateOut:
    """紛失した端末などに残ったログインを、この端末以外すべて失効させる。"""
    sess = current_session(request, db)
    assert sess is not None
    n = db.execute(update(SessionRow).where(SessionRow.identity_id == ident.id, SessionRow.id != sess.id,
                                            SessionRow.revoked_at.is_(None))
                   .values(revoked_at=now()).returning(SessionRow.id)).scalars().all()
    _audit(db, ident, "session.revoke_others", count=len(n))
    db.commit()
    return state_out(db, ident, sess.assurance_level)
