"""振込先(受取口座・PayPay ID)の登録と、精算の支払者だけへの限定表示。

- 保存できるのは Passkey 登録済みの本人だけ。登録・変更・共有設定・全桁表示には直近の Passkey 確認(step-up)が必要。
- V0 で機能を停止中でも、既存データは登録済み所有者が暗号学的に消去できる。この例外は作成・変更・共有・開示には使えない。
- 中身は版ごとに暗号化(crypto.py)。一覧・監査ログ・URL には伏せ字(masked_label)と ID だけを出す。
- 表示できるのは「その送金の支払者」だけ。受取人が共有を許可していなければ表示しない。
- アプリは送金を実行しない。表示とコピーのみ。照合コードは支払いの証明ではない。
"""

from __future__ import annotations

import secrets
import unicodedata
import uuid
from typing import Annotated, Any, Literal

from cryptography.exceptions import InvalidTag
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings, require_feature
from app.core.db import get_db
from app.core.errors import ApiError, not_found
from app.core.security import current_identity, load_transfer_party, now, require_step_up
from app.models.tables import (
    AuditEvent,
    Identity,
    Membership,
    PaymentDestination,
    PaymentDestinationVersion,
    TransferDestinationPin,
)

from . import crypto

router = APIRouter(tags=["payment-destinations"])

ACCOUNT_TYPE_LABEL = {"ordinary": "普通", "checking": "当座", "savings": "貯蓄"}
# 見間違えやすい 0/O/1/I を除いた英数字
REF_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
# 口座名義: カタカナ・長音・英大文字・数字・空白・一部の記号(全銀の名義で使われる範囲。正規化後に判定)
HOLDER_RE = r"^[ァ-ヴー A-Z0-9().,\-/・]{1,30}$"

Text30 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=30)]


class BankIn(BaseModel):
    bank_code: str = Field(pattern=r"^\d{4}$")
    bank_name: Text30
    branch_code: str = Field(pattern=r"^\d{3}$")
    branch_name: Text30
    account_type: Literal["ordinary", "checking", "savings"]
    account_number: str = Field(pattern=r"^\d{7}$")
    holder_kana: str

    @field_validator("holder_kana")
    @classmethod
    def _holder(cls, v: str) -> str:
        import re  # noqa: PLC0415

        # 半角カナ・全角英数を正規化してから判定(ゆらぎを吸収)
        n = unicodedata.normalize("NFKC", v).strip().upper()
        n = "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in n)  # ひらがな → カタカナ
        if not re.match(HOLDER_RE, n):
            raise ValueError("口座名義はカタカナ・英大文字・数字で入力してください")
        return n


class PayPayIn(BaseModel):
    paypay_id: str = Field(pattern=r"^[A-Za-z0-9_\-]{3,40}$")


class DestinationIn(BaseModel):
    kind: Literal["bank", "paypay"]
    bank: BankIn | None = None
    paypay: PayPayIn | None = None
    share_with_payers: bool = False

    @model_validator(mode="after")
    def _one(self) -> DestinationIn:
        if (self.kind == "bank") != (self.bank is not None) or (self.kind == "paypay") != (self.paypay is not None):
            raise ValueError("振込先の種類と入力内容が一致しません")
        return self

    def payload(self) -> dict[str, Any]:
        return self.bank.model_dump() if self.bank else self.paypay.model_dump()  # type: ignore[union-attr]


class SharingIn(BaseModel):
    share_with_payers: bool


class DestinationOut(BaseModel):
    id: str
    kind: str
    version: int
    masked_label: str
    share_with_payers: bool
    updated_at: str


class DestinationRevealOut(DestinationOut):
    details: dict[str, Any]


class PayerDestinationOut(BaseModel):
    available: bool
    reason: Literal["not_shared"] | None = None
    kind: str | None = None
    details: dict[str, Any] | None = None  # 支払者に必要な最小限(銀行名・支店・種別・番号・名義 / PayPay ID)
    masked_label: str | None = None
    reference_code: str | None = None
    changed_since_first_view: bool = False
    first_viewed_masked_label: str | None = None
    notice: str = ("このアプリは送金を実行しません。表示された振込先へ、ご自身の銀行アプリ等で送金してください。"
                   "照合コードは送金の目印で、支払いの証明ではありません")


def masked(kind: str, p: dict[str, Any]) -> str:
    if kind == "bank":
        return (f"{p['bank_name']} {p['branch_name']} {ACCOUNT_TYPE_LABEL[p['account_type']]} "
                f"***{p['account_number'][-4:]}")
    return f"PayPay ID ***{p['paypay_id'][-2:]}"


def _require_registered(ident: Identity) -> None:
    if ident.kind != "registered":
        raise ApiError(403, "REGISTRATION_REQUIRED", "振込先を保存するには Passkey の登録が必要です")


def _mine(db: Session, ident: Identity, *, for_update: bool = False) -> PaymentDestination | None:
    q = select(PaymentDestination).where(PaymentDestination.owner_identity_id == ident.id,
                                         PaymentDestination.erased_at.is_(None))
    if for_update:
        q = q.with_for_update()
    return db.execute(q).scalar_one_or_none()


def _version(db: Session, d: PaymentDestination, version: int | None = None) -> PaymentDestinationVersion:
    return db.execute(select(PaymentDestinationVersion).where(
        PaymentDestinationVersion.destination_id == d.id,
        PaymentDestinationVersion.version == (version or d.current_version))).scalar_one()


def _open(d: PaymentDestination, v: PaymentDestinationVersion) -> dict[str, Any]:
    if v.ciphertext is None or v.wrapped_dek is None or v.nonce is None:
        raise ApiError(410, "DESTINATION_ERASED", "この振込先は削除されました")
    try:
        return crypto.open_sealed(crypto.Sealed(v.key_version, v.wrapped_dek, v.nonce, v.ciphertext),
                                  destination_id=str(d.id), version=v.version, owner_id=str(d.owner_identity_id))
    except crypto.KeyUnavailable:
        raise _unavailable() from None
    except InvalidTag:
        # 暗号文の改ざん・別の行からの付け替え・鍵の取り違え。中身は出さずに止める
        raise ApiError(500, "DESTINATION_INTEGRITY",
                       "振込先を読み出せませんでした。受取人に直接確認してください") from None


def _unavailable() -> ApiError:
    return ApiError(503, "DESTINATIONS_UNAVAILABLE", "現在、振込先の機能を利用できません。受取人に直接確認してください")


def _seal_version(db: Session, d: PaymentDestination, version: int, body: DestinationIn) -> PaymentDestinationVersion:
    try:
        sealed = crypto.seal(body.payload(), destination_id=str(d.id), version=version,
                             owner_id=str(d.owner_identity_id))
    except crypto.KeyUnavailable:
        raise _unavailable() from None
    v = PaymentDestinationVersion(destination_id=d.id, version=version, key_version=sealed.key_version,
                                  wrapped_dek=sealed.wrapped_dek, nonce=sealed.nonce, ciphertext=sealed.ciphertext,
                                  masked_label=masked(body.kind, body.payload()))
    db.add(v)
    return v


def _out(d: PaymentDestination, v: PaymentDestinationVersion) -> DestinationOut:
    return DestinationOut(id=str(d.id), kind=d.kind, version=v.version, masked_label=v.masked_label,
                          share_with_payers=d.share_with_payers, updated_at=v.created_at.isoformat())


def _audit(db: Session, ident: Identity, action: str, target: uuid.UUID, **meta: Any) -> None:
    # 監査には ID・版だけを残す(口座番号・名義・暗号文は残さない)
    db.add(AuditEvent(actor_identity_id=ident.id, action=action, target_type="payment_destination",
                      target_id=str(target), safe_metadata=meta))


# ---------------------------------------------------------------- 本人の操作


@router.get("/account/payment-destination", response_model=DestinationOut | None)
def get_mine(db: Session = Depends(get_db), ident: Identity = Depends(current_identity)) -> DestinationOut | None:
    d = _mine(db, ident)
    return None if d is None else _out(d, _version(db, d))


@router.post("/account/payment-destination", response_model=DestinationOut, status_code=201)
def create(body: DestinationIn, request: Request, db: Session = Depends(get_db),
           ident: Identity = Depends(current_identity)) -> DestinationOut:
    require_feature("payment_destinations")
    _require_registered(ident)
    require_step_up(request, db)
    if _mine(db, ident, for_update=True) is not None:
        raise ApiError(409, "DESTINATION_EXISTS", "振込先は既に登録されています。変更してください")
    d = PaymentDestination(id=uuid.uuid4(), owner_identity_id=ident.id, kind=body.kind, current_version=1,
                           share_with_payers=body.share_with_payers)
    db.add(d)
    db.flush()
    v = _seal_version(db, d, 1, body)
    _audit(db, ident, "destination.create", d.id, version=1, shared=body.share_with_payers)
    db.flush()
    out = _out(d, v)
    db.commit()
    return out


@router.put("/account/payment-destination", response_model=DestinationOut)
def change(body: DestinationIn, request: Request, db: Session = Depends(get_db),
           ident: Identity = Depends(current_identity)) -> DestinationOut:
    """新しい版を作る(以前の版は書き換えない。支払者には変更を警告する)。"""
    require_feature("payment_destinations")
    _require_registered(ident)
    require_step_up(request, db)
    d = _mine(db, ident, for_update=True)
    if d is None:
        raise not_found()
    d.current_version += 1
    d.kind = body.kind
    d.share_with_payers = body.share_with_payers
    v = _seal_version(db, d, d.current_version, body)
    _audit(db, ident, "destination.change", d.id, version=d.current_version, shared=body.share_with_payers)
    db.flush()
    out = _out(d, v)
    db.commit()
    return out


@router.post("/account/payment-destination/sharing", response_model=DestinationOut)
def set_sharing(body: SharingIn, request: Request, db: Session = Depends(get_db),
                ident: Identity = Depends(current_identity)) -> DestinationOut:
    """共有の許可・取り消し。取り消すと、以後は支払者に表示しない(既に見た人の記憶は消せない)。"""
    require_feature("payment_destinations")
    _require_registered(ident)
    require_step_up(request, db)
    d = _mine(db, ident, for_update=True)
    if d is None:
        raise not_found()
    d.share_with_payers = body.share_with_payers
    _audit(db, ident, "destination.sharing", d.id, shared=body.share_with_payers)
    db.flush()
    out = _out(d, _version(db, d))
    db.commit()
    return out


@router.get("/account/payment-destination/reveal", response_model=DestinationRevealOut)
def reveal(request: Request, db: Session = Depends(get_db),
           ident: Identity = Depends(current_identity)) -> DestinationRevealOut:
    require_feature("payment_destinations")
    require_step_up(request, db)
    d = _mine(db, ident)
    if d is None:
        raise not_found()
    v = _version(db, d)
    details = _open(d, v)
    _audit(db, ident, "destination.reveal_own", d.id, version=v.version)
    db.commit()
    return DestinationRevealOut(**_out(d, v).model_dump(), details=details)


@router.delete("/account/payment-destination", response_model=None, status_code=204)
def erase(request: Request, db: Session = Depends(get_db), ident: Identity = Depends(current_identity)) -> None:
    """消去: 有効時は step-up を要求し、V0 停止中は所有者の不可逆な既存データ消去だけを許可する。"""
    _require_registered(ident)
    settings = get_settings()
    legacy_cleanup = not (settings.feature_payment_destinations and settings.feature_passkeys)
    if not legacy_cleanup:
        require_step_up(request, db)
    d = _mine(db, ident, for_update=True)
    if d is None:
        raise not_found()
    db.execute(update(PaymentDestinationVersion).where(PaymentDestinationVersion.destination_id == d.id)
               .values(wrapped_dek=None, nonce=None, ciphertext=None))
    d.erased_at = now()
    d.share_with_payers = False
    # V0 の例外であることを監査に残す。復号・共有・再利用できる状態は一切残さない。
    _audit(db, ident, "destination.erase", d.id, legacy_feature_disabled=legacy_cleanup)
    db.commit()


# ---------------------------------------------------------------- 精算の支払者への表示


def _reference_code() -> str:
    return "".join(secrets.choice(REF_ALPHABET) for _ in range(8))


@router.get("/settlements/{transfer_id}/destination", response_model=PayerDestinationOut)
def payer_view(transfer_id: str, db: Session = Depends(get_db),
               ident: Identity = Depends(current_identity)) -> PayerDestinationOut:
    require_feature("payment_destinations")
    t, me = load_transfer_party(db, transfer_id, ident)  # 当事者以外は 404(存在を隠す)
    if me.id != t.from_member_id:
        raise ApiError(403, "ACCESS_DENIED", "振込先を表示できるのは支払う本人だけです")
    if t.status == "superseded":
        raise ApiError(409, "TRANSFER_SUPERSEDED", "この精算案は新しい精算案に置き換えられました")
    payee = db.get(Membership, t.to_member_id)
    owner_id = payee.identity_id if payee else None
    d = None
    if owner_id is not None:
        d = db.execute(select(PaymentDestination).where(PaymentDestination.owner_identity_id == owner_id,
                                                        PaymentDestination.erased_at.is_(None))).scalar_one_or_none()
    owner = db.get(Identity, owner_id) if owner_id else None
    if d is None or not d.share_with_payers or owner is None or owner.kind != "registered":
        return PayerDestinationOut(available=False, reason="not_shared")
    v = _version(db, d)
    details = _open(d, v)
    pin = db.get(TransferDestinationPin, t.id)
    first_label = None
    if pin is None:
        pin = TransferDestinationPin(transfer_id=t.id, destination_version_id=v.id, reference_code=_reference_code())
        db.add(pin)
    elif pin.destination_version_id != v.id:
        first = db.get(PaymentDestinationVersion, pin.destination_version_id)
        first_label = first.masked_label if first else None
    _audit(db, ident, "destination.disclose", d.id, version=v.version, transfer_id=str(t.id))
    db.commit()
    return PayerDestinationOut(available=True, kind=d.kind, details=details, masked_label=v.masked_label,
                               reference_code=pin.reference_code, changed_since_first_view=first_label is not None,
                               first_viewed_masked_label=first_label)
