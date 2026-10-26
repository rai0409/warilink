"""振込先の暗号化保存と、精算の支払者だけへの限定表示(実 PostgreSQL)。"""

import base64
import json
import os
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text, update

from app.core.config import get_settings
from app.core.db import get_engine
from app.models.tables import SessionRow

from .conftest import User, equal_expense, join_as, make_group
from .test_passkeys import register
from .webauthn_soft import SoftAuthenticator

ACCOUNT = "1234567"
BANK = {"bank_code": "0001", "bank_name": "テスト銀行", "branch_code": "123", "branch_name": "本店",
        "account_type": "ordinary", "account_number": ACCOUNT, "holder_kana": "ﾃｽﾄ ﾀﾛｳ"}


def registered(u: User) -> SoftAuthenticator:
    auth = SoftAuthenticator()
    assert register(u, auth).status_code == 200
    return auth


def bank(**over):
    return {"kind": "bank", "bank": {**BANK, **over}, "share_with_payers": True}


def scenario(user_factory):
    """owner O が作成。P が受取人(登録済み)、Q が支払者、R は同じグループの当事者でないメンバー。"""
    o, p, q, r, outsider = (user_factory() for _ in range(5))
    g, ids = make_group(o, names=("Pさん", "Qさん", "Rさん"))
    pid = g["public_id"]
    for u, name in ((p, "Pさん"), (q, "Qさん"), (r, "Rさん")):
        join_as(u, o, pid, name)
    registered(p)
    p.post(f"/groups/{pid}/expenses", json=equal_expense(ids["Pさん"], 2000, [ids["Pさん"], ids["Qさん"]]))
    v = o.get(f"/groups/{pid}").json()["ledger_version"]
    batch = o.post(f"/groups/{pid}/settlement-batches", json={"expected_ledger_version": v}).json()
    (t,) = batch["transfers"]
    assert t["from_member_id"] == ids["Qさん"] and t["to_member_id"] == ids["Pさん"]
    return o, p, q, r, outsider, pid, ids, t


def raw_dump() -> str:
    """暗号化対象の表と監査ログを丸ごと文字列にする(平文が残っていないかを調べる)。"""
    with get_engine().connect() as c:
        rows = []
        for table in ("payment_destinations", "payment_destination_versions", "transfer_destination_pins",
                      "audit_events"):
            rows += [json.dumps(dict(r._mapping), default=lambda x: x.hex() if isinstance(x, bytes) else str(x),
                                ensure_ascii=False) for r in c.execute(text(f"SELECT * FROM {table}"))]
    return "\n".join(rows)


def _identity_id(u: User) -> uuid.UUID:
    return uuid.UUID(u.get("/identity/me").json()["id"])


def _set_session(u: User, **values) -> None:
    """Passkey の再認証を行わずに拒否経路を確認するため、当該テスト利用者のセッションだけを古くする。"""
    with get_engine().begin() as c:
        c.execute(update(SessionRow).where(SessionRow.identity_id == _identity_id(u)).values(**values))


@contextmanager
def _feature_flags(monkeypatch, *, payment_destinations: bool, passkeys: bool):
    """V0 の停止状態を一時的に再現し、設定キャッシュを必ず元に戻す。"""
    with monkeypatch.context() as m:
        m.setenv("WARILINK_FEATURE_PAYMENT_DESTINATIONS", str(payment_destinations).lower())
        m.setenv("WARILINK_FEATURE_PASSKEYS", str(passkeys).lower())
        get_settings.cache_clear()
        yield
    get_settings.cache_clear()


def test_registration_requires_passkey_account_and_recent_step_up(user_factory):
    g = user_factory()
    r = g.post("/account/payment-destination", json=bank())
    assert r.status_code == 403 and r.json()["code"] == "REGISTRATION_REQUIRED"
    u = user_factory()
    registered(u)
    with get_engine().begin() as c:  # 確認から時間が経ったセッション
        c.execute(update(SessionRow).values(created_at=datetime.now(UTC) - timedelta(hours=1)))
    r = u.post("/account/payment-destination", json=bank())
    assert r.status_code == 401 and r.json()["code"] == "STEP_UP_REQUIRED"
    assert u.get("/account/payment-destination/reveal").json()["code"] == "STEP_UP_REQUIRED"


def test_create_masks_and_never_stores_plaintext(user_factory):
    u = user_factory()
    registered(u)
    r = u.post("/account/payment-destination", json=bank())
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["masked_label"] == "テスト銀行 本店 普通 ***4567" and "details" not in d
    assert u.post("/account/payment-destination", json=bank()).json()["code"] == "DESTINATION_EXISTS"
    dump = raw_dump()
    assert ACCOUNT not in dump and "テスト タロウ" not in dump and "ﾃｽﾄ" not in dump
    full = u.get("/account/payment-destination/reveal").json()
    assert full["details"]["account_number"] == ACCOUNT and full["details"]["holder_kana"] == "テスト タロウ"


@pytest.mark.parametrize("over", [{"bank_code": "12"}, {"branch_code": "12a"}, {"account_number": "12345"},
                                  {"holder_kana": "山田太郎"}, {"account_type": "foreign"}])
def test_bank_input_validation(user_factory, over):
    u = user_factory()
    registered(u)
    assert u.post("/account/payment-destination", json=bank(**over)).status_code == 422


def test_only_the_payer_of_the_transfer_can_see_destination(user_factory):
    o, p, q, r, outsider, pid, ids, t = scenario(user_factory)
    assert p.post("/account/payment-destination", json=bank()).status_code == 201
    path = f"/settlements/{t['id']}/destination"
    # 支払者 Q だけが見られる
    v = q.get(path).json()
    assert v["available"] and v["details"]["account_number"] == ACCOUNT and len(v["reference_code"]) == 8
    assert "送金を実行しません" in v["notice"]
    # オーナー・同じグループの非当事者・外部の人は存在も分からない(404)、受取人本人は 403
    for who in (o, r, outsider):
        assert who.get(path).status_code == 404
    assert p.get(path).status_code == 403
    # 存在しない送金 ID・不正な ID も 404
    assert q.get("/settlements/00000000-0000-0000-0000-000000000000/destination").status_code == 404
    assert q.get("/settlements/not-a-uuid/destination").status_code == 404
    # 監査ログには ID と版だけ
    dump = raw_dump()
    assert "destination.disclose" in dump and ACCOUNT not in dump


def test_change_warns_payer_and_keeps_reference_code(user_factory):
    o, p, q, r, outsider, pid, ids, t = scenario(user_factory)
    p.post("/account/payment-destination", json=bank())
    path = f"/settlements/{t['id']}/destination"
    first = q.get(path).json()
    assert not first["changed_since_first_view"]
    r2 = p.put("/account/payment-destination", json=bank(account_number="7654321", branch_name="駅前"))
    assert r2.json()["version"] == 2
    second = q.get(path).json()
    assert second["changed_since_first_view"] and second["first_viewed_masked_label"] == "テスト銀行 本店 普通 ***4567"
    assert second["details"]["account_number"] == "7654321"
    assert second["reference_code"] == first["reference_code"]


def test_sharing_revoke_and_erase(user_factory):
    o, p, q, r, outsider, pid, ids, t = scenario(user_factory)
    path = f"/settlements/{t['id']}/destination"
    assert q.get(path).json() == {**q.get(path).json(), "available": False, "reason": "not_shared"}  # 未登録
    p.post("/account/payment-destination", json={**bank(), "share_with_payers": False})
    assert q.get(path).json()["available"] is False  # 共有を許可していない
    p.post("/account/payment-destination/sharing", json={"share_with_payers": True})
    assert q.get(path).json()["available"] is True
    p.post("/account/payment-destination/sharing", json={"share_with_payers": False})  # 取り消し
    assert q.get(path).json()["available"] is False
    assert p.delete("/account/payment-destination").status_code == 204
    assert q.get(path).json()["available"] is False
    assert p.get("/account/payment-destination").json() is None
    with get_engine().connect() as c:
        left = c.execute(text("SELECT count(*) FROM payment_destination_versions WHERE ciphertext IS NOT NULL "
                              "OR wrapped_dek IS NOT NULL")).scalar_one()
    assert left == 0  # 消去後は復号に必要なものが残らない


def test_sharing_requires_fresh_step_up_before_any_mutation(user_factory):
    u = user_factory()
    registered(u)
    assert u.post("/account/payment-destination", json={**bank(), "share_with_payers": False}).status_code == 201

    # Passkey 以外の有効セッションでは、許可も取り消しも状態を変えられない。
    _set_session(u, assurance_level="guest")
    r = u.post("/account/payment-destination/sharing", json={"share_with_payers": True})
    assert r.status_code == 401 and r.json()["code"] == "STEP_UP_REQUIRED"
    assert u.get("/account/payment-destination").json()["share_with_payers"] is False

    _set_session(u, assurance_level="passkey", created_at=datetime.now(UTC))
    assert u.post("/account/payment-destination/sharing", json={"share_with_payers": True}).status_code == 200
    assert u.get("/account/payment-destination").json()["share_with_payers"] is True

    _set_session(u, created_at=datetime.now(UTC) - timedelta(hours=1))
    r = u.post("/account/payment-destination/sharing", json={"share_with_payers": False})
    assert r.status_code == 401 and r.json()["code"] == "STEP_UP_REQUIRED"
    assert u.get("/account/payment-destination").json()["share_with_payers"] is True

    _set_session(u, created_at=datetime.now(UTC))
    assert u.post("/account/payment-destination/sharing", json={"share_with_payers": False}).status_code == 200
    assert u.get("/account/payment-destination").json()["share_with_payers"] is False


def test_enabled_destination_erasure_requires_fresh_step_up(user_factory):
    u = user_factory()
    registered(u)
    assert u.post("/account/payment-destination", json=bank()).status_code == 201

    _set_session(u, created_at=datetime.now(UTC) - timedelta(hours=1))
    r = u.delete("/account/payment-destination")
    assert r.status_code == 401 and r.json()["code"] == "STEP_UP_REQUIRED"
    with get_engine().connect() as c:
        encrypted = c.execute(
            text("SELECT count(*) FROM payment_destination_versions WHERE ciphertext IS NOT NULL")
        ).scalar_one()
    assert encrypted == 1

    _set_session(u, created_at=datetime.now(UTC))
    assert u.delete("/account/payment-destination").status_code == 204
    with get_engine().connect() as c:
        encrypted = c.execute(
            text("SELECT count(*) FROM payment_destination_versions WHERE ciphertext IS NOT NULL")
        ).scalar_one()
    assert encrypted == 0


def test_v0_only_allows_owner_to_cryptographically_erase_legacy_destination(user_factory, monkeypatch):
    owner, other, guest = user_factory(), user_factory(), user_factory()
    registered(owner)
    registered(other)
    assert owner.post("/account/payment-destination", json=bank()).status_code == 201
    _set_session(owner, created_at=datetime.now(UTC) - timedelta(hours=1))

    with _feature_flags(monkeypatch, payment_destinations=False, passkeys=False):
        # V0 は復号・作成・変更・共有・支払者への開示を止める。
        for method, path, body in (
            (other.post, "/account/payment-destination", bank()),
            (owner.put, "/account/payment-destination", bank(account_number="7654321")),
            (owner.post, "/account/payment-destination/sharing", {"share_with_payers": False}),
        ):
            r = method(path, json=body)
            assert r.status_code == 503 and r.json()["code"] == "FEATURE_DISABLED"
        assert owner.get("/account/payment-destination/reveal").json()["code"] == "FEATURE_DISABLED"
        disclosure = owner.get("/settlements/00000000-0000-0000-0000-000000000000/destination")
        assert disclosure.json()["code"] == "FEATURE_DISABLED"

        # 所有者以外、ゲスト、CSRF 不備はいずれも既存暗号文を消せない。
        assert other.delete("/account/payment-destination").status_code == 404
        assert guest.delete("/account/payment-destination").json()["code"] == "REGISTRATION_REQUIRED"
        csrf = owner.c.delete("/api/v1/account/payment-destination", headers={"X-WariLink-CSRF": "0"})
        assert csrf.status_code == 403 and csrf.json()["code"] == "CSRF_REJECTED"
        with get_engine().connect() as c:
            encrypted = c.execute(
                text("SELECT count(*) FROM payment_destination_versions WHERE ciphertext IS NOT NULL")
            ).scalar_one()
        assert encrypted == 1

        # 停止中の例外は、古い Passkey セッションでも所有者による不可逆消去だけを許可する。
        assert owner.delete("/account/payment-destination").status_code == 204
        dump = raw_dump()
        assert ACCOUNT not in dump and '"legacy_feature_disabled": true' in dump
        with get_engine().connect() as c:
            row = c.execute(text("SELECT wrapped_dek, nonce, ciphertext FROM payment_destination_versions")).one()
        assert row == (None, None, None)

    # 機能を再度有効にしても、消去済みの秘密は読み出せない。
    assert owner.get("/account/payment-destination/reveal").status_code == 404


def test_superseded_transfer_does_not_disclose(user_factory):
    o, p, q, r, outsider, pid, ids, t = scenario(user_factory)
    p.post("/account/payment-destination", json=bank())
    p.post(f"/groups/{pid}/expenses", json=equal_expense(ids["Pさん"], 1000, [ids["Pさん"], ids["Qさん"]]))
    v = o.get(f"/groups/{pid}").json()["ledger_version"]
    assert o.post(f"/groups/{pid}/settlement-batches", json={"expected_ledger_version": v}).status_code == 201
    r2 = q.get(f"/settlements/{t['id']}/destination")
    assert r2.status_code == 409 and r2.json()["code"] == "TRANSFER_SUPERSEDED"


def test_ciphertext_cannot_be_moved_to_another_row(user_factory):
    a, b = user_factory(), user_factory()
    registered(a)
    registered(b)
    a.post("/account/payment-destination", json=bank())
    b.post("/account/payment-destination", json=bank(account_number="7777777"))
    with get_engine().begin() as c:  # A の暗号文を B の行へ付け替える(DB を奪われた想定)
        c.execute(text("""UPDATE payment_destination_versions SET ciphertext = src.ciphertext,
                          wrapped_dek = src.wrapped_dek, nonce = src.nonce
                          FROM (SELECT v.ciphertext, v.wrapped_dek, v.nonce FROM payment_destination_versions v
                                JOIN payment_destinations d ON d.id = v.destination_id
                                JOIN identities i ON i.id = d.owner_identity_id ORDER BY d.created_at LIMIT 1) src
                          WHERE payment_destination_versions.id = (SELECT v.id FROM payment_destination_versions v
                                JOIN payment_destinations d ON d.id = v.destination_id
                                ORDER BY d.created_at DESC LIMIT 1)"""))
    r = b.get("/account/payment-destination/reveal")
    assert r.status_code == 500 and r.json()["code"] == "DESTINATION_INTEGRITY" and ACCOUNT not in r.text


def _key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def test_key_rotation_rewraps_and_old_key_can_be_removed(user_factory, monkeypatch):
    from app.modules.payment_destinations import cli

    k1, k2 = _key(), _key()
    monkeypatch.setenv("WARILINK_DESTINATION_KEYS", json.dumps({"v1": k1}))
    monkeypatch.setenv("WARILINK_DESTINATION_ACTIVE_KEY", "v1")
    get_settings.cache_clear()
    try:
        u = user_factory()
        registered(u)
        assert u.post("/account/payment-destination", json=bank()).status_code == 201
        assert cli.main(["status"]) == 0
        monkeypatch.setenv("WARILINK_DESTINATION_KEYS", json.dumps({"v1": k1, "v2": k2}))
        monkeypatch.setenv("WARILINK_DESTINATION_ACTIVE_KEY", "v2")
        get_settings.cache_clear()
        from sqlalchemy.orm import Session

        with Session(get_engine()) as db:
            assert cli.rotate(db, dry_run=True)["rewrapped"] == 1
            assert cli.rotate(db, dry_run=False)["rewrapped"] == 1
            assert cli.status(db)["versions_by_key"] == {"v2": 1}
        monkeypatch.setenv("WARILINK_DESTINATION_KEYS", json.dumps({"v2": k2}))  # 古い鍵を外す
        get_settings.cache_clear()
        assert u.get("/account/payment-destination/reveal").json()["details"]["account_number"] == ACCOUNT
        # 鍵を失うと読み出せない(DB だけでは復号できない = バックアップと鍵の分離)
        monkeypatch.setenv("WARILINK_DESTINATION_KEYS", json.dumps({"v3": _key()}))
        monkeypatch.setenv("WARILINK_DESTINATION_ACTIVE_KEY", "v3")
        get_settings.cache_clear()
        r = u.get("/account/payment-destination/reveal")
        assert r.status_code == 503 and r.json()["code"] == "DESTINATIONS_UNAVAILABLE"
    finally:
        get_settings.cache_clear()


def test_production_without_keys_disables_feature_and_dev_key_is_rejected(monkeypatch):
    from app.core.config import Settings, production_problems
    from app.modules.payment_destinations import crypto

    prod = Settings(_env_file=None, environment="production", cookie_secure=True,
                    allowed_origins=["https://warilink.example"], database_url="postgresql+psycopg://a:b@h/x")
    with pytest.raises(crypto.KeyUnavailable):
        crypto.keyring(prod)  # 鍵なし → 機能停止(503)
    dev = base64.b64encode(crypto.DEV_KEY).decode()
    bad = prod.model_copy(update={"destination_keys": {"x": dev}, "destination_active_key": "x"})
    assert any("開発用の鍵" in m for m in production_problems(bad))
    broken = prod.model_copy(update={"destination_keys": {"x": "short"}, "destination_active_key": "x"})
    assert any("DESTINATION_KEYS" in m for m in production_problems(broken))
