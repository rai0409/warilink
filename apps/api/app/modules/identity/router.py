from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import require_feature
from app.core.db import get_db
from app.core.security import check_csrf, create_guest_session, current_session, optional_identity, rate_limit
from app.models.tables import Identity, PasskeyCredential

from .passkeys import router as passkeys_router

# main.py はこの router だけを読み込む。Passkey(/auth/...)もここに含める
router = APIRouter(tags=["identity"])
router.include_router(passkeys_router)


class IdentityOut(BaseModel):
    identity_id: str
    kind: str  # guest | registered
    assurance_level: str = "guest"  # guest | passkey(このセッションを Passkey で確認したか)
    passkey_count: int = 0


@router.post("/identity/guest", response_model=IdentityOut, status_code=201)
def start_guest(request: Request, response: Response, db: Session = Depends(get_db),
                existing: Identity | None = Depends(optional_identity)) -> IdentityOut:
    """メール・電話番号不要のゲストセッションを開始する。既存の有効なセッションがあれば再利用する。"""
    require_feature("guest_sessions")
    check_csrf(request)
    if existing is not None:
        return IdentityOut(identity_id=str(existing.id), kind=existing.kind)
    # 新規作成だけを数える(Cookie を消して作り直すゲストも同じ IP・全体の上限に入る)
    rate_limit(request, "guest_create")
    ident = create_guest_session(db, response)
    db.commit()
    return IdentityOut(identity_id=str(ident.id), kind=ident.kind)


@router.get("/identity/me", response_model=IdentityOut | None)
def me(request: Request, db: Session = Depends(get_db),
       ident: Identity | None = Depends(optional_identity)) -> IdentityOut | None:
    if ident is None:
        return None
    sess = current_session(request, db)
    n = db.execute(select(func.count()).select_from(PasskeyCredential).where(
        PasskeyCredential.identity_id == ident.id, PasskeyCredential.revoked_at.is_(None))).scalar_one()
    return IdentityOut(identity_id=str(ident.id), kind=ident.kind,
                       assurance_level=sess.assurance_level if sess else "guest", passkey_count=n)
