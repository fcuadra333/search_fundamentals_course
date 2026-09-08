"""Risk classification and absolute prohibitions (§7).

| Class                 | Examples                          | Policy                    |
|-----------------------|-----------------------------------|---------------------------|
| `read_only`           | export, query, read               | runs freely               |
| `reversible_write`    | create a draft, save to own folder| runs and notifies         |
| `irreversible_write`  | send, pay, delete, confirm, publish| prior human approval always|

No skill may ever: initiate payments or transfers, delete records, send external
communications without approval, modify credentials or permissions, or disable
the platform's own controls. That list is not configurable per tenant.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.enums import ActionType, RiskClass
from ..domain.errors import ProhibitedAction

# Action verbs that are refused outright, whatever the risk classification says.
ABSOLUTE_PROHIBITIONS = {
    "payment": ("pay", "transfer", "wire", "remit", "pagar", "transferir"),
    "deletion": ("delete_record", "drop_table", "purge", "borrar_registro"),
    "credentials": ("change_password", "reset_credentials", "grant_permission",
                    "revoke_permission", "cambiar_credenciales"),
    "controls": ("disable_audit", "disable_killswitch", "disable_approval",
                 "desactivar_control"),
}

# Verbs that mark a write the user cannot undo from the platform. Both
# languages: the interface being automated is as likely to say "Enviar" as
# "Send", and a heuristic that only reads English would understate the risk of
# every Spanish application.
IRREVERSIBLE_ACTION_HINTS = frozenset(
    {
        "send", "confirm", "publish", "submit", "pay", "delete", "approve", "transfer",
        "enviar", "confirmar", "publicar", "pagar", "borrar", "eliminar", "aprobar",
        "transferir", "emitir", "presentar",
    }
)


@dataclass(frozen=True)
class RiskDecision:
    risk: RiskClass
    requires_approval: bool
    notify: bool
    reason_key: str


def evaluate(risk: RiskClass) -> RiskDecision:
    if risk is RiskClass.IRREVERSIBLE_WRITE:
        return RiskDecision(risk, True, True, "skill.risk_hint_irreversible_write")
    if risk is RiskClass.REVERSIBLE_WRITE:
        return RiskDecision(risk, False, True, "skill.risk_hint_reversible_write")
    return RiskDecision(risk, False, False, "skill.risk_hint_read_only")


def assert_allowed(action_type: str, *, value: str | None = None) -> None:
    """Refuse an action that falls under an absolute prohibition.

    Checked before the driver is touched, on the action verb and on the value,
    so that "click the button labelled Pay" cannot slip through as a plain click.
    """
    haystack = f"{action_type} {value or ''}".lower()
    for category, verbs in ABSOLUTE_PROHIBITIONS.items():
        for verb in verbs:
            if verb in haystack:
                raise ProhibitedAction(action=action_type, category=category)


def looks_irreversible(action_type: str, value: str | None = None) -> bool:
    """Heuristic used when proposing a classification from a recording.

    It only ever proposes the stricter option; the designer declares the final
    classification explicitly, and certification checks that they did (§3).
    """
    haystack = f"{action_type} {value or ''}".lower()
    return any(hint in haystack for hint in IRREVERSIBLE_ACTION_HINTS)


def propose_classification(action_types: list[str], values: list[str] | None = None) -> RiskClass:
    values = values or []
    for index, action_type in enumerate(action_types):
        value = values[index] if index < len(values) else None
        if looks_irreversible(action_type, value):
            return RiskClass.IRREVERSIBLE_WRITE
    writes = {ActionType.TYPE_TEXT.value, ActionType.SELECT_OPTION.value, ActionType.SAVE_FILE.value}
    if any(action_type in writes for action_type in action_types):
        return RiskClass.REVERSIBLE_WRITE
    return RiskClass.READ_ONLY
