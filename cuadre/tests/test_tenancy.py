"""Invariant 1 (every query filters by firm) and invariant 2 (a closed period
admits no writes)."""

from __future__ import annotations

import pytest
from conftest import make_period, make_reconciliation

from cuadre.enums import PeriodStatus, Role
from cuadre.models import Client, Firm, Reconciliation, User
from cuadre.security import hash_password
from cuadre.tenancy import (
    PeriodClosedError,
    RoleError,
    Tenant,
    TenancyError,
    get_scoped,
    require_approver,
    require_role,
    require_writable,
    scoped,
    segregation_ok,
)


@pytest.fixture
def other_firm(session):
    firm = Firm(name="Otra firma", slug="otra")
    session.add(firm)
    session.flush()
    user = User(firm_id=firm.id, email="x@otra.ni", name="Otro", role=Role.ADMIN,
                password_hash=hash_password("x"))
    client = Client(firm_id=firm.id, code="O-1", name="Cliente ajeno")
    session.add_all([user, client])
    session.flush()
    return {"firm": firm, "user": user, "client": client}


def test_a_scoped_query_never_returns_another_firms_rows(session, tenant, entity, other_firm):
    visible = list(session.execute(scoped(Client, tenant)).scalars())
    assert [client.name for client in visible] == ["Cliente de prueba"]
    assert other_firm["client"] not in visible


def test_fetching_another_firms_row_by_id_raises(session, tenant, other_firm):
    with pytest.raises(TenancyError):
        get_scoped(session, Client, other_firm["client"].id, tenant)


def test_the_error_does_not_say_the_row_exists(session, tenant, other_firm):
    """A cross-firm id is reported as not found: that it exists is itself
    information we do not owe the caller."""
    with pytest.raises(TenancyError) as caught:
        get_scoped(session, Client, other_firm["client"].id, tenant)
    assert "no encontrado" in str(caught.value)
    assert "Cliente ajeno" not in str(caught.value)


def test_a_closed_period_refuses_writes(session, firm, entity, accounts, tenant):
    period = make_period(session, firm, entity)
    reconciliation = make_reconciliation(session, firm, period, accounts)
    require_writable(session, reconciliation)  # open: fine
    period.status = PeriodStatus.CLOSED
    session.flush()
    with pytest.raises(PeriodClosedError):
        require_writable(session, reconciliation)


def test_only_the_reviewer_approves(users):
    for role in (Role.ADMIN, Role.PREPARER):
        tenant = Tenant(firm_id=1, user_id=users[role].id, role=role)
        with pytest.raises(RoleError):
            require_approver(tenant)
    reviewer = Tenant(firm_id=1, user_id=users[Role.REVIEWER].id, role=Role.REVIEWER)
    require_approver(reviewer)


def test_require_role_names_what_is_allowed(tenant):
    with pytest.raises(RoleError) as caught:
        require_role(tenant, Role.ADMIN)
    assert "Administrador" in str(caught.value)


def test_whoever_prepared_does_not_approve():
    assert not segregation_ok(prepared_by_id=7, approver_id=7)
    assert segregation_ok(prepared_by_id=7, approver_id=8)
    assert segregation_ok(prepared_by_id=None, approver_id=8)


def test_every_reachable_table_carries_the_firm(session, firm, tenant, reconciliation):
    """The tenancy filter only works if the column is there. This is the guard
    that catches a new table added without it."""
    from cuadre.models import Base

    exempt = {"firms", "match_movements", "match_entries", "audit_log", "jobs"}
    for table in Base.metadata.sorted_tables:
        if table.name in exempt:
            continue
        assert "firm_id" in table.columns, f"{table.name} no tiene firm_id"
