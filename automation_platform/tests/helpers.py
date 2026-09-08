"""Shared fixtures.

Everything runs against an in-memory database built through the same public
paths the product uses, so a test that passes means the real flow works, not
that a fixture was inserted behind the platform's back.
"""

from __future__ import annotations

from automation_platform.db import RepositorySet, connect, initialize
from automation_platform.domain.enums import Role
from automation_platform.engine.drivers import DriverMode, SimulatedDesktopDriver
from automation_platform.simulation import erp_application, make_driver_factory
from automation_platform.tenancy import TenantContext


def fresh_database():
    conn = connect(":memory:")
    initialize(conn)
    return conn


def make_organization(conn, name: str, slug: str, *, locale: str = "es"):
    bootstrap = RepositorySet(conn, TenantContext("bootstrap"))
    organization_id = bootstrap.organizations.create(name, slug, default_locale=locale)
    tenant = TenantContext(organization_id, role=Role.ADMIN, locale=locale,
                           organization_name=name)
    repos = RepositorySet(conn, tenant)
    user_id = repos.users.create(f"admin@{slug}.test", "Admin", Role.ADMIN.value)
    tenant = TenantContext(organization_id, user_id=user_id, role=Role.ADMIN, locale=locale,
                           organization_name=name)
    return RepositorySet(conn, tenant)


def erp_skill(repos, **overrides):
    """The reference skill: three screens, read only, evaluable postcondition."""
    application_id = repos.applications.create("ERP Contable", detected_version="12.4.1")
    fields = {
        "application_id": application_id,
        "key": "exportar_mayor",
        "name": "Exportar mayor del ERP",
        "executor": "computer_use",
        "start_anchor": "erp.main",
        "precondition": "screen_is('erp.main')",
        "postcondition": "file_exists('mayor') and file_rows('mayor') > 0",
        "semantic_check": "file_rows('mayor') > 0",
        "risk_classification": "read_only",
        "declared_screens": 3,
    }
    fields.update(overrides)
    skill_id = repos.skills.create(**fields)
    add_erp_steps(repos, skill_id)
    return skill_id, application_id


def add_erp_steps(repos, skill_id):
    steps = [
        (0, "click", "menuReportes", "Reportes", "menuitem", None, None),
        (1, "type_text", "txtPeriodo", "Período", "edit", "{periodo}", None),
        (1, "click", "btnExportar", "Exportar", "button", None, None),
        (2, "click", "btnInicio", "Volver al inicio", "button", None, "screen_is('erp.main')"),
    ]
    for position, (screen, action, automation_id, name, role, value, expected) in enumerate(steps, 1):
        repos.skills.add_step(
            skill_id,
            position=position,
            screen_index=screen,
            action_type=action,
            selectors=[
                {"tier": 1, "value": automation_id},
                {"tier": 2, "value": name, "role": role},
                {"tier": 3, "value": name},
            ],
            value_template=value,
            expected_value=expected,
        )


def erp_driver(**kwargs):
    kwargs.setdefault("mode", DriverMode.PRODUCTION)
    return SimulatedDesktopDriver(erp_application(), **kwargs)


def driver_factory(repos):
    return make_driver_factory(repos)


DEFAULT_INPUTS = {"periodo": "2026-01"}
