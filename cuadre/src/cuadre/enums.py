"""Domain enumerations.

Stored as short strings, not database-native enums, so the same schema runs on
SQLite and PostgreSQL and a new value never needs a migration lock.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    ADMIN = "ADMIN"
    PREPARER = "PREPARER"
    REVIEWER = "REVIEWER"

    @property
    def label(self) -> str:
        return {"ADMIN": "Administrador", "PREPARER": "Preparador", "REVIEWER": "Revisor"}[self]

    @property
    def can_approve(self) -> bool:
        """Only the reviewer approves. The admin manages the firm, it does not
        sign the reconciliation."""
        return self is Role.REVIEWER


class AccountKind(StrEnum):
    BANK = "BANK"
    LEDGER = "LEDGER"


class PeriodStatus(StrEnum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    IN_REVIEW = "IN_REVIEW"
    CLOSED = "CLOSED"

    @property
    def label(self) -> str:
        return {
            "OPEN": "Abierto",
            "IN_PROGRESS": "En proceso",
            "IN_REVIEW": "En revisión",
            "CLOSED": "Cerrado",
        }[self]

    @property
    def is_writable(self) -> bool:
        return self is not PeriodStatus.CLOSED


class ReconciliationStatus(StrEnum):
    PENDING_DATA = "PENDING_DATA"
    READY = "READY"
    RUNNING = "RUNNING"
    IN_REVIEW = "IN_REVIEW"
    APPROVED = "APPROVED"
    FAILED = "FAILED"

    @property
    def label(self) -> str:
        return {
            "PENDING_DATA": "Faltan archivos",
            "READY": "Listo para conciliar",
            "RUNNING": "Conciliando",
            "IN_REVIEW": "En revisión",
            "APPROVED": "Aprobada",
            "FAILED": "Con error",
        }[self]


class UploadKind(StrEnum):
    BANK_STATEMENT = "BANK_STATEMENT"
    LEDGER = "LEDGER"
    SUBLEDGER = "SUBLEDGER"

    @property
    def label(self) -> str:
        return {
            "BANK_STATEMENT": "Estado de cuenta",
            "LEDGER": "Mayor",
            "SUBLEDGER": "Auxiliar",
        }[self]


class UploadStatus(StrEnum):
    AWAITING_PROFILE = "AWAITING_PROFILE"
    IMPORTED = "IMPORTED"
    DUPLICATE = "DUPLICATE"
    REJECTED = "REJECTED"

    @property
    def label(self) -> str:
        return {
            "AWAITING_PROFILE": "Esperando mapeo",
            "IMPORTED": "Importado",
            "DUPLICATE": "Ya cargado",
            "REJECTED": "Rechazado",
        }[self]


class Side(StrEnum):
    BANK = "BANK"
    LEDGER = "LEDGER"

    @property
    def label(self) -> str:
        return {"BANK": "Banco", "LEDGER": "Libros"}[self]


class LineState(StrEnum):
    """State of a single statement line or ledger entry."""

    OPEN = "OPEN"
    MATCHED = "MATCHED"
    EXCEPTION = "EXCEPTION"
    CARRIED_FORWARD = "CARRIED_FORWARD"


class MatchMethod(StrEnum):
    EXACT = "EXACT"
    FUZZY = "FUZZY"
    MODEL = "MODEL"
    MANUAL = "MANUAL"

    @property
    def label(self) -> str:
        return {
            "EXACT": "Exacto",
            "FUZZY": "Difuso",
            "MODEL": "Modelo",
            "MANUAL": "Manual",
        }[self]

    @property
    def provenance(self) -> str:
        """Colour-coding axis of the interface: who decided this match."""
        return {
            "EXACT": "deterministic",
            "FUZZY": "deterministic",
            "MODEL": "model",
            "MANUAL": "human",
        }[self]


class MatchStatus(StrEnum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"

    @property
    def label(self) -> str:
        return {"PROPOSED": "Propuesto", "APPROVED": "Aprobado", "REJECTED": "Rechazado"}[self]


class ExceptionStatus(StrEnum):
    OPEN = "OPEN"
    RECONCILING_ITEM = "RECONCILING_ITEM"
    DEFERRED = "DEFERRED"
    RESOLVED = "RESOLVED"

    @property
    def label(self) -> str:
        return {
            "OPEN": "Abierta",
            "RECONCILING_ITEM": "Partida conciliatoria",
            "DEFERRED": "Diferida",
            "RESOLVED": "Resuelta",
        }[self]

    @property
    def is_open(self) -> bool:
        return self in (ExceptionStatus.OPEN, ExceptionStatus.DEFERRED)


class ExceptionCategory(StrEnum):
    """Why the line is unmatched. Drives the reconciliation statement layout."""

    DEPOSIT_IN_TRANSIT = "DEPOSIT_IN_TRANSIT"
    OUTSTANDING_CHECK = "OUTSTANDING_CHECK"
    BANK_CREDIT_UNRECORDED = "BANK_CREDIT_UNRECORDED"
    BANK_CHARGE_UNRECORDED = "BANK_CHARGE_UNRECORDED"
    BANK_ERROR = "BANK_ERROR"
    LEDGER_ERROR = "LEDGER_ERROR"
    UNDETERMINED = "UNDETERMINED"

    @property
    def label(self) -> str:
        return {
            "DEPOSIT_IN_TRANSIT": "Depósito en tránsito",
            "OUTSTANDING_CHECK": "Cheque en circulación",
            "BANK_CREDIT_UNRECORDED": "Nota de crédito no registrada",
            "BANK_CHARGE_UNRECORDED": "Nota de débito no registrada",
            "BANK_ERROR": "Error del banco",
            "LEDGER_ERROR": "Error del libro",
            "UNDETERMINED": "Sin clasificar",
        }[self]


class ModelTier(StrEnum):
    ECONOMY = "ECONOMY"
    MID = "MID"


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
