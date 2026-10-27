import ipaddress
from functools import lru_cache
from urllib.parse import urlsplit

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.errors import ApiError

ENVIRONMENTS = ("development", "test", "production")
# 開発用の既定の DB 認証情報(compose・.env.example と同じ)。本番では使わせない
DEV_DB_CREDENTIALS = ("warilink:warilink@", "warilink:warilink-dev-only@")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0")


class UnsafeProductionConfig(ValueError):
    """本番で危険な設定。起動を中止する(fail-closed)。"""


class Settings(BaseSettings):
    # 検証エラーに入力値(DB の URL など秘密を含みうる)を出さない
    model_config = SettingsConfigDict(env_prefix="WARILINK_", env_file=".env", extra="ignore",
                                      hide_input_in_errors=True)

    environment: str = "development"  # development | test | production
    database_url: str = "postgresql+psycopg://warilink:warilink@localhost:55432/warilink"

    session_cookie_name: str = "wl_session"
    session_ttl_days: int = 90
    cookie_secure: bool = False  # production では True 必須
    allowed_origins: list[str] = ["http://localhost:3000"]

    invite_default_ttl_hours: int = 24 * 7
    invite_default_max_uses: int = 50

    # V0 beta の提供範囲。明示的に有効化した機能だけを API から提供する。
    feature_guest_sessions: bool = True
    feature_groups: bool = True
    feature_manual_expenses: bool = True
    feature_itemized_allocation: bool = True
    feature_settlement: bool = True
    feature_settlement_confirmations: bool = True
    feature_passkeys: bool = False
    feature_payment_destinations: bool = False

    settlement_exact_max_participants: int = 20
    settlement_time_limit_ms: int = 1500

    # OCR 利用枠(コード定数にせず設定値で管理。台帳による消費は Phase 6)
    ocr_free_monthly_scans: int = 3
    ocr_reward_scans_per_ad: int = 1
    ocr_monthly_scan_cap: int = 10  # 無料枠 + 広告報酬の合計上限
    ocr_pro_monthly_scans_min: int = 100  # Pro は検討中の参考値
    ocr_pro_monthly_scans_max: int = 300
    # OCR を受け付けるか。true でも、稼働中のワーカーの heartbeat が無ければ受け付けない
    feature_ocr: bool = False
    feature_ads: bool = False
    feature_billing: bool = False

    # レシート画像・OCR ジョブ(Phase 3)
    receipt_storage_dir: str = "./var/receipts"  # API とワーカーで共有するディレクトリ(コンテナではボリューム)
    receipt_max_bytes: int = 10 * 1024 * 1024
    receipt_max_pixels: int = 40_000_000  # 展開爆弾対策(ヘッダーの寸法で、デコード前に判定)
    receipt_max_side: int = 12_000  # 極端に細長い画像の拒否(1 辺の上限 px)
    # 濫用対策(OCR は CPU が重い)。ゲストは Cookie を消せば別人になれるため、利用者単位だけでなく
    # グループ単位・全体の待ち行列でも制限する
    ocr_rate_identity_per_10min: int = 5
    ocr_rate_group_per_hour: int = 30
    ocr_max_pending_per_identity: int = 3  # 読み取り待ち・処理中のジョブ数
    ocr_max_queue_backlog: int = 100  # 全体の読み取り待ち。超えたら 503(時間をおいて再試行)
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

    # 契約・法務確認まで本番では有効化できない
    feature_bank_provider: bool = False

    # Passkey(WebAuthn)。RP ID は Web のホスト名(またはその親ドメイン)。Origin は allowed_origins を使う
    webauthn_rp_id: str | None = None  # 未指定なら allowed_origins の最初のホスト名
    webauthn_rp_name: str = "WariLink"
    webauthn_challenge_ttl_seconds: int = 300
    # 本人の再確認(step-up): 振込先の登録・変更・全桁表示は、この秒数以内に Passkey で確認したセッションだけ
    step_up_max_age_seconds: int = 600

    # 濫用対策の回数制限(app/core/security.py の RATE_LIMITS)。IP は秘密鍵つきのハッシュだけを保存する
    # None = 本番だけ有効(開発・E2E は全員が 127.0.0.1 から来るため既定で無効。API テストは明示的に有効化して検証する)
    rate_limits_enabled: bool | None = None
    rate_limit_secret: str | None = None  # 本番は 32 文字以上が必須
    trusted_proxies: list[str] = []  # X-Forwarded-For を信頼する中継(IP / CIDR)。本番では Web(Next)の中継が必須
    rate_limit_overrides: dict[str, tuple[int, int]] = {}  # {"guest_create:ip": [上限回数, 期間の秒数]}

    @property
    def rate_limits_active(self) -> bool:
        return self.environment == "production" if self.rate_limits_enabled is None else self.rate_limits_enabled

    # 振込先の暗号化鍵(KEK)。{"版": "base64 の 32 バイト"}。DB・バックアップには入れない。
    # 未設定のとき、development / test は開発用の固定鍵を使い、production は振込先機能を停止する(503)
    destination_keys: dict[str, str] = {}
    destination_active_key: str | None = None

    @property
    def rp_id(self) -> str:
        if self.webauthn_rp_id:
            return self.webauthn_rp_id
        return (urlsplit(self.allowed_origins[0]).hostname or "") if self.allowed_origins else ""


# 本番で受け付ける回数制限の範囲(誤設定で制限が実質無効になる・正常利用を止めることを防ぐ)
RATE_LIMIT_BOUNDS = {"limit": (1, 10_000), "window": (10, 86_400)}


def rate_limit_problems(s: "Settings") -> list[str]:
    # security が config を読むため、ここで読み込む
    from app.core.security import RATE_LIMITS  # noqa: PLC0415

    out: list[str] = []
    if s.rate_limits_enabled is False:
        out.append("本番では回数制限を無効にできません(WARILINK_RATE_LIMITS_ENABLED)")
    if not s.rate_limit_secret or len(s.rate_limit_secret) < 32:
        out.append("WARILINK_RATE_LIMIT_SECRET に 32 文字以上のランダムな値を指定してください")
    if not s.trusted_proxies:
        # Web(Next)が API を中継するため、未設定だと全員が Next の IP として数えられる
        out.append("WARILINK_TRUSTED_PROXIES に API を中継するサーバー(Web)の IP / CIDR を指定してください")
    for n in s.trusted_proxies:
        try:
            ipaddress.ip_network(n, strict=False)
        except ValueError:
            out.append("WARILINK_TRUSTED_PROXIES は IP アドレスか CIDR の一覧にしてください")
            break
    lo_l, hi_l = RATE_LIMIT_BOUNDS["limit"]
    lo_w, hi_w = RATE_LIMIT_BOUNDS["window"]
    for name, (limit, window) in s.rate_limit_overrides.items():
        if name not in RATE_LIMITS:
            out.append(f"WARILINK_RATE_LIMIT_OVERRIDES に不明な制限名があります: {name}")
        elif not (lo_l <= limit <= hi_l and lo_w <= window <= hi_w):
            out.append(f"WARILINK_RATE_LIMIT_OVERRIDES の {name} は"
                       f"回数 {lo_l}〜{hi_l}、期間 {lo_w}〜{hi_w} 秒にしてください")
    return out


def production_problems(s: "Settings") -> list[str]:
    """本番(environment=production)で拒否する設定の一覧。値そのもの(秘密)はメッセージに含めない。"""
    out: list[str] = []
    if not s.cookie_secure:
        out.append("WARILINK_COOKIE_SECURE を true にしてください")
    if not s.allowed_origins:
        out.append("WARILINK_ALLOWED_ORIGINS に Web の https オリジンを指定してください")
    for o in s.allowed_origins:
        u = urlsplit(o)
        if o == "*" or "*" in o:
            out.append("WARILINK_ALLOWED_ORIGINS にワイルドカードは使えません")
        elif u.scheme != "https" or not u.hostname:
            out.append("WARILINK_ALLOWED_ORIGINS は https:// のオリジンだけにしてください")
        elif u.hostname in LOCAL_HOSTS:
            out.append("WARILINK_ALLOWED_ORIGINS に localhost は使えません")
        elif u.path not in ("", "/") or u.query or u.fragment or u.username:
            out.append("WARILINK_ALLOWED_ORIGINS はパスを含まないオリジン(https://ホスト[:ポート])にしてください")
    if any(c in s.database_url for c in DEV_DB_CREDENTIALS):
        out.append("WARILINK_DATABASE_URL に開発用の既定パスワードが使われています")
    if s.feature_bank_provider:
        out.append("銀行連携は契約・同意・法務確認まで本番で有効にできません(WARILINK_FEATURE_BANK_PROVIDER)")
    hosts = {urlsplit(o).hostname for o in s.allowed_origins}
    rp = s.rp_id
    if rp in LOCAL_HOSTS or not rp or not all(h and (h == rp or h.endswith("." + rp)) for h in hosts):
        out.append("WARILINK_WEBAUTHN_RP_ID を Web のホスト名(またはその親ドメイン)にしてください")
    if s.destination_keys or s.destination_active_key:
        from app.modules.payment_destinations.crypto import key_problems  # noqa: PLC0415

        out.extend(key_problems(s))
    out.extend(rate_limit_problems(s))
    if s.feature_ads or s.feature_billing:
        out.append("広告・課金は未実装のため本番で有効にできません(WARILINK_FEATURE_ADS / WARILINK_FEATURE_BILLING)")
    return out


def ensure_safe(s: Settings) -> Settings:
    """起動前の検証。本番で危険な設定なら例外で起動を中止する(fail-closed)。値はメッセージに含めない。"""
    if s.environment not in ENVIRONMENTS:
        raise UnsafeProductionConfig(f"WARILINK_ENVIRONMENT は {' / '.join(ENVIRONMENTS)} のいずれかにしてください")
    if s.environment == "production":
        problems = production_problems(s)
        if problems:
            raise UnsafeProductionConfig("本番の設定が安全ではないため起動を中止します: " + " / ".join(problems))
    return s


def require_feature(name: str) -> None:
    """無効な任意機能の書込み・利用入口を設定だけで閉じる。"""
    if not getattr(get_settings(), f"feature_{name}"):
        raise ApiError(503, "FEATURE_DISABLED", "この機能は現在提供していません")


@lru_cache
def get_settings() -> Settings:
    return ensure_safe(Settings())
