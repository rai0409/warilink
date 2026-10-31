"""データの保持期限の処理と、バックアップ復元後の削除の再適用(STEP4、docs/DATA_MODEL.md §11)。

purge-images の定期ジョブ(1 日 1 回)から purge_retention を呼ぶ。何度実行しても同じ結果になる。

  python -m app.modules.identity.retention export-tombstones --out FILE
      削除済み一覧を DB の外へ書き出す(追記・重複なし)
  python -m app.modules.identity.retention apply-tombstones --in FILE
      復元した DB に削除を再適用する

削除済み一覧には ID しか入れない(個人情報は持たない)。古いバックアップで DB を戻すと DB 内の一覧も古くなるため、
復元の手順では DB の外に書き出した一覧を必ず再適用する(infra/ops/README.md)。
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_engine
from app.core.security import now
from app.models.tables import (
    AuditEvent,
    DeletionTombstone,
    Group,
    Identity,
    Invitation,
    MaintenanceRun,
    Membership,
    PasskeyCredential,
    PaymentDestination,
    SessionRow,
    WebauthnChallenge,
)
from app.modules.groups.lifecycle import groups_due_for_deletion, hard_delete_group, mark_if_orphaned, release_owner
from app.modules.payment_destinations.router import crypto_erase
from app.ocr.images import delete_image

RETENTION_JOB = "retention_purge"


def record_run(db: Session, job: str, *, ok: bool, at: datetime | None = None) -> None:
    """定期ジョブの結果(時刻だけ)を残す。画面の「自動削除が動いているか」の根拠にする(方針の日数とは別)。"""
    t = at or now()
    row = db.get(MaintenanceRun, job) or MaintenanceRun(job=job)
    if ok:
        row.last_success_at = t
    else:
        row.last_failure_at = t
    db.add(row)
    db.commit()


def purge_retention(db: Session, *, at: datetime | None = None) -> dict[str, int]:
    """保持期限を過ぎたものを消す。グループの画像ファイルを消せなかった場合は、そのグループを次回に回す。"""
    s = get_settings()
    t = at or now()
    out: dict[str, int] = {}
    # 使い捨ての challenge: 使用済み・期限切れは使い道がない
    out["webauthn_challenges"] = db.execute(delete(WebauthnChallenge).where(
        (WebauthnChallenge.used_at.is_not(None)) | (WebauthnChallenge.expires_at <= t))).rowcount or 0
    # セッション: 失効・期限切れから一定期間(参照している未使用の challenge は先に消す)
    old = (SessionRow.revoked_at <= t - timedelta(days=s.expired_session_retention_days)) | (
        SessionRow.expires_at <= t - timedelta(days=s.expired_session_retention_days))
    db.execute(delete(WebauthnChallenge).where(WebauthnChallenge.session_id.in_(select(SessionRow.id).where(old))))
    out["sessions"] = db.execute(delete(SessionRow).where(old)).rowcount or 0
    # 招待: 失効・期限切れ・使い切りから一定期間
    inv_cut = t - timedelta(days=s.expired_invite_retention_days)
    out["invitations"] = db.execute(delete(Invitation).where(
        (Invitation.revoked_at <= inv_cut) | (Invitation.expires_at <= inv_cut))).rowcount or 0
    # 監査ログ
    out["audit_events"] = db.execute(delete(AuditEvent).where(
        AuditEvent.occurred_at <= t - timedelta(days=s.audit_log_retention_days))).rowcount or 0
    # 削除済み一覧(バックアップの保持日数を過ぎれば、再適用が必要な復元元は残っていない)
    out["deletion_tombstones"] = db.execute(delete(DeletionTombstone).where(
        DeletionTombstone.created_at <= t - timedelta(days=s.tombstone_retention_days))).rowcount or 0
    db.commit()

    deleted = failed = 0
    for g in groups_due_for_deletion(db, orphaned_before=t - timedelta(days=s.orphaned_group_retention_days),
                                     closed_before=t - timedelta(days=s.closed_group_retention_days)):
        keys = hard_delete_group(db, g)
        try:
            for k in keys:
                delete_image(k)
        except (OSError, ValueError):
            db.rollback()  # 画像を消せなければ行も残し、次回の実行で再試行する
            failed += 1
            continue
        db.commit()
        deleted += 1
    out["groups"] = deleted
    out["groups_failed"] = failed
    return out


# ---------------------------------------------------------------- 削除済み一覧(tombstone)


def export_tombstones(db: Session, path: Path) -> int:
    """DB の削除済み一覧をファイルへ追記する(既にある行は書かない)。戻り値は追記した件数。"""
    have: set[tuple[str, str]] = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                have.add((r["kind"], r["target_id"]))
    rows = db.execute(select(DeletionTombstone).order_by(DeletionTombstone.id)).scalars().all()
    new = [r for r in rows if (r.kind, str(r.target_id)) not in have]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in new:
            f.write(json.dumps({"kind": r.kind, "target_id": str(r.target_id),
                                "created_at": r.created_at.isoformat()}) + "\n")
    return len(new)


def apply_tombstones(db: Session, path: Path) -> dict[str, int]:
    """復元した DB に削除を再適用する。何度実行しても同じ結果になる(既に消えているものは何もしない)。"""
    from app.modules.groups.lifecycle import add_tombstone  # noqa: PLC0415

    t = now()
    out = {"identity": 0, "destination": 0, "group": 0}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        tid = uuid.UUID(r["target_id"])
        if r["kind"] == "destination":
            d = db.get(PaymentDestination, tid, with_for_update=True)
            if d is not None and d.erased_at is None:
                crypto_erase(db, d)
                out["destination"] += 1
        elif r["kind"] == "identity":
            ident = db.get(Identity, tid, with_for_update=True)
            if ident is not None and ident.disabled_at is None:
                _reapply_account_deletion(db, ident, t)
                out["identity"] += 1
        elif r["kind"] == "group":
            g = db.get(Group, tid, with_for_update=True)
            if g is not None:
                for k in hard_delete_group(db, g):
                    delete_image(k)
                out["group"] += 1
        add_tombstone(db, r["kind"], tid)
        db.commit()
    return out


def _reapply_account_deletion(db: Session, ident: Identity, t: datetime) -> None:
    db.execute(update(SessionRow).where(SessionRow.identity_id == ident.id, SessionRow.revoked_at.is_(None))
               .values(revoked_at=t))
    db.execute(update(PasskeyCredential).where(PasskeyCredential.identity_id == ident.id,
                                               PasskeyCredential.revoked_at.is_(None)).values(revoked_at=t))
    db.execute(delete(WebauthnChallenge).where(WebauthnChallenge.identity_id == ident.id))
    for d in db.execute(select(PaymentDestination).where(PaymentDestination.owner_identity_id == ident.id,
                                                         PaymentDestination.erased_at.is_(None))).scalars():
        crypto_erase(db, d)
    db.execute(update(Invitation).where(Invitation.created_by == ident.id, Invitation.revoked_at.is_(None))
               .values(revoked_at=t))
    for m in db.execute(select(Membership).where(Membership.identity_id == ident.id)).scalars().all():
        group = db.get(Group, m.group_id, with_for_update=True)
        assert group is not None
        if m.role == "owner":
            release_owner(db, group, m)
        m.identity_id = None
        mark_if_orphaned(db, group)
    ident.disabled_at = t
    ident.webauthn_user_handle = None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.modules.identity.retention")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export-tombstones")
    e.add_argument("--out", required=True)
    a = sub.add_parser("apply-tombstones")
    a.add_argument("--in", dest="src", required=True)
    args = p.parse_args(argv)
    with Session(get_engine()) as db:
        if args.cmd == "export-tombstones":
            appended = export_tombstones(db, Path(args.out))
            record_run(db, "tombstone_export", ok=True)  # 監視で書き出しの新しさを見る(PR C)
            print(json.dumps({"appended": appended}))
        else:
            print(json.dumps({"reapplied": apply_tombstones(db, Path(args.src))}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
