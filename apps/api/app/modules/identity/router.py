from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import check_csrf, create_guest_session, optional_identity
from app.models.tables import Identity

router = APIRouter(prefix="/identity", tags=["identity"])


class IdentityOut(BaseModel):
    identity_id: str
    kind: str


@router.post("/guest", response_model=IdentityOut, status_code=201)
def start_guest(request: Request, response: Response, db: Session = Depends(get_db),
                existing: Identity | None = Depends(optional_identity)) -> IdentityOut:
    """メール・電話番号不要のゲストセッションを開始する。既存の有効なセッションがあれば再利用する。"""
    check_csrf(request)
    if existing is not None:
        return IdentityOut(identity_id=str(existing.id), kind=existing.kind)
    ident = create_guest_session(db, response)
    db.commit()
    return IdentityOut(identity_id=str(ident.id), kind=ident.kind)


@router.get("/me", response_model=IdentityOut | None)
def me(ident: Identity | None = Depends(optional_identity)) -> IdentityOut | None:
    if ident is None:
        return None
    return IdentityOut(identity_id=str(ident.id), kind=ident.kind)
