"""Tenant context.

Rule: every query filters by organization, without exception (§2). Nothing in
the platform reads or writes a business table without one of these objects in
hand, and `db.database.TenantGuardedConnection` refuses statements that reach a
tenant-scoped table without an `organization_id` predicate.
"""

from __future__ import annotations

from dataclasses import dataclass

from .domain.enums import Role
from .domain.errors import PermissionDenied


@dataclass(frozen=True)
class TenantContext:
    """Who is asking, on behalf of which organization, in which language."""

    organization_id: str
    user_id: str | None = None
    role: Role = Role.OPERATOR
    locale: str = "es"
    organization_name: str = ""

    @property
    def is_admin(self) -> bool:
        return self.role is Role.ADMIN

    def require_role(self, *roles: Role) -> None:
        """Raise unless the caller holds one of `roles`. Admins pass always."""
        if self.role is Role.ADMIN or self.role in roles:
            return
        raise PermissionDenied(
            required=", ".join(role.value for role in roles),
            actual=self.role.value,
        )

    def owns(self, row) -> bool:
        """True when `row` belongs to this tenant."""
        if row is None:
            return False
        organization_id = row["organization_id"] if "organization_id" in row.keys() else None
        return organization_id == self.organization_id


SYSTEM_CONTEXT_ROLE = Role.ADMIN


def system_context(organization_id: str, locale: str = "es") -> TenantContext:
    """Context used by background work (scheduler, recertification).

    It is still bound to a single organization: there is no global context that
    can read across tenants.
    """
    return TenantContext(
        organization_id=organization_id,
        user_id=None,
        role=SYSTEM_CONTEXT_ROLE,
        locale=locale,
    )
