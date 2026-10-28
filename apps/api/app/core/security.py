"""ゲストセッション・CSRF・アクセス制御の共通処理。

- セッショントークンは 256bit 乱数。DB には SHA-256 ハッシュのみ保存する。
- Cookie は httpOnly / SameSite=Lax / (本番) Secure。
- 状態変更リクエストには独自ヘッダー `X-WariLink-CSRF` を必須にする(クロスサイトの
  単純リクエストでは付与できないため、CORS 許可オリジン以外からの CSRF を防ぐ)。
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import math
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Depends, Request, Response
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models.tables import Group, Identity, Membership, RateLimitEvent, SessionRow, SettlementTransfer

from .config import get_settings
from .db import get_db, get_engine
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


# ---------------------------------------------------------------- 濫用対策の回数制限(STEP3)
#
# - 1 つの操作を、接続元 IP・本人(identity)・対象(招待 token)・全体の複数の単位で同時に数える。
#   Cookie を作り直す・IP を変える・別のアカウントを使う、のどれか 1 つだけでは上限を回避できない。
# - 記録は rate_limit_events(PostgreSQL)。複数ワーカー・複数プロセスでも、単位ごとの advisory lock で
#   「数える → 記録する」を直列化するので、同時に来ても上限を超えない。再起動しても記録は残る。
# - IP・ID・token はそのまま保存せず、WARILINK_RATE_LIMIT_SECRET の HMAC の先頭 32 文字だけを残す。ログにも出さない。
# - 期間を過ぎた行は記録のたびに消す(保存量は「上限 × 使われた鍵の数」で頭打ち)。
# - 接続元は request.client.host。WARILINK_TRUSTED_PROXIES に入った中継からの接続のときだけ X-Forwarded-For を
#   右から見て最初の信頼しないアドレスを使う(ヘッダーの偽装で回避されないように)。IPv6 は /64 ごとに数える。

# 名前 → (上限回数, 期間の秒数)。WARILINK_RATE_LIMIT_OVERRIDES で上書きできる(本番は範囲を検証)
RATE_LIMITS: dict[str, tuple[int, int]] = {
    "guest_create:ip": (20, 3600),  # Cookie を消して作り直すゲストも含む
    "guest_create:global": (600, 60),
    "invite_create:identity": (30, 3600),
    "invite_create:ip": (60, 3600),
    "invite_access:ip": (60, 600),  # 招待の内容確認と参加
    "invite_access:identity": (30, 600),
    "invite_access:token": (200, 600),
    "invite_miss:ip": (20, 600),  # 無効・期限切れ・存在しない招待(推測の試行)
    "passkey_register:identity": (10, 600),
    "passkey_register:ip": (20, 600),
    "passkey_login:ip": (30, 600),
    "passkey_login:global": (1200, 60),
    "passkey_fail:ip": (10, 600),  # Passkey の検証の失敗(登録・ログイン)
    "passkey_fail:identity": (10, 600),
}
_DEV_RATE_SECRET = "warilink-dev-only-rate-limit-secret"  # 開発・テスト用(本番は設定が必須)
_PRUNE_BATCH = 500


class RateLimited(ApiError):
    """429。Retry-After ヘッダーは app/main.py の例外ハンドラーで付ける。"""

    def __init__(self, retry_after: int) -> None:
        super().__init__(429, "RATE_LIMITED", "短時間に操作が集中しています。しばらく待ってからもう一度お試しください",
                         retry_after_seconds=retry_after)
        self.retry_after = retry_after


def _rate_clock() -> datetime:  # テストで固定時刻に差し替える
    return now()


def _in_networks(addr: str, networks: list[str]) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in ipaddress.ip_network(n, strict=False) for n in networks)


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    trusted = get_settings().trusted_proxies
    if trusted and _in_networks(peer, trusted):
        hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
        for hop in reversed(hops):
            if not _in_networks(hop, trusted):
                return hop
    return peer


def _ip_unit(addr: str) -> str:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return addr or "unknown"
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return str(ipaddress.ip_network(f"{ip}/64", strict=False)) if ip.version == 6 else str(ip)


def _key(kind: str, value: str) -> str:
    secret = (get_settings().rate_limit_secret or _DEV_RATE_SECRET).encode()
    return hmac.new(secret, f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()[:32]


def rate_limit_keys(request: Request, *, identity: Identity | None = None, token: str | None = None) -> dict[str, str]:
    keys = {"ip": _key("ip", _ip_unit(client_ip(request))), "global": "global"}
    if identity is not None:
        keys["identity"] = _key("identity", str(identity.id))
    if token is not None:
        keys["token"] = _key("token", token)
    return keys


def _rules() -> dict[str, tuple[int, int]]:
    return {**RATE_LIMITS, **{k: tuple(v) for k, v in get_settings().rate_limit_overrides.items()}}  # type: ignore[misc]


def _buckets(actions: tuple[str, ...], keys: dict[str, str]) -> list[tuple[str, str, int, int]]:
    rules = _rules()
    out = []
    for action in actions:
        for scope, key in keys.items():
            name = f"{action}:{scope}"
            if name in rules:
                out.append((name, key, *rules[name]))
    return sorted(out)  # ロックの順番を固定する(デッドロック防止)


def rate_limit(request: Request, action: str | None, *, identity: Identity | None = None, token: str | None = None,
               check: tuple[str, ...] = ()) -> None:
    """`action` と `check` の各単位の上限を確かめ、超えていれば 429(何も記録しない)。通れば `action` を 1 回記録する。

    API の処理とは別のトランザクションで記録する(処理が失敗しても試行は数える)。`action=None` は確かめるだけ。
    """
    if not get_settings().rate_limits_active:
        return
    keys = rate_limit_keys(request, identity=identity, token=token)
    record = _buckets((action,), keys) if action else []
    checked = _buckets((*((action,) if action else ()), *check), keys)
    t = _rate_clock()
    with Session(get_engine()) as db:
        for bucket, key, _limit, _window in checked:
            db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"{bucket}|{key}", 0))))
        for bucket, key, limit, window in checked:
            since = t - timedelta(seconds=window)
            n, oldest = db.execute(
                select(func.count(), func.min(RateLimitEvent.created_at))
                .where(RateLimitEvent.bucket == bucket, RateLimitEvent.key_hash == key,
                       RateLimitEvent.created_at > since)
            ).one()
            if n >= limit:
                db.rollback()
                reset = (oldest + timedelta(seconds=window) - t).total_seconds() if oldest else window
                raise RateLimited(max(1, math.ceil(reset)))
        for bucket, key, _limit, window in record:
            db.execute(delete(RateLimitEvent).where(RateLimitEvent.bucket == bucket, RateLimitEvent.key_hash == key,
                                                    RateLimitEvent.created_at <= t - timedelta(seconds=window)))
            db.add(RateLimitEvent(bucket=bucket, key_hash=key, created_at=t))
        prune_rate_limit_events(db, at=t, commit=False)
        db.commit()


def record_rate_event(request: Request, action: str, *, identity: Identity | None = None,
                      token: str | None = None) -> None:
    """確かめずに 1 回記録する(検証の失敗・無効な招待など、処理の結果で数えるもの)。"""
    if not get_settings().rate_limits_active:
        return
    keys = rate_limit_keys(request, identity=identity, token=token)
    t = _rate_clock()
    with Session(get_engine()) as db:
        for bucket, key, _limit, _window in _buckets((action,), keys):
            db.add(RateLimitEvent(bucket=bucket, key_hash=key, created_at=t))
        db.commit()


def prune_rate_limit_events(db: Session, *, at: datetime | None = None, commit: bool = True) -> int:
    """一番長い期間を過ぎた行を、一度に最大 _PRUNE_BATCH 件消す(記録のたびに呼ぶので溜まり続けない)。"""
    longest = max(w for _l, w in _rules().values())
    cutoff = (at or _rate_clock()) - timedelta(seconds=longest)
    ids = select(RateLimitEvent.id).where(RateLimitEvent.created_at < cutoff).limit(_PRUNE_BATCH)
    n = db.execute(delete(RateLimitEvent).where(RateLimitEvent.id.in_(ids))).rowcount
    if commit:
        db.commit()
    return int(n or 0)
