"""Relational model.

Table names are English (CLAUDE.md: code in English) and map one to one onto the
data model of docs/ESPECIFICACION.md section 2:

    firmas -> firms                   movimientos_banco -> bank_movements
    usuarios -> users                 asientos -> ledger_entries
    clientes -> clients               emparejamientos -> matches
    entidades -> entities             excepciones -> exceptions
    cuentas -> accounts               correcciones -> corrections
    periodos -> periods               reglas -> rules
    cargas -> uploads                 perfiles_formato -> format_profiles

Two additions the specification implies but does not name:

    reconciliations  the work unit of one bank account within one period. The
                     hierarchy in the specification reads cuenta -> periodo ->
                     partida while the periods table is described per entity;
                     this table is that intersection, and it is what carries the
                     balances, the status and the workpaper.
    runs             one execution of the five stages, so the model output of a
                     period can be replayed instead of re-derived (rule 4).

Multi-tenancy: every table that a query can reach carries firm_id, directly or
through a parent that does. Queries go through cuadre.tenancy, never raw.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .enums import (
    AccountKind,
    ExceptionCategory,
    ExceptionStatus,
    JobStatus,
    LineState,
    MatchMethod,
    MatchStatus,
    ModelTier,
    PeriodStatus,
    ReconciliationStatus,
    Role,
    RunStatus,
    Side,
    UploadKind,
    UploadStatus,
)


def enum_column(enum_cls, length: int) -> SAEnum:
    """A VARCHAR column that always hands back the enum member, not a string.

    Without this an object read from the database carries a plain `str` while
    one still in memory carries the member, so `status is Status.OPEN` would be
    true or false depending on where the row came from. Stored as text (not a
    database-native enum) so the same schema runs on SQLite and PostgreSQL and
    a new value never needs a migration lock.
    """
    return SAEnum(
        enum_cls,
        native_enum=False,
        length=length,
        create_constraint=False,
        validate_strings=True,
    )


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now, nullable=False
    )


# --------------------------------------------------------------------------- #
# Tenancy root
# --------------------------------------------------------------------------- #


class Firm(TimestampMixin, Base):
    __tablename__ = "firms"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    country: Mapped[str] = mapped_column(String(2), default="NI", nullable=False)
    plan: Mapped[str] = mapped_column(String(20), default="FIRMA", nullable=False)
    entity_quota: Mapped[int] = mapped_column(Integer, default=15, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    users: Mapped[list[User]] = relationship(back_populates="firm", cascade="all, delete-orphan")
    clients: Mapped[list[Client]] = relationship(back_populates="firm", cascade="all, delete-orphan")


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("firm_id", "email", name="uq_users_firm_email"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int] = mapped_column(ForeignKey("firms.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(160), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[Role] = mapped_column(enum_column(Role, 16), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime)

    firm: Mapped[Firm] = relationship(back_populates="users")

    @property
    def can_approve(self) -> bool:
        return Role(self.role).can_approve


# --------------------------------------------------------------------------- #
# Portfolio
# --------------------------------------------------------------------------- #


class Client(TimestampMixin, Base):
    __tablename__ = "clients"
    __table_args__ = (UniqueConstraint("firm_id", "code", name="uq_clients_firm_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int] = mapped_column(ForeignKey("firms.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(24), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    contact_email: Mapped[str | None] = mapped_column(String(160))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    firm: Mapped[Firm] = relationship(back_populates="clients")
    entities: Mapped[list[Entity]] = relationship(
        back_populates="client", cascade="all, delete-orphan"
    )


class Entity(TimestampMixin, Base):
    """A legal entity. One client may keep several sets of books."""

    __tablename__ = "entities"

    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int] = mapped_column(ForeignKey("firms.id", ondelete="CASCADE"), index=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    tax_id: Mapped[str | None] = mapped_column(String(40))
    functional_currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    close_day: Mapped[int] = mapped_column(Integer, default=5, nullable=False)

    client: Mapped[Client] = relationship(back_populates="entities")
    accounts: Mapped[list[Account]] = relationship(
        back_populates="entity", cascade="all, delete-orphan"
    )
    periods: Mapped[list[Period]] = relationship(
        back_populates="entity", cascade="all, delete-orphan"
    )


class Account(TimestampMixin, Base):
    __tablename__ = "accounts"
    __table_args__ = (Index("ix_accounts_entity_kind", "entity_id", "kind"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int] = mapped_column(ForeignKey("firms.id", ondelete="CASCADE"), index=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"), index=True)
    kind: Mapped[AccountKind] = mapped_column(enum_column(AccountKind, 8), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    bank_name: Mapped[str | None] = mapped_column(String(120))
    number: Mapped[str | None] = mapped_column(String(60))
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    gl_code: Mapped[str | None] = mapped_column(String(40))
    # Learned column mapping for this bank, reused on every later upload.
    format_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("format_profiles.id", ondelete="SET NULL")
    )
    # A bank account points at the ledger account that carries its cash balance.
    counterpart_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL")
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    entity: Mapped[Entity] = relationship(back_populates="accounts")
    format_profile: Mapped[FormatProfile | None] = relationship(foreign_keys=[format_profile_id])

    @property
    def masked_number(self) -> str:
        if not self.number:
            return "—"
        tail = self.number[-4:]
        return f"···{tail}"


# --------------------------------------------------------------------------- #
# Period and work unit
# --------------------------------------------------------------------------- #


class Period(TimestampMixin, Base):
    __tablename__ = "periods"
    __table_args__ = (
        UniqueConstraint("entity_id", "year", "month", name="uq_periods_entity_month"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int] = mapped_column(ForeignKey("firms.id", ondelete="CASCADE"), index=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"), index=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    start_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    end_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    status: Mapped[PeriodStatus] = mapped_column(
        enum_column(PeriodStatus, 16), default=PeriodStatus.OPEN
    )
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    closed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    entity: Mapped[Entity] = relationship(back_populates="periods")
    reconciliations: Mapped[list[Reconciliation]] = relationship(
        back_populates="period", cascade="all, delete-orphan"
    )

    @property
    def label(self) -> str:
        names = [
            "enero", "febrero", "marzo", "abril", "mayo", "junio",
            "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
        ]
        return f"{names[self.month - 1]} {self.year}"

    @property
    def is_writable(self) -> bool:
        return PeriodStatus(self.status).is_writable


class Reconciliation(TimestampMixin, Base):
    """One bank account within one period: the unit of work and of the workpaper."""

    __tablename__ = "reconciliations"
    __table_args__ = (
        UniqueConstraint("period_id", "bank_account_id", name="uq_recon_period_account"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int] = mapped_column(ForeignKey("firms.id", ondelete="CASCADE"), index=True)
    period_id: Mapped[int] = mapped_column(ForeignKey("periods.id", ondelete="CASCADE"), index=True)
    bank_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    ledger_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"))
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)

    opening_bank_cents: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    closing_bank_cents: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    opening_ledger_cents: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    closing_ledger_cents: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    # Result of stage 5. Zero means the reconciliation ties.
    difference_cents: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    balanced: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    status: Mapped[ReconciliationStatus] = mapped_column(
        enum_column(ReconciliationStatus, 16), default=ReconciliationStatus.PENDING_DATA
    )
    prepared_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    notes: Mapped[str | None] = mapped_column(Text)

    period: Mapped[Period] = relationship(back_populates="reconciliations")
    bank_account: Mapped[Account] = relationship(foreign_keys=[bank_account_id])
    ledger_account: Mapped[Account | None] = relationship(foreign_keys=[ledger_account_id])
    prepared_by: Mapped[User | None] = relationship(foreign_keys=[prepared_by_id])
    reviewed_by: Mapped[User | None] = relationship(foreign_keys=[reviewed_by_id])
    uploads: Mapped[list[Upload]] = relationship(
        back_populates="reconciliation", cascade="all, delete-orphan"
    )
    runs: Mapped[list[Run]] = relationship(
        back_populates="reconciliation", cascade="all, delete-orphan"
    )


# --------------------------------------------------------------------------- #
# Ingestion
# --------------------------------------------------------------------------- #


class FormatProfile(TimestampMixin, Base):
    """Learned column mapping for one bank and client. Confirmed once, reused."""

    __tablename__ = "format_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int] = mapped_column(ForeignKey("firms.id", ondelete="CASCADE"), index=True)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    source_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    kind: Mapped[UploadKind] = mapped_column(enum_column(UploadKind, 20), nullable=False)
    # {"date": "Fecha", "amount": "Monto", "debit": ..., "credit": ..., ...}
    mapping: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    header_row: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    date_format: Mapped[str | None] = mapped_column(String(32))
    decimal_style: Mapped[str] = mapped_column(String(16), default="auto", nullable=False)
    # "single" (one signed column), "debit_credit" (two columns), "flag" (sign column)
    sign_convention: Mapped[str] = mapped_column(String(16), default="single", nullable=False)
    invert_sign: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    confirmed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    confirmed_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    times_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Upload(TimestampMixin, Base):
    __tablename__ = "uploads"
    __table_args__ = (Index("ix_uploads_hash", "firm_id", "content_hash"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int] = mapped_column(ForeignKey("firms.id", ondelete="CASCADE"), index=True)
    reconciliation_id: Mapped[int] = mapped_column(
        ForeignKey("reconciliations.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[UploadKind] = mapped_column(enum_column(UploadKind, 20), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    blob_path: Mapped[str] = mapped_column(String(400), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[UploadStatus] = mapped_column(
        enum_column(UploadStatus, 20), default=UploadStatus.AWAITING_PROFILE
    )
    format_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("format_profiles.id", ondelete="SET NULL")
    )
    rows_read: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_imported: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_rejected: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rejects: Mapped[list[Any]] = mapped_column(JSON, default=list)
    uploaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    reconciliation: Mapped[Reconciliation] = relationship(back_populates="uploads")
    format_profile: Mapped[FormatProfile | None] = relationship()
    uploaded_by: Mapped[User | None] = relationship()


class _LineMixin:
    """Columns shared by both sides of the reconciliation."""

    id: Mapped[int] = mapped_column(primary_key=True)
    row_number: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    value_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    raw_description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    normalized_description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    reference: Mapped[str | None] = mapped_column(String(80), index=True)
    state: Mapped[LineState] = mapped_column(
        enum_column(LineState, 20), default=LineState.OPEN, nullable=False
    )
    raw_row: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class BankMovement(_LineMixin, TimestampMixin, Base):
    """A normalized statement line. Positive increases the bank balance."""

    __tablename__ = "bank_movements"
    __table_args__ = (Index("ix_bank_movements_recon_state", "reconciliation_id", "state"),)

    firm_id: Mapped[int] = mapped_column(ForeignKey("firms.id", ondelete="CASCADE"), index=True)
    reconciliation_id: Mapped[int] = mapped_column(
        ForeignKey("reconciliations.id", ondelete="CASCADE"), index=True
    )
    upload_id: Mapped[int | None] = mapped_column(ForeignKey("uploads.id", ondelete="SET NULL"))
    # Set only by an approved match: a line belongs to at most one at a time.
    match_id: Mapped[int | None] = mapped_column(ForeignKey("matches.id", ondelete="SET NULL"))
    # Non-null when the line was carried in from an earlier period.
    carried_from_id: Mapped[int | None] = mapped_column(
        ForeignKey("bank_movements.id", ondelete="SET NULL")
    )


class LedgerEntry(_LineMixin, TimestampMixin, Base):
    """A normalized ledger or subledger line, in the same sign convention."""

    __tablename__ = "ledger_entries"
    __table_args__ = (Index("ix_ledger_entries_recon_state", "reconciliation_id", "state"),)

    firm_id: Mapped[int] = mapped_column(ForeignKey("firms.id", ondelete="CASCADE"), index=True)
    reconciliation_id: Mapped[int] = mapped_column(
        ForeignKey("reconciliations.id", ondelete="CASCADE"), index=True
    )
    upload_id: Mapped[int | None] = mapped_column(ForeignKey("uploads.id", ondelete="SET NULL"))
    match_id: Mapped[int | None] = mapped_column(ForeignKey("matches.id", ondelete="SET NULL"))
    carried_from_id: Mapped[int | None] = mapped_column(
        ForeignKey("ledger_entries.id", ondelete="SET NULL")
    )
    gl_account: Mapped[str | None] = mapped_column(String(40))
    document_type: Mapped[str | None] = mapped_column(String(40))
    third_party: Mapped[str | None] = mapped_column(String(160))


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #


class Match(TimestampMixin, Base):
    """N:M by construction. A deposit may clear five invoices; a payment may be
    partial across two. A 1:1 model breaks in the first week of real data."""

    __tablename__ = "matches"
    __table_args__ = (Index("ix_matches_recon_status", "reconciliation_id", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int] = mapped_column(ForeignKey("firms.id", ondelete="CASCADE"), index=True)
    reconciliation_id: Mapped[int] = mapped_column(
        ForeignKey("reconciliations.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    method: Mapped[MatchMethod] = mapped_column(enum_column(MatchMethod, 12), nullable=False)
    stage: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    confidence: Mapped[float] = mapped_column(default=1.0, nullable=False)
    status: Mapped[MatchStatus] = mapped_column(
        enum_column(MatchStatus, 12), default=MatchStatus.PROPOSED
    )
    amount_cents: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    difference_cents: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)
    # Stable hash of the inputs, so an identical proposal is never duplicated.
    fingerprint: Mapped[str] = mapped_column(String(64), default="", nullable=False, index=True)
    model_call_id: Mapped[int | None] = mapped_column(ForeignKey("model_calls.id", ondelete="SET NULL"))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime)

    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_id])
    approved_by: Mapped[User | None] = relationship(foreign_keys=[approved_by_id])
    movements: Mapped[list[BankMovement]] = relationship(
        secondary="match_movements", lazy="selectin"
    )
    entries: Mapped[list[LedgerEntry]] = relationship(secondary="match_entries", lazy="selectin")

    @property
    def provenance(self) -> str:
        return MatchMethod(self.method).provenance


class MatchMovement(Base):
    __tablename__ = "match_movements"

    match_id: Mapped[int] = mapped_column(
        ForeignKey("matches.id", ondelete="CASCADE"), primary_key=True
    )
    movement_id: Mapped[int] = mapped_column(
        ForeignKey("bank_movements.id", ondelete="CASCADE"), primary_key=True
    )


class MatchEntry(Base):
    __tablename__ = "match_entries"

    match_id: Mapped[int] = mapped_column(
        ForeignKey("matches.id", ondelete="CASCADE"), primary_key=True
    )
    entry_id: Mapped[int] = mapped_column(
        ForeignKey("ledger_entries.id", ondelete="CASCADE"), primary_key=True
    )


class Exception_(TimestampMixin, Base):
    """What did not match. Carries across periods until somebody resolves it."""

    __tablename__ = "exceptions"
    __table_args__ = (Index("ix_exceptions_recon_status", "reconciliation_id", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int] = mapped_column(ForeignKey("firms.id", ondelete="CASCADE"), index=True)
    reconciliation_id: Mapped[int] = mapped_column(
        ForeignKey("reconciliations.id", ondelete="CASCADE"), index=True
    )
    side: Mapped[Side] = mapped_column(enum_column(Side, 8), nullable=False)
    movement_id: Mapped[int | None] = mapped_column(
        ForeignKey("bank_movements.id", ondelete="CASCADE")
    )
    entry_id: Mapped[int | None] = mapped_column(ForeignKey("ledger_entries.id", ondelete="CASCADE"))
    category: Mapped[ExceptionCategory] = mapped_column(
        enum_column(ExceptionCategory, 28), default=ExceptionCategory.UNDETERMINED
    )
    status: Mapped[ExceptionStatus] = mapped_column(
        enum_column(ExceptionStatus, 20), default=ExceptionStatus.OPEN
    )
    amount_cents: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    value_date: Mapped[dt.date | None] = mapped_column(Date)
    # Days since the underlying line's value date, refreshed on each run.
    age_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    first_seen_period_id: Mapped[int | None] = mapped_column(
        ForeignKey("periods.id", ondelete="SET NULL")
    )
    carried_from_id: Mapped[int | None] = mapped_column(
        ForeignKey("exceptions.id", ondelete="SET NULL")
    )
    assigned_to_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reason: Mapped[str | None] = mapped_column(Text)
    # Candidates the engine considered and why it discarded them.
    candidates: Mapped[list[Any]] = mapped_column(JSON, default=list)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    resolved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    movement: Mapped[BankMovement | None] = relationship()
    entry: Mapped[LedgerEntry | None] = relationship()
    assigned_to: Mapped[User | None] = relationship(foreign_keys=[assigned_to_id])

    @property
    def line(self) -> BankMovement | LedgerEntry | None:
        return self.movement if self.side == Side.BANK else self.entry


class Correction(TimestampMixin, Base):
    """Every human override of a system proposal. Never discarded: this is the
    training data of the phase 2 learning loop."""

    __tablename__ = "corrections"

    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int] = mapped_column(ForeignKey("firms.id", ondelete="CASCADE"), index=True)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="SET NULL"))
    reconciliation_id: Mapped[int] = mapped_column(
        ForeignKey("reconciliations.id", ondelete="CASCADE"), index=True
    )
    match_id: Mapped[int | None] = mapped_column(ForeignKey("matches.id", ondelete="SET NULL"))
    exception_id: Mapped[int | None] = mapped_column(ForeignKey("exceptions.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    proposed: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    corrected: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    reason: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    user: Mapped[User | None] = relationship()


class Rule(TimestampMixin, Base):
    """Per-client matching rule derived from corrections. Recorded in v1,
    applied automatically only from phase 2 (ESPECIFICACION 2)."""

    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int] = mapped_column(ForeignKey("firms.id", ondelete="CASCADE"), index=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    derived_from_correction_id: Mapped[int | None] = mapped_column(
        ForeignKey("corrections.id", ondelete="SET NULL")
    )
    hits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    auto_apply: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


# --------------------------------------------------------------------------- #
# Execution, models, audit
# --------------------------------------------------------------------------- #


class Run(TimestampMixin, Base):
    """One execution of the five stages over one reconciliation."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int] = mapped_column(ForeignKey("firms.id", ondelete="CASCADE"), index=True)
    reconciliation_id: Mapped[int] = mapped_column(
        ForeignKey("reconciliations.id", ondelete="CASCADE"), index=True
    )
    engine_version: Mapped[str] = mapped_column(String(20), default="1.0.0", nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        enum_column(RunStatus, 12), default=RunStatus.RUNNING
    )
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, nullable=False)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    triggered_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    reconciliation: Mapped[Reconciliation] = relationship(back_populates="runs")


class ModelCall(TimestampMixin, Base):
    """Token and cost ledger. One row per call, whatever the provider."""

    __tablename__ = "model_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int | None] = mapped_column(ForeignKey("firms.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"), index=True)
    purpose: Mapped[str] = mapped_column(String(40), default="exception_resolution")
    tier: Mapped[ModelTier] = mapped_column(enum_column(ModelTier, 10), nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    model: Mapped[str] = mapped_column(String(80), nullable=False)
    batched: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Cost in millionths of a dollar: integers, no float money.
    cost_micros: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    schema_error: Mapped[str | None] = mapped_column(Text)
    request_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    # Persisted so the period is replayed, never re-derived (CLAUDE.md rule 4).
    response: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_firm_at", "firm_id", "at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int | None] = mapped_column(ForeignKey("firms.id", ondelete="CASCADE"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, nullable=False)
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    object_type: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    object_id: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ip: Mapped[str | None] = mapped_column(String(45))

    user: Mapped[User | None] = relationship()


class Job(Base):
    """Database-backed job queue. Reconciliation is night work, so a table with
    SKIP LOCKED semantics is enough and adds no broker to the deployment."""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status_run_after", "status", "run_after"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    firm_id: Mapped[int | None] = mapped_column(ForeignKey("firms.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[JobStatus] = mapped_column(
        enum_column(JobStatus, 12), default=JobStatus.QUEUED, nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    run_after: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, nullable=False)
    locked_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    locked_by: Mapped[str | None] = mapped_column(String(60))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, nullable=False)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
