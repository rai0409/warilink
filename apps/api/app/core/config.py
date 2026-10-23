from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WARILINK_", env_file=".env", extra="ignore")

    environment: str = "development"  # development | test | production
    database_url: str = "postgresql+psycopg://warilink:warilink@localhost:55432/warilink"

    session_cookie_name: str = "wl_session"
    session_ttl_days: int = 90
    cookie_secure: bool = False  # production では True 必須
    allowed_origins: list[str] = ["http://localhost:3000"]

    invite_default_ttl_hours: int = 24 * 7
    invite_default_max_uses: int = 50

    settlement_exact_max_participants: int = 20
    settlement_time_limit_ms: int = 1500

    # OCR 利用枠(コード定数にせず設定値で管理。台帳による消費は Phase 6)
    ocr_free_monthly_scans: int = 3
    ocr_reward_scans_per_ad: int = 1
    ocr_monthly_scan_cap: int = 10  # 無料枠 + 広告報酬の合計上限
    ocr_pro_monthly_scans_min: int = 100  # Pro は検討中の参考値
    ocr_pro_monthly_scans_max: int = 300
    # OCR を受け付けるか。true でも、稼働中のワーカーの heartbeat が無ければ受け付けない
    feature_ocr: bool = True
    feature_ads: bool = False
    feature_billing: bool = False

    # レシート画像・OCR ジョブ(Phase 3)
    receipt_storage_dir: str = "./var/receipts"  # API とワーカーで共有するディレクトリ(コンテナではボリューム)
    receipt_max_bytes: int = 10 * 1024 * 1024
    receipt_max_pixels: int = 40_000_000  # 展開爆弾対策
    receipt_image_retention_days: int = 30  # 期限後は画像だけ削除(`python -m app.ocr.cli purge-images`)
    ocr_job_max_attempts: int = 3
    ocr_job_lease_seconds: int = 120
    ocr_job_retry_backoff_seconds: int = 10
    ocr_worker_heartbeat_ttl_seconds: int = 90
    ocr_worker_poll_seconds: float = 1.0
    ocr_cpu_threads: int | None = None  # 未指定なら min(4, CPU 数)。CPU 数を超えると aarch64 で異常終了する
    ocr_det_model: str = "PP-OCRv6_medium_det"
    ocr_rec_model: str = "PP-OCRv6_medium_rec"
    ocr_low_confidence: float = 0.90  # これ未満の確信度の行は「要確認」と表示(暫定値・実測で調整)

    # 契約・法務確認まで本番では有効化できない(Phase 5 で fail-closed を実装)
    feature_bank_provider: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
