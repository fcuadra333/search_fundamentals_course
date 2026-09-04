"""What the worker knows how to do.

Handlers are plain functions registered by name. Keeping them as separate
functions rather than one prompt-shaped entry point is what makes phase 3
orchestration an addition instead of a rewrite (ESPECIFICACION 7).
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from ..enums import Role
from ..models import Reconciliation, User
from ..tenancy import Tenant

Handler = Callable[[Session, dict], dict]
_REGISTRY: dict[str, Handler] = {}


def task(name: str) -> Callable[[Handler], Handler]:
    def register(func: Handler) -> Handler:
        _REGISTRY[name] = func
        return func

    return register


def get_handler(name: str) -> Handler:
    if name not in _REGISTRY:
        raise KeyError(f"Tarea desconocida: {name}")
    return _REGISTRY[name]


def registered() -> list[str]:
    return sorted(_REGISTRY)


def _tenant_from(session: Session, payload: dict) -> Tenant:
    user = session.get(User, payload["user_id"])
    if user is None:
        raise ValueError(f"Usuario {payload['user_id']} inexistente")
    return Tenant(firm_id=user.firm_id, user_id=user.id, role=Role(user.role))


@task("run_reconciliation")
def run_reconciliation_task(session: Session, payload: dict) -> dict:
    from ..engine.runner import run_reconciliation

    tenant = _tenant_from(session, payload)
    reconciliation = session.get(Reconciliation, payload["reconciliation_id"])
    if reconciliation is None or reconciliation.firm_id != tenant.firm_id:
        raise ValueError("Conciliación inexistente para esta firma")
    outcome = run_reconciliation(
        session, tenant, reconciliation, use_model=payload.get("use_model", True)
    )
    session.commit()
    return {
        "corrida": outcome.run.id,
        "cuadra": outcome.verification.balanced,
        "diferencia": outcome.verification.difference_cents,
    }


@task("export_workpapers")
def export_workpapers_task(session: Session, payload: dict) -> dict:
    from ..workpapers.builder import export_reconciliation

    tenant = _tenant_from(session, payload)
    reconciliation = session.get(Reconciliation, payload["reconciliation_id"])
    if reconciliation is None or reconciliation.firm_id != tenant.firm_id:
        raise ValueError("Conciliación inexistente para esta firma")
    paths = export_reconciliation(session, tenant, reconciliation)
    session.commit()
    return {"archivos": [str(path) for path in paths]}
