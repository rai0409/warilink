"""テーブル定義。金額はすべて BIGINT の整数円(minor units)。

Phase 3 でレシート・OCR・利用枠のテーブルを追加。振込先・銀行照合は後続フェーズ(docs/DATA_MODEL.md)。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Identity(Base):
    __tablename__ = "identities"
    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # guest | registered
    created_at: Mapped[datetime] = _created()
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint("kind IN ('guest','registered')", name="ck_identity_kind"),)


class SessionRow(Base):
    __tablename__ = "sessions"
    id: Mapped[uuid.UUID] = _uuid_pk()
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False, unique=True)
    identity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("identities.id"), nullable=False, index=True)
    assurance_level: Mapped[str] = mapped_column(String(16), nullable=False, default="guest")
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Group(Base):
    __tablename__ = "groups"
    id: Mapped[uuid.UUID] = _uuid_pk()
    public_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    owner_identity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("identities.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="JPY")
    # 支出の追加・変更・削除ごとに +1。精算バッチ確定時の楽観ロックに使う。
    ledger_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = _created()
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint("currency = 'JPY'", name="ck_group_currency_jpy"),)


class Membership(Base):
    __tablename__ = "memberships"
    id: Mapped[uuid.UUID] = _uuid_pk()
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("groups.id"), nullable=False, index=True)
    # NULL = 名前だけのメンバー(まだ誰も招待URLで参加・紐付けしていない)
    identity_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("identities.id"))
    display_name: Mapped[str] = mapped_column(String(40), nullable=False)
    # 表示順(グループ内で一意)。同一トランザクション内の作成でも順序が安定する。
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = _created()
    __table_args__ = (
        CheckConstraint("role IN ('owner','member')", name="ck_membership_role"),
        CheckConstraint("status IN ('active','removed')", name="ck_membership_status"),
        UniqueConstraint("group_id", "position", name="uq_membership_group_position"),
        Index("uq_membership_group_identity", "group_id", "identity_id", unique=True,
              postgresql_where=text("identity_id IS NOT NULL")),
    )


class Invitation(Base):
    __tablename__ = "invitations"
    id: Mapped[uuid.UUID] = _uuid_pk()
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("groups.id"), nullable=False, index=True)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False, unique=True)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("identities.id"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    max_uses: Mapped[int] = mapped_column(Integer, nullable=False)
    uses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()
    __table_args__ = (CheckConstraint("uses >= 0 AND uses <= max_uses", name="ck_invite_uses"),)


class Expense(Base):
    __tablename__ = "expenses"
    id: Mapped[uuid.UUID] = _uuid_pk()
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("groups.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="JPY")
    split_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    occurred_on: Mapped[date | None] = mapped_column(Date)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("identities.id"), nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now(), nullable=False)
    __table_args__ = (
        CheckConstraint("state IN ('active','deleted')", name="ck_expense_state"),
        CheckConstraint("currency = 'JPY'", name="ck_expense_currency_jpy"),
    )


class ExpenseRevision(Base):
    """支出の全版(作成・訂正・削除)。入力仕様と計算結果を不変のスナップショットとして残す。"""

    __tablename__ = "expense_revisions"
    id: Mapped[uuid.UUID] = _uuid_pk()
    expense_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("expenses.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    change_kind: Mapped[str] = mapped_column(String(16), nullable=False)  # create | update | delete
    spec: Mapped[dict] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)
    changed_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("identities.id"), nullable=False)
    created_at: Mapped[datetime] = _created()
    __table_args__ = (UniqueConstraint("expense_id", "version", name="uq_expense_revision_version"),)


class ExpensePayer(Base):
    __tablename__ = "expense_payers"
    expense_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("expenses.id", ondelete="CASCADE"), primary_key=True)
    member_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("memberships.id"), primary_key=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)


class ExpenseShare(Base):
    __tablename__ = "expense_shares"
    expense_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("expenses.id", ondelete="CASCADE"), primary_key=True)
    member_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("memberships.id"), primary_key=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)


class SettlementBatch(Base):
    """確定した精算計画のスナップショット。上書きせず、新しい版を積み上げる。"""

    __tablename__ = "settlement_batches"
    id: Mapped[uuid.UUID] = _uuid_pk()
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("groups.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    ledger_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    method: Mapped[str] = mapped_column(String(32), nullable=False)
    is_exact_minimum: Mapped[bool] = mapped_column(nullable=False)
    fallback_reason: Mapped[str | None] = mapped_column(String(32))
    naive_transfer_count: Mapped[int] = mapped_column(Integer, nullable=False)
    minimum_transfer_count: Mapped[int | None] = mapped_column(Integer)
    balances: Mapped[dict] = mapped_column(JSONB, nullable=False)  # 確定時の残差残高
    assumptions: Mapped[dict] = mapped_column(JSONB, nullable=False)
    elapsed_ms: Mapped[float] = mapped_column(nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("identities.id"), nullable=False)
    created_at: Mapped[datetime] = _created()
    __table_args__ = (
        UniqueConstraint("group_id", "version", name="uq_batch_group_version"),
        CheckConstraint("state IN ('active','superseded')", name="ck_batch_state"),
        # 1グループにつき有効なバッチは常に1つ(二重確定防止の最終防衛線)
        Index("uq_batch_one_active", "group_id", unique=True, postgresql_where=text("state = 'active'")),
    )


class SettlementTransfer(Base):
    __tablename__ = "settlement_transfers"
    id: Mapped[uuid.UUID] = _uuid_pk()
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("settlement_batches.id"), nullable=False, index=True)
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("groups.id"), nullable=False, index=True)
    from_member_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("memberships.id"), nullable=False)
    to_member_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("memberships.id"), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="JPY")
    # unpaid → reported_paid(送金者申告) → recipient_confirmed(受取人の手動確認)
    # superseded: 再計算で置き換えられた未払いの送金。銀行照合は別の evidence state(Phase 5)。
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="unpaid")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now(), nullable=False)
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="ck_transfer_positive"),
        CheckConstraint("from_member_id <> to_member_id", name="ck_transfer_distinct"),
        CheckConstraint("status IN ('unpaid','reported_paid','recipient_confirmed','superseded')",
                        name="ck_transfer_status"),
    )


class TransferEvent(Base):
    __tablename__ = "transfer_events"
    id: Mapped[uuid.UUID] = _uuid_pk()
    transfer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("settlement_transfers.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    from_status: Mapped[str] = mapped_column(String(24), nullable=False)
    to_status: Mapped[str] = mapped_column(String(24), nullable=False)
    actor_identity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("identities.id"), nullable=False)
    created_at: Mapped[datetime] = _created()


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    identity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("identities.id"), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer)
    response: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created()


class AuditEvent(Base):
    """監査ログ。口座情報・トークン・OCR原文などの秘密情報は記録しない。"""

    __tablename__ = "audit_events"
    id: Mapped[uuid.UUID] = _uuid_pk()
    actor_identity_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("identities.id"))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    safe_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = _created()


# ---------------------------------------------------------------- Phase 3: レシート・OCR


class Receipt(Base):
    """アップロードされたレシート画像 1 枚。画像本体はファイル保存(EXIF 除去済み)で、DB には置かない。"""

    __tablename__ = "receipts"
    id: Mapped[uuid.UUID] = _uuid_pk()
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("groups.id"), nullable=False, index=True)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("identities.id"), nullable=False)
    original_sha256: Mapped[str] = mapped_column(String(64), nullable=False)  # 同じ画像の重複検出用
    content_type: Mapped[str] = mapped_column(String(32), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # uploaded → processing → draft_ready | failed → confirmed
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="uploaded")
    failure_code: Mapped[str | None] = mapped_column(String(48))
    expense_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("expenses.id"))
    review_elapsed_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created()
    image_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("state IN ('uploaded','processing','draft_ready','failed','confirmed')",
                        name="ck_receipt_state"),
        Index("uq_receipt_group_image", "group_id", "original_sha256", unique=True,
              postgresql_where=text("image_deleted_at IS NULL")),
    )


class ReceiptJob(Base):
    """OCR ジョブ(DB キュー)。FOR UPDATE SKIP LOCKED で取得し、lease 期限切れは別ワーカーが再取得する。"""

    __tablename__ = "receipt_jobs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    receipt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("receipts.id"), nullable=False, unique=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(48))
    created_at: Mapped[datetime] = _created()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("state IN ('queued','running','succeeded','failed','dead')", name="ck_job_state"),
        CheckConstraint("attempts >= 0", name="ck_job_attempts"),
        Index("ix_job_ready", "state", "available_at"),
    )


class OcrRun(Base):
    """1 回の OCR 実行の生結果と計測。利用者が確定した値とは別に保存する(上書きしない)。"""

    __tablename__ = "ocr_runs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    receipt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("receipts.id"), nullable=False, index=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("receipt_jobs.id"), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(64), nullable=False)
    adapter: Mapped[str] = mapped_column(String(32), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version: Mapped[str] = mapped_column(String(128), nullable=False)
    preprocess_version: Mapped[str] = mapped_column(String(32), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(32), nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    quality: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    tokens: Mapped[list | None] = mapped_column(JSONB)  # [{text, score, bbox}] 前処理後の画像座標
    draft: Mapped[dict | None] = mapped_column(JSONB)  # パーサー出力(行ごとの役割判定を含む)
    preprocess_ms: Mapped[float | None] = mapped_column()
    recognize_ms: Mapped[float | None] = mapped_column()
    parse_ms: Mapped[float | None] = mapped_column()
    elapsed_ms: Mapped[float] = mapped_column(nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(48))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = _created()


class OcrCorrection(Base):
    """読み取り結果と、利用者が確定した内容の差分。検証前は正解ラベルとして扱わない(評価・学習には使わない)。"""

    __tablename__ = "ocr_corrections"
    id: Mapped[uuid.UUID] = _uuid_pk()
    receipt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("receipts.id"), nullable=False, index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ocr_runs.id"), nullable=False)
    field_path: Mapped[str] = mapped_column(String(128), nullable=False)  # items[ocr-i2].line_minor 等
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    bbox: Mapped[list | None] = mapped_column(JSONB)
    # recognition=文字の誤認識 / structure=行の役割・対応付けの誤り / missing=読み落とし / spurious=余分な行
    error_class: Mapped[str] = mapped_column(String(16), nullable=False)
    label_status: Mapped[str] = mapped_column(String(16), nullable=False, default="proposed")
    corrected_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("identities.id"), nullable=False)
    created_at: Mapped[datetime] = _created()
    __table_args__ = (
        CheckConstraint("error_class IN ('recognition','structure','missing','spurious')", name="ck_corr_class"),
        CheckConstraint("label_status IN ('proposed','verified','rejected')", name="ck_corr_label"),
    )


class OcrQuotaEntry(Base):
    """OCR 利用枠の台帳(追記のみ)。上限は設定値。消費・返還はレシートごとに 1 回だけ(一意制約)。"""

    __tablename__ = "ocr_quota_ledger"
    id: Mapped[uuid.UUID] = _uuid_pk()
    identity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("identities.id"), nullable=False)
    period: Mapped[str] = mapped_column(String(7), nullable=False)  # YYYY-MM(日本時間)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # consume | refund
    units: Mapped[int] = mapped_column(Integer, nullable=False)
    receipt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("receipts.id"), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(48))
    created_at: Mapped[datetime] = _created()
    __table_args__ = (
        UniqueConstraint("receipt_id", "kind", name="uq_quota_receipt_kind"),
        CheckConstraint("kind IN ('consume','refund')", name="ck_quota_kind"),
        CheckConstraint("units > 0", name="ck_quota_units"),
        Index("ix_quota_identity_period", "identity_id", "period"),
    )


class OcrWorkerHeartbeat(Base):
    __tablename__ = "ocr_worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version: Mapped[str] = mapped_column(String(128), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
