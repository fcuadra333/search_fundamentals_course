"""Classification of what did not match.

The category is what turns a list of leftovers into a reconciliation statement:
it decides which side of the four-column layout the item lands on.
"""

from __future__ import annotations

from ..enums import ExceptionCategory, Side
from .values import Line

# Wording that appears on Latin American statements for bank-originated items.
_CHARGE_HINTS = (
    "COMISION", "MANTENIMIENTO", "MANEJO", "ITBMS", "IVA", "IMPUESTO", "CARGO",
    "PORTES", "CHEQUERA", "SOBREGIRO", "INTERES MORA", "RETENCION", "IR ",
)
_CREDIT_HINTS = ("INTERES", "INTERESES", "ABONO", "NOTA CREDITO", "RENDIMIENTO", "DEVOLUCION")


def classify(line: Line, side: Side) -> ExceptionCategory:
    """Where an unmatched line belongs in the reconciliation statement.

    On the ledger side the entry is recorded in the books and not yet at the
    bank; on the bank side the bank moved money the books do not know about.
    The sign says which direction, the wording only refines the label.
    """
    description = (line.description or "").upper()
    if side is Side.LEDGER:
        return (
            ExceptionCategory.DEPOSIT_IN_TRANSIT
            if line.amount_cents > 0
            else ExceptionCategory.OUTSTANDING_CHECK
        )
    if line.amount_cents < 0:
        if any(hint in description for hint in _CHARGE_HINTS):
            return ExceptionCategory.BANK_CHARGE_UNRECORDED
        return ExceptionCategory.BANK_CHARGE_UNRECORDED
    if any(hint in description for hint in _CREDIT_HINTS):
        return ExceptionCategory.BANK_CREDIT_UNRECORDED
    return ExceptionCategory.BANK_CREDIT_UNRECORDED


def statement_section(category: ExceptionCategory) -> str:
    """Which side of the four-column statement the category adjusts."""
    if category in (
        ExceptionCategory.DEPOSIT_IN_TRANSIT,
        ExceptionCategory.OUTSTANDING_CHECK,
        ExceptionCategory.BANK_ERROR,
    ):
        return "bank"
    return "ledger"
