"""STEP4: 退会・グループからの離脱・招待の失効・期限切れの一時データの削除。"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_engine
from app.core.security import now
from app.modules.identity import account, retention
from app.ocr import jobs
from app.ocr.cli import main as cli

from .conftest import User, equal_expense, join_as, make_group
from .test_destinations import ACCOUNT, _set_session, bank, registered
from .test_passkeys import device, login
from .test_receipts import png, upload


@pytest.fixture
def receipt_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("WARILINK_RECEIPT_STORAGE_DIR", str(tmp_path / "receipts"))
    get_settings.cache_clear()
    (tmp_path / "receipts").mkdir()
    with Session(get_engine()) as db:
        jobs.heartbeat(db, "test-worker", "fixed", "0", datetime.now(UTC))
    yield tmp_path / "receipts"
    get_settings.cache_clear()


def _scalar(sql: str, **params):
    with get_engine().connect() as c:
        return c.execute(text(sql), params).scalar_one()


def _ledger(owner: User, pid: str) -> dict:
    """他のメンバーから見た金額(残高・精算案)。退会・離脱の前後で変わってはいけない。"""
    g = owner.get(f"/groups/{pid}").json()
    plan = owner.post(f"/groups/{pid}/settlements/preview").json()
    return {"ledger_version": g["ledger_version"],
            "balances": sorted((b["member_id"], b["net_minor"]) for b in plan["balances"]),
            "transfers": sorted((t["from_member_id"], t["to_member_id"], t["amount_minor"]) for t in plan["transfers"])}


def _scenario(user_factory):
    owner, member = user_factory(), user_factory()
    g, ids = make_group(owner)
    pid = g["public_id"]
    join_as(member, owner, pid, "Bさん")
    body = equal_expense(ids["Bさん"], 3000, [ids["Aさん"], ids["Bさん"], ids["Cさん"]])
    assert member.post(f"/groups/{pid}/expenses", json=body).status_code == 201
    return owner, member, pid, ids


def delete_me(u: User, **kw):
    return u.c.request("DELETE", "/api/v1/account", json={"confirm": True}, **kw)


# ---------------------------------------------------------------- 退会

def test_guest_can_delete_own_account_and_shared_ledger_is_kept(user_factory):
    owner, member, pid, ids = _scenario(user_factory)
    before = _ledger(owner, pid)
    r = delete_me(member)
    assert r.status_code == 200 and r.json() == {"groups_left": 1, "destination_erased": False}
    # 本人はもう使えない(セッション失効・Cookie 削除)
    assert member.get("/identity/me").json() is None
    assert member.get(f"/groups/{pid}").status_code == 401
    # 他のメンバーから見た金額・履歴は変わらず、名前だけのメンバー行が残る
    assert _ledger(owner, pid) == before
    b = next(m for m in owner.get(f"/groups/{pid}").json()["members"] if m["id"] == ids["Bさん"])
    assert b["display_name"] == "Bさん" and b["claimed"] is False
    meta = _scalar("SELECT safe_metadata FROM audit_events WHERE action = 'identity.delete'")
    assert meta == {"groups_left": 1, "destination_erased": False, "kind": "guest"}


def test_delete_requires_explicit_confirmation_and_csrf(user_factory):
    u = user_factory()
    assert u.c.request("DELETE", "/api/v1/account", json={}).status_code == 422
    assert u.c.request("DELETE", "/api/v1/account", json={"confirm": False}).status_code == 422
    r = u.c.request("DELETE", "/api/v1/account", json={"confirm": True}, headers={"X-WariLink-CSRF": "0"})
    assert r.status_code == 403 and r.json()["code"] == "CSRF_REJECTED"
    assert u.get("/identity/me").json() is not None  # どれも何も変えない


def test_registered_user_needs_fresh_step_up_and_everything_is_revoked(user_factory):
    owner, member, pid, ids = _scenario(user_factory)
    auth = registered(member)
    assert member.post("/account/payment-destination", json=bank()).status_code == 201
    token = member.post(f"/groups/{pid}/invites", json={})
    assert token.status_code == 403  # 参加者は招待を作れない(作成者だけ)
    own_g, _ = make_group(member, title="自分の旅行")
    own_inv = member.post(f"/groups/{own_g['public_id']}/invites", json={}).json()["token"]

    _set_session(member, created_at=datetime.now(UTC) - timedelta(hours=1))
    r = delete_me(member)
    assert r.status_code == 401 and r.json()["code"] == "STEP_UP_REQUIRED"
    assert member.get("/identity/me").json() is not None  # 拒否したときは何も変えない
    assert _scalar("SELECT count(*) FROM payment_destination_versions WHERE ciphertext IS NOT NULL") == 1

    _set_session(member, created_at=datetime.now(UTC))
    r = delete_me(member)
    assert r.status_code == 200 and r.json() == {"groups_left": 2, "destination_erased": True}
    assert _scalar("SELECT count(*) FROM payment_destination_versions WHERE ciphertext IS NOT NULL") == 0
    assert _scalar("SELECT count(*) FROM passkey_credentials WHERE revoked_at IS NULL") == 0
    assert _scalar("SELECT count(*) FROM sessions s JOIN identities i ON i.id = s.identity_id "
                   "WHERE i.disabled_at IS NOT NULL AND s.revoked_at IS NULL") == 0
    other = user_factory()
    assert other.get(f"/invites/{own_inv}").json()["code"] == "INVITE_INVALID"  # 本人が作った招待は失効
    # 別の端末から同じ Passkey でログインしようとしても使えない
    assert login(device(), auth).status_code == 400
    assert ACCOUNT not in json.dumps(_scalar("SELECT json_agg(a) FROM audit_events a"), ensure_ascii=False)


def test_failure_midway_changes_nothing(user_factory, monkeypatch):
    owner, member, pid, ids = _scenario(user_factory)
    registered(member)
    assert member.post("/account/payment-destination", json=bank()).status_code == 201

    def boom(db, d):
        raise RuntimeError("途中で失敗")

    monkeypatch.setattr(account, "crypto_erase", boom)
    with pytest.raises(RuntimeError):
        delete_me(member)
    # トランザクションごと巻き戻る: セッション・Passkey・メンバーの結び付き・暗号文はそのまま
    assert member.get("/identity/me").json()["kind"] == "registered"
    assert member.get(f"/groups/{pid}").status_code == 200
    assert _scalar("SELECT count(*) FROM passkey_credentials WHERE revoked_at IS NULL") == 1
    assert _scalar("SELECT count(*) FROM payment_destination_versions WHERE ciphertext IS NOT NULL") == 1
    assert _scalar("SELECT count(*) FROM audit_events WHERE action = 'identity.delete'") == 0


def test_deleting_one_account_never_touches_another(user_factory):
    owner, member, pid, ids = _scenario(user_factory)
    registered(owner)
    assert owner.post("/account/payment-destination", json=bank()).status_code == 201
    assert delete_me(member).status_code == 200
    assert owner.get("/identity/me").json()["kind"] == "registered"
    assert owner.get(f"/groups/{pid}").status_code == 200
    assert _scalar("SELECT count(*) FROM payment_destination_versions WHERE ciphertext IS NOT NULL") == 1


# ---------------------------------------------------------------- グループからの離脱・招待の失効

def test_member_can_leave_group_and_loses_access(user_factory):
    owner, member, pid, ids = _scenario(user_factory)
    before = _ledger(owner, pid)
    assert member.post(f"/groups/{pid}/leave").status_code == 204
    assert member.get(f"/groups/{pid}").status_code == 404  # 抜けた後は見られない(存在も隠す)
    assert member.post(f"/groups/{pid}/leave").status_code == 404
    assert _ledger(owner, pid) == before
    b = next(m for m in owner.get(f"/groups/{pid}").json()["members"] if m["id"] == ids["Bさん"])
    assert b["claimed"] is False
    assert member.get("/identity/me").json() is not None  # アカウントはそのまま


def test_owner_leaving_hands_over_to_earliest_member_and_outsider_gets_404(user_factory):
    owner, member, pid, ids = _scenario(user_factory)
    assert user_factory().post(f"/groups/{pid}/leave").status_code == 404
    assert owner.post(f"/groups/{pid}/leave").status_code == 204
    assert owner.get(f"/groups/{pid}").status_code == 404
    g = member.get(f"/groups/{pid}").json()
    assert g["is_owner"] is True  # 参加の早い参加者(B)が作成者を引き継ぐ
    assert member.post(f"/groups/{pid}/invites", json={}).status_code == 201


def test_owner_revokes_all_invites(user_factory):
    owner, member, pid, ids = _scenario(user_factory)
    tokens = [owner.post(f"/groups/{pid}/invites", json={}).json()["token"] for _ in range(2)]
    assert member.post(f"/groups/{pid}/invites/revoke").status_code == 403  # 参加者は失効できない
    assert user_factory().post(f"/groups/{pid}/invites/revoke").status_code == 404
    r = owner.post(f"/groups/{pid}/invites/revoke")
    assert r.status_code == 200 and r.json()["revoked"] >= 2
    newcomer = user_factory()
    for t in tokens:
        assert newcomer.get(f"/invites/{t}").json()["code"] == "INVITE_INVALID"
    assert member.get(f"/groups/{pid}").status_code == 200  # 参加済みのメンバーはそのまま
    assert owner.post(f"/groups/{pid}/invites/revoke").json()["revoked"] == 0


# ---------------------------------------------------------------- 期限切れの一時データ

def test_expired_and_used_challenges_are_purged_idempotently(user_factory):
    for _ in range(3):
        assert device().post("/api/v1/auth/passkeys/login/options").status_code == 200
    assert _scalar("SELECT count(*) FROM webauthn_challenges") == 3
    with Session(get_engine()) as db:
        assert account.purge_expired_challenges(db) == 0  # まだ期限内・未使用なので消さない
        later = datetime.now(UTC) + timedelta(hours=1)
        assert account.purge_expired_challenges(db, at=later) == 3
        assert account.purge_expired_challenges(db, at=later) == 0  # 何度実行しても同じ結果


def test_purge_job_reports_deleted_challenges(user_factory, capsys):
    assert device().post("/api/v1/auth/passkeys/login/options").status_code == 200
    with get_engine().begin() as c:
        c.execute(text("UPDATE webauthn_challenges SET expires_at = now() - interval '1 minute'"))
    assert cli(["purge-images", "--json"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["webauthn_challenges_deleted"] == 1
    assert cli(["purge-images", "--json"]) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["webauthn_challenges_deleted"] == 0


# ---------------------------------------------------------------- 作成者の引き継ぎ・終了・参加者不在

def test_owner_can_hand_over_explicitly(user_factory):
    owner, member, pid, ids = _scenario(user_factory)
    assert member.post(f"/groups/{pid}/owner", json={"member_id": ids["Aさん"]}).status_code == 403
    r = owner.post(f"/groups/{pid}/owner", json={"member_id": ids["Cさん"]})  # 未参加の名前には渡せない
    assert r.status_code == 422
    r = owner.post(f"/groups/{pid}/owner", json={"member_id": ids["Bさん"]})
    assert r.status_code == 200 and r.json()["is_owner"] is False
    assert member.get(f"/groups/{pid}").json()["is_owner"] is True


def test_deleting_the_owner_account_hands_over_the_group(user_factory):
    owner, member, pid, ids = _scenario(user_factory)
    assert delete_me(owner).status_code == 200
    assert member.get(f"/groups/{pid}").json()["is_owner"] is True


def test_closed_group_is_read_only_and_revokes_invites(user_factory):
    owner, member, pid, ids = _scenario(user_factory)
    token = owner.post(f"/groups/{pid}/invites", json={}).json()["token"]
    assert member.post(f"/groups/{pid}/close").status_code == 403
    r = owner.post(f"/groups/{pid}/close")
    assert r.status_code == 200 and r.json()["closed"] is True
    before = _ledger(owner, pid)
    body = equal_expense(ids["Aさん"], 1000, [ids["Aさん"], ids["Bさん"]])
    v = owner.get(f"/groups/{pid}").json()["ledger_version"]
    for res in (owner.post(f"/groups/{pid}/expenses", json=body),
                owner.post(f"/groups/{pid}/members", json={"names": ["Eさん"]}),
                owner.post(f"/groups/{pid}/invites", json={}),
                owner.post(f"/groups/{pid}/settlement-batches", json={"expected_ledger_version": v}),
                owner.post(f"/groups/{pid}/close")):
        assert res.status_code == 409 and res.json()["code"] == "GROUP_CLOSED"
    assert user_factory().get(f"/invites/{token}").json()["code"] == "INVITE_INVALID"
    assert member.get(f"/groups/{pid}").json()["closed"] is True  # 閲覧はできる
    assert _ledger(owner, pid) == before


def test_last_member_leaving_orphans_group_and_join_cancels_it(user_factory):
    owner, member, pid, ids = _scenario(user_factory)
    token = owner.post(f"/groups/{pid}/invites", json={}).json()["token"]
    assert member.post(f"/groups/{pid}/leave").status_code == 204
    assert owner.post(f"/groups/{pid}/leave").status_code == 204
    assert _scalar("SELECT orphaned_at IS NOT NULL FROM groups WHERE public_id = :p", p=pid) is True
    newcomer = user_factory()
    preview = newcomer.get(f"/invites/{token}").json()
    mid = next(m["id"] for m in preview["unclaimed_members"] if m["display_name"] == "Cさん")
    assert newcomer.post(f"/invites/{token}/join", json={"claim_member_id": mid}).status_code == 200
    assert _scalar("SELECT orphaned_at IS NULL FROM groups WHERE public_id = :p", p=pid) is True


def test_orphaned_group_is_fully_deleted_after_30_days(user_factory, receipt_storage):
    storage = receipt_storage
    owner, member, pid, ids = _scenario(user_factory)
    assert upload(owner, pid, png()).status_code == 201
    assert len(list(storage.iterdir())) == 1
    for u in (member, owner):
        assert u.post(f"/groups/{pid}/leave").status_code == 204
    with Session(get_engine()) as db:
        assert retention.purge_retention(db, at=now() + timedelta(days=29))["groups"] == 0
        assert retention.purge_retention(db, at=now() + timedelta(days=31))["groups"] == 1
        assert _scalar("SELECT count(*) FROM deletion_tombstones WHERE kind = 'group'") == 1  # 復元後の再適用用
        assert retention.purge_retention(db, at=now() + timedelta(days=31))["groups"] == 0  # 冪等
    assert _scalar("SELECT count(*) FROM groups") == 0
    for table in ("memberships", "expenses", "expense_shares", "receipts", "invitations", "settlement_batches"):
        assert _scalar(f"SELECT count(*) FROM {table}") == 0, table
    assert list(storage.iterdir()) == []  # 画像ファイルも消す


def test_closed_group_is_deleted_after_one_year(user_factory):
    owner, member, pid, ids = _scenario(user_factory)
    assert owner.post(f"/groups/{pid}/close").status_code == 200
    with Session(get_engine()) as db:
        assert retention.purge_retention(db, at=now() + timedelta(days=300))["groups"] == 0
        assert retention.purge_retention(db, at=now() + timedelta(days=366))["groups"] == 1
    assert _scalar("SELECT count(*) FROM groups WHERE public_id = :p", p=pid) == 0


# ---------------------------------------------------------------- 保存期間

def test_retention_removes_only_expired_records(user_factory):
    owner, member, pid, ids = _scenario(user_factory)
    owner.post(f"/groups/{pid}/invites", json={})
    assert owner.post(f"/groups/{pid}/invites/revoke").status_code == 200
    with get_engine().begin() as c:
        c.execute(text("UPDATE sessions SET revoked_at = now() WHERE identity_id = "
                       "(SELECT identity_id FROM memberships WHERE id = :m)"), {"m": ids["Bさん"]})
    counts = {t: _scalar(f"SELECT count(*) FROM {t}") for t in ("sessions", "invitations", "audit_events")}
    with Session(get_engine()) as db:
        assert retention.purge_retention(db, at=now() + timedelta(days=29)) == {
            "webauthn_challenges": 0, "sessions": 0, "invitations": 0, "audit_events": 0,
            "deletion_tombstones": 0, "groups": 0, "groups_failed": 0}
        r = retention.purge_retention(db, at=now() + timedelta(days=31))
    assert r["sessions"] == 1 and r["invitations"] == counts["invitations"] and r["audit_events"] == 0
    assert owner.get(f"/groups/{pid}").status_code == 200  # 有効なセッションは消さない
    with Session(get_engine()) as db:
        r = retention.purge_retention(db, at=now() + timedelta(days=366))
    assert r["audit_events"] >= counts["audit_events"]  # 1 年を過ぎた監査ログは消す
    assert _scalar("SELECT count(*) FROM audit_events WHERE occurred_at <= now()") == 0


# ---------------------------------------------------------------- 復元後の削除の再適用

def test_tombstones_reapply_deletions_after_restore(user_factory, tmp_path):
    owner, member, pid, ids = _scenario(user_factory)
    registered(member)
    assert member.post("/account/payment-destination", json=bank()).status_code == 201
    with get_engine().connect() as c:  # 「バックアップ」: 削除前の暗号文
        backup = [tuple(r) for r in c.execute(text(
            "SELECT id, wrapped_dek, nonce, ciphertext FROM payment_destination_versions"))]
        member_identity = c.execute(text("SELECT identity_id FROM memberships WHERE id = :m"),
                                    {"m": ids["Bさん"]}).scalar_one()
    assert delete_me(member).status_code == 200
    out = tmp_path / "tombstones.jsonl"
    with Session(get_engine()) as db:
        assert retention.export_tombstones(db, out) == 2  # identity と destination
        assert retention.export_tombstones(db, out) == 0  # 追記は重複しない
    # 古いバックアップで戻した状態を再現する(退会前の行に戻し、DB 内の削除済み一覧も消える)
    with get_engine().begin() as c:
        c.execute(text("UPDATE identities SET disabled_at = NULL WHERE id = :i"), {"i": member_identity})
        c.execute(text("UPDATE memberships SET identity_id = :i WHERE id = :m"),
                  {"i": member_identity, "m": ids["Bさん"]})
        c.execute(text("UPDATE passkey_credentials SET revoked_at = NULL"))
        for vid, dek, nonce, ct in backup:
            c.execute(text("UPDATE payment_destination_versions SET wrapped_dek = :d, nonce = :n, ciphertext = :c "
                           "WHERE id = :v"), {"d": dek, "n": nonce, "c": ct, "v": vid})
        c.execute(text("UPDATE payment_destinations SET erased_at = NULL"))
        c.execute(text("TRUNCATE deletion_tombstones"))
    with Session(get_engine()) as db:
        assert retention.apply_tombstones(db, out) == {"identity": 1, "destination": 1, "group": 0}
        assert retention.apply_tombstones(db, out) == {"identity": 0, "destination": 0, "group": 0}  # 冪等
    assert _scalar("SELECT count(*) FROM payment_destination_versions WHERE ciphertext IS NOT NULL") == 0
    assert _scalar("SELECT disabled_at IS NOT NULL FROM identities WHERE id = :i", i=member_identity) is True
    assert _scalar("SELECT identity_id IS NULL FROM memberships WHERE id = :m", m=ids["Bさん"]) is True
    assert _scalar("SELECT count(*) FROM passkey_credentials WHERE revoked_at IS NULL") == 0


def _backup_module():
    import importlib.util  # noqa: PLC0415
    import os  # noqa: PLC0415

    path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "scripts", "warilink_backup.py")
    spec = importlib.util.spec_from_file_location("warilink_backup", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_backup_prune_removes_only_expired_backups_made_by_the_script(tmp_path, capsys):
    mod = _backup_module()
    assert mod.BACKUP_RETENTION_DAYS == 30
    for name, days in (("old", 31), ("new", 1)):
        d = tmp_path / name
        d.mkdir()
        created = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        (d / "manifest.json").write_text(json.dumps({"format": "warilink-backup-1", "created_at": created}))
    (tmp_path / "other").mkdir()  # manifest の無いものには触れない
    assert mod.main(["prune", "--root", str(tmp_path), "--dry-run"]) == 0
    assert (tmp_path / "old").exists()
    assert mod.main(["prune", "--root", str(tmp_path)]) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["new", "other"]
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["removed"] == ["old"] and out["kept"] == 1
    assert mod.main(["prune", "--root", str(tmp_path / "missing")]) == 3


def test_tombstone_cli_export_and_apply(user_factory, tmp_path, capsys):
    owner, member, pid, ids = _scenario(user_factory)
    assert delete_me(member).status_code == 200
    out = tmp_path / "t.jsonl"
    assert retention.main(["export-tombstones", "--out", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["appended"] == 1
    assert retention.main(["apply-tombstones", "--in", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["reapplied"] == {"identity": 0, "destination": 0, "group": 0}
