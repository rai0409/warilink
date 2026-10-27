from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
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
