"""振込先の暗号化鍵の運用コマンド(中身・鍵の値は出力しない)。

  python -m app.modules.payment_destinations.cli status           鍵の版ごとの件数
  python -m app.modules.payment_destinations.cli rotate [--dry-run] 使用中の鍵で DEK を包み直す(中身は再暗号化しない)

鍵の入れ替え手順:
  1. WARILINK_DESTINATION_KEYS に新しい版を追加し、WARILINK_DESTINATION_ACTIVE_KEY を新しい版にする(古い版は残す)
  2. rotate を実行し、status で古い版が 0 件になったことを確かめる
  3. 古い版を WARILINK_DESTINATION_KEYS から外す
"""

from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import get_engine
from app.models.tables import PaymentDestination, PaymentDestinationVersion

from . import crypto


def status(db: Session) -> dict:
    active, _ = crypto.keyring()
    rows = db.execute(select(PaymentDestinationVersion.key_version, func.count())
                      .where(PaymentDestinationVersion.wrapped_dek.is_not(None))
                      .group_by(PaymentDestinationVersion.key_version)).all()
    return {"active_key_version": active, "versions_by_key": {k: n for k, n in rows}}


def rotate(db: Session, *, dry_run: bool) -> dict:
    active, _ = crypto.keyring()
    q = (select(PaymentDestinationVersion, PaymentDestination)
         .join(PaymentDestination, PaymentDestination.id == PaymentDestinationVersion.destination_id)
         .where(PaymentDestinationVersion.wrapped_dek.is_not(None), PaymentDestinationVersion.key_version != active)
         .with_for_update(of=PaymentDestinationVersion))
    done = 0
    for v, d in db.execute(q).all():
        if not dry_run:
            assert v.wrapped_dek is not None and v.nonce is not None and v.ciphertext is not None
            new = crypto.rewrap(crypto.Sealed(v.key_version, v.wrapped_dek, v.nonce, v.ciphertext),
                                destination_id=str(d.id), version=v.version, owner_id=str(d.owner_identity_id))
            v.key_version, v.wrapped_dek = new.key_version, new.wrapped_dek
        done += 1
    if dry_run:
        db.rollback()
    else:
        db.commit()
    return {"dry_run": dry_run, "rewrapped": done, "active_key_version": active}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.modules.payment_destinations.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    r = sub.add_parser("rotate")
    r.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    try:
        with Session(get_engine()) as db:
            out = status(db) if a.cmd == "status" else rotate(db, dry_run=a.dry_run)
    except crypto.KeyUnavailable as e:
        print(json.dumps({"error": "鍵が設定されていません", "detail": str(e)}, ensure_ascii=False))
        return 1
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
