"""Role permissions (§2).

Four roles: administrator, designer (creates and trains skills), operator (runs
and supervises), approver (authorizes irreversible writes).

An approver approving their own request is allowed only when the organization
has a single approver, and the audit log records it either way. Blocking it
outright would strand small tenants; hiding it would defeat the point.
"""

from __future__ import annotations

from ..domain.enums import Role
from ..domain.errors import PermissionDenied
from ..tenancy import TenantContext

# Permission name -> roles that hold it. Administrators hold everything.
PERMISSIONS: dict[str, frozenset[Role]] = {
    "skill.view": frozenset({Role.DESIGNER, Role.OPERATOR, Role.APPROVER}),
    "skill.edit": frozenset({Role.DESIGNER}),
    "skill.certify": frozenset({Role.DESIGNER}),
    "skill.publish": frozenset({Role.DESIGNER}),
    "skill.stop": frozenset({Role.OPERATOR}),
    "process.view": frozenset({Role.DESIGNER, Role.OPERATOR, Role.APPROVER}),
    "process.edit": frozenset({Role.DESIGNER}),
    "process.run": frozenset({Role.OPERATOR}),
    "process.stop": frozenset({Role.OPERATOR}),
    "execution.view": frozenset({Role.DESIGNER, Role.OPERATOR, Role.APPROVER}),
    "execution.export": frozenset({Role.DESIGNER, Role.OPERATOR, Role.APPROVER}),
    "approval.view": frozenset({Role.APPROVER, Role.OPERATOR}),
    "approval.decide": frozenset({Role.APPROVER}),
    "training.record": frozenset({Role.DESIGNER}),
    "training.review": frozenset({Role.DESIGNER}),
    "health.view": frozenset({Role.DESIGNER, Role.OPERATOR, Role.APPROVER}),
    "audit.view": frozenset({Role.APPROVER}),
    "organization.manage": frozenset(),  # administrators only
}


def has_permission(role: Role, permission: str) -> bool:
    if role is Role.ADMIN:
        return True
    return role in PERMISSIONS.get(permission, frozenset())


def require(tenant: TenantContext, permission: str) -> None:
    if has_permission(tenant.role, permission):
        return
    raise PermissionDenied(required=permission, actual=tenant.role.value)


def permissions_of(role: Role) -> list[str]:
    return sorted(name for name in PERMISSIONS if has_permission(role, name))
