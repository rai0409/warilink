from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.core.security import now
from app.models.tables import MaintenanceRun
from app.modules.identity.retention import RETENTION_JOB
from app.ocr.jobs import worker_available

router = APIRouter(tags=["meta"])


class PlanOut(BaseModel):
    id: str
    name: str
    price_label: str
    ocr_monthly_scans_min: int
    ocr_monthly_scans_max: int
    available: bool  # false = 準備中(表示のみ。購入・広告視聴はできない)
    notes: list[str]


class PlansOut(BaseModel):
    passkeys_available: bool
    payment_destinations_available: bool
    ocr_available: bool  # 設定で有効、かつ稼働中の OCR ワーカーがある
    ads_available: bool
    billing_available: bool
    ocr_free_monthly_scans: int
    ocr_reward_scans_per_ad: int
    ocr_monthly_scan_cap: int
    plans: list[PlanOut]


@router.get("/plans", response_model=PlansOut)
def plans(db: Session = Depends(get_db)) -> PlansOut:
    """料金比較の表示用。数値はすべて設定値から返す(画面に定数を持たない)。"""
    s = get_settings()
    return PlansOut(
        passkeys_available=s.feature_passkeys,
        # 振込先は Passkey(登録・step-up)が前提。どちらかが停止中なら提供していない
        payment_destinations_available=s.feature_passkeys and s.feature_payment_destinations,
        ocr_available=s.feature_ocr and worker_available(db),
        ads_available=s.feature_ads, billing_available=s.feature_billing,
        ocr_free_monthly_scans=s.ocr_free_monthly_scans, ocr_reward_scans_per_ad=s.ocr_reward_scans_per_ad,
        ocr_monthly_scan_cap=s.ocr_monthly_scan_cap,
        plans=[
            PlanOut(id="free", name="無料", price_label="0円", ocr_monthly_scans_min=s.ocr_free_monthly_scans,
                    ocr_monthly_scans_max=s.ocr_monthly_scan_cap, available=True,
                    notes=["登録なしの割り勘・精算はいつでも無料",
                           f"レシート読み取りは月{s.ocr_free_monthly_scans}枚まで",
                           f"報酬広告の視聴で合計{s.ocr_monthly_scan_cap}枚/月まで追加(広告が配信されない場合の追加は保証しません)"]),
            PlanOut(id="pro", name="Pro(検討中)", price_label="未定", ocr_monthly_scans_min=s.ocr_pro_monthly_scans_min,
                    ocr_monthly_scans_max=s.ocr_pro_monthly_scans_max, available=False,
                    notes=["繰り返し利用する方向けの参考値です", "価格・提供時期は未定です"]),
        ],
    )


# ---------------------------------------------------------------- データの保存期間(PR B)

# バックアップの保持日数(scripts/warilink_backup.py の BACKUP_RETENTION_DAYS と同じ。テストで一致を確認)
BACKUP_RETENTION_DAYS = 30
# 保持期限の削除ジョブは 1 日 1 回。最後の成功がこれより古ければ「動いていることを確認できない」とする
AUTOMATIC_DELETION_MAX_AGE_HOURS = 48


RetentionKey = Literal["receipt_images", "audit_logs", "expired_sessions", "expired_invites", "orphaned_groups",
                       "closed_groups", "backups", "deletion_tombstones"]


class RetentionPolicyOut(BaseModel):
    key: RetentionKey
    days: int


class AutomaticDeletionOut(BaseModel):
    # verified = 削除ジョブが直近 max_age_hours 以内に成功した。
    # unverified = 成功の記録が無い・古い(この環境では期限を過ぎても消えていない可能性がある)
    status: Literal["verified", "unverified"]
    last_success_at: datetime | None
    last_failure_at: datetime | None
    max_age_hours: int


class RetentionOut(BaseModel):
    """保存期間の方針(設定値)と、削除ジョブの実績(記録)を分けて返す。方針だけでは削除されたことにならない。"""

    policies: list[RetentionPolicyOut]
    automatic_deletion: AutomaticDeletionOut
    backup_encrypted_offsite: bool  # 暗号化した外部保管が確認できているか(PR C まで常に false)


@router.get("/retention", response_model=RetentionOut)
def retention(db: Session = Depends(get_db)) -> RetentionOut:
    s = get_settings()
    run = db.get(MaintenanceRun, RETENTION_JOB)
    last_ok = run.last_success_at if run else None
    fresh = last_ok is not None and now() - last_ok <= timedelta(hours=AUTOMATIC_DELETION_MAX_AGE_HOURS)
    return RetentionOut(
        policies=[
            RetentionPolicyOut(key="receipt_images", days=s.receipt_image_retention_days),
            RetentionPolicyOut(key="audit_logs", days=s.audit_log_retention_days),
            RetentionPolicyOut(key="expired_sessions", days=s.expired_session_retention_days),
            RetentionPolicyOut(key="expired_invites", days=s.expired_invite_retention_days),
            RetentionPolicyOut(key="orphaned_groups", days=s.orphaned_group_retention_days),
            RetentionPolicyOut(key="closed_groups", days=s.closed_group_retention_days),
            RetentionPolicyOut(key="backups", days=BACKUP_RETENTION_DAYS),
            RetentionPolicyOut(key="deletion_tombstones", days=s.tombstone_retention_days),
        ],
        automatic_deletion=AutomaticDeletionOut(
            status="verified" if fresh else "unverified", last_success_at=last_ok,
            last_failure_at=run.last_failure_at if run else None, max_age_hours=AUTOMATIC_DELETION_MAX_AGE_HOURS),
        backup_encrypted_offsite=False,
    )
