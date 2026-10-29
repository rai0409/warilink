"""退会(アカウントの削除)。docs/SECURITY_PRIVACY.md §2.3 の設計どおり、次を 1 つのトランザクションで行う。

1. この本人のセッションをすべて失効させ、Passkey を失効させる(未使用の challenge は消す)
2. 振込先を暗号学的に消去する(すべての版の暗号文・nonce・包んだ鍵を消す)
3. 本人が作った有効な招待を失効させる
4. 参加中のグループのメンバー行は、名前だけ残して本人との結び付きを外す(支出・精算の金額と履歴は変えない)
5. identity を無効にする(以後このセッション・Passkey では使えない)

途中で失敗したら何も変わらない。削除できるのは自分だけで、他人のデータには触れない。
Passkey の機能が有効で登録済みの本人には、直近の Passkey 確認(step-up)を要求する。
ゲスト(と Passkey 停止中の登録済み本人)は、このブラウザのセッションと明示的な確認だけで削除できる
(ゲストにはそれ以外の本人確認の手段がなく、消せるのは自分のデータだけ)。

作成者だったグループは、参加の早い参加者へ自動で引き継ぐ(誰もいなければ参加者不在になり、30 日後に完全に削除する)。
消えないもの: 他のメンバーと共有している支出・精算の記録、名前だけのメンバー行、監査ログ(1 年で削除)、
バックアップに残る過去のデータ(30 日分。復元後は削除済み一覧で退会を再適用する。docs/DATA_MODEL.md §11)。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.core.security import clear_session_cookie, current_identity, now, require_step_up
from app.models.tables import (
    AuditEvent,
    Group,
    Identity,
    Invitation,
    Membership,
    PasskeyCredential,
    PaymentDestination,
    SessionRow,
    WebauthnChallenge,
)
from app.modules.groups.lifecycle import add_tombstone, mark_if_orphaned, release_owner
from app.modules.payment_destinations.router import crypto_erase

router = APIRouter(tags=["identity"])


class AccountDeleteIn(BaseModel):
    # 誤操作防止: 画面で「元に戻せない」ことを確認したときだけ true を送る
    confirm: Literal[True]


class AccountDeleteOut(BaseModel):
    groups_left: int
    destination_erased: bool


@router.delete("/account", response_model=AccountDeleteOut)
def delete_account(body: AccountDeleteIn, request: Request, response: Response, db: Session = Depends(get_db),
                   ident: Identity = Depends(current_identity)) -> AccountDeleteOut:
    if ident.kind == "registered" and get_settings().feature_passkeys:
        require_step_up(request, db)
    me = db.execute(select(Identity).where(Identity.id == ident.id).with_for_update()).scalar_one()
    t = now()

    db.execute(update(SessionRow).where(SessionRow.identity_id == me.id, SessionRow.revoked_at.is_(None))
               .values(revoked_at=t))
    db.execute(update(PasskeyCredential).where(PasskeyCredential.identity_id == me.id,
                                               PasskeyCredential.revoked_at.is_(None)).values(revoked_at=t))
    db.execute(delete(WebauthnChallenge).where(WebauthnChallenge.identity_id == me.id))

    dest = db.execute(select(PaymentDestination).where(PaymentDestination.owner_identity_id == me.id,
                                                       PaymentDestination.erased_at.is_(None))
                      .with_for_update()).scalar_one_or_none()
    if dest is not None:
        crypto_erase(db, dest)
        add_tombstone(db, "destination", dest.id)

    db.execute(update(Invitation).where(Invitation.created_by == me.id, Invitation.revoked_at.is_(None))
               .values(revoked_at=t))
    mine = list(db.execute(select(Membership).where(Membership.identity_id == me.id)
                           .order_by(Membership.group_id).with_for_update()).scalars())
    for m in mine:
        group = db.execute(select(Group).where(Group.id == m.group_id).with_for_update()).scalar_one()
        if m.role == "owner":
            release_owner(db, group, m)  # 参加の早い参加者へ引き継ぐ(誰もいなければ参加者不在になる)
        m.identity_id = None
        mark_if_orphaned(db, group)
    left = [m.id for m in mine]

    me.disabled_at = t
    me.webauthn_user_handle = None
    add_tombstone(db, "identity", me.id)  # バックアップから戻したときに、この退会を再適用する
    # 監査ログには件数だけを残す(名前・グループ名などは書かない)
    db.add(AuditEvent(actor_identity_id=me.id, action="identity.delete", target_type="identity", target_id=str(me.id),
                      safe_metadata={"groups_left": len(left), "destination_erased": dest is not None,
                                     "kind": me.kind}))
    db.commit()
    clear_session_cookie(response)
    return AccountDeleteOut(groups_left=len(left), destination_erased=dest is not None)


def purge_expired_challenges(db: Session, *, at=None) -> int:
    """使用済み・期限切れの WebAuthn challenge を消す(使い捨てで、期限後に使い道がない。何度実行しても同じ結果)。"""
    t = at or now()
    n = db.execute(delete(WebauthnChallenge).where(
        (WebauthnChallenge.used_at.is_not(None)) | (WebauthnChallenge.expires_at <= t))).rowcount
    db.commit()
    return int(n or 0)
