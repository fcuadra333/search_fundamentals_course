"""Multi-tenancy and write guards.

Invariant 1 of the specification: every query filters by firm, without
exception. Enforced here rather than trusted to each call site, and covered by
tests/test_tenancy.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeVar

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from .enums import PeriodStatus, Role
from .models import Period, Reconciliation

T = TypeVar("T")


class TenancyError(PermissionError):
    """The object exists but belongs to another firm. Reported as not found."""


class PeriodClosedError(PermissionError):
    """Invariant 2: a closed period admits no writes."""


class RoleError(PermissionError):
    """The role does not carry the permission for this action."""


@dataclass(frozen=True)
class Tenant:
    firm_id: int
    user_id: int
    role: Role

    @property
    def can_approve(self) -> bool:
        return Role(self.role).can_approve

    @property
    def is_admin(self) -> bool:
        return Role(self.role) is Role.ADMIN


def scoped(model: type[T], tenant: Tenant) -> Select[tuple[T]]:
    """Start a SELECT already filtered by firm. The only sanctioned entry point."""
    return select(model).where(model.firm_id == tenant.firm_id)  # type: ignore[attr-defined]


def get_scoped(session: Session, model: type[T], obj_id: int, tenant: Tenant) -> T:
    """Fetch by id within the tenant. Cross-firm access raises, never leaks."""
    obj = session.execute(
        scoped(model, tenant).where(model.id == obj_id)  # type: ignore[attr-defined]
    ).scalar_one_or_none()
    if obj is None:
        raise TenancyError(f"{model.__name__} {obj_id} no encontrado en esta firma")
    return obj


def require_role(tenant: Tenant, *roles: Role) -> None:
    if Role(tenant.role) not in roles:
        allowed = ", ".join(r.label for r in roles)
        raise RoleError(f"Acción reservada para: {allowed}")


def require_approver(tenant: Tenant) -> None:
    if not tenant.can_approve:
        raise RoleError("Solo el revisor puede aprobar una conciliación")


def require_writable_period(session: Session, period: Period) -> None:
    if not PeriodStatus(period.status).is_writable:
        raise PeriodClosedError(
            f"El período {period.label} está cerrado y no admite modificaciones"
        )


def require_writable(session: Session, reconciliation: Reconciliation) -> None:
    period = session.get(Period, reconciliation.period_id)
    if period is None:  # pragma: no cover - foreign key guarantees it
        raise TenancyError("Período inexistente")
    require_writable_period(session, period)


def segregation_ok(prepared_by_id: int | None, approver_id: int) -> bool:
    """Whoever prepared the reconciliation does not approve it. A one-person
    firm can still work: it just needs a second user to sign."""
    return prepared_by_id != approver_id


def audit(
    session: Session,
    tenant: Tenant | None,
    action: str,
    *,
    object_type: str = "",
    object_id: int | None = None,
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    from .models import AuditLog

    session.add(
        AuditLog(
            firm_id=tenant.firm_id if tenant else None,
            user_id=tenant.user_id if tenant else None,
            action=action,
            object_type=object_type,
            object_id=object_id,
            detail=detail or {},
            ip=ip,
        )
    )
