"""Demo data: two tenants, two different worlds.

Acme runs a monthly reconciliation across an ERP, a bank portal and a mail
client. Beta runs a weekly ticket export. They share the same code and see none
of each other's data, which is the point of seeding two of them.

Everything here goes through the same public paths a real user would take:
record, generalize, review, certify, publish, run. Nothing is inserted behind
the platform's back, so the demo is also an end-to-end exercise of the rules.
"""

from __future__ import annotations

from typing import Any

from .certification import Certifier
from .db import RepositorySet, connect, initialize, transaction
from .domain.enums import Role, Trigger
from .engine.runner import Runner
from .simulation import BANK_ROWS, LEDGER_ROWS, make_driver_factory
from .tenancy import TenantContext

RECONCILIATION_CASES = [{"erp_rows": LEDGER_ROWS, "banco_rows": BANK_ROWS}]


def seed(conn, *, with_runs: bool = True) -> dict[str, Any]:
    """Build both tenants. Returns a summary for the CLI to print."""
    initialize(conn)
    summary: dict[str, Any] = {}
    with transaction(conn):
        summary["acme"] = _seed_acme(conn)
        summary["beta"] = _seed_beta(conn)
    if with_runs:
        summary["acme"]["runs"] = _run_acme(conn, summary["acme"])
        summary["beta"]["runs"] = _run_beta(conn, summary["beta"])
    return summary


# ------------------------------------------------------------------- Acme ---


def _seed_acme(conn) -> dict[str, Any]:
    bootstrap = RepositorySet(conn, TenantContext("bootstrap"))
    organization_id = bootstrap.organizations.create(
        "Acme Servicios Contables", "acme", default_locale="es"
    )
    admin = TenantContext(organization_id, role=Role.ADMIN, locale="es",
                          organization_name="Acme Servicios Contables")
    repos = RepositorySet(conn, admin)

    users = {
        "admin": repos.users.create("ana@acme.test", "Ana Duarte", Role.ADMIN.value, "es"),
        "designer": repos.users.create("diego@acme.test", "Diego Fuentes", Role.DESIGNER.value, "es"),
        "operator": repos.users.create("olivia@acme.test", "Olivia Ramos", Role.OPERATOR.value, "es"),
        # An English-speaking approver, so the bilingual layer is exercised by
        # the demo rather than only by the tests.
        "approver": repos.users.create("albert@acme.test", "Albert Shaw", Role.APPROVER.value, "en"),
    }

    area = repos.areas.create("Administración")
    job_role = repos.job_roles.create(
        "Analista de conciliaciones",
        area_id=area,
        description="Concilia el mayor contra el extracto bancario cada mes.",
        inputs=["período", "cuenta contable"],
        outputs=["planilla de diferencias", "informe enviado"],
        approver_user_id=users["approver"],
    )

    erp = repos.applications.create("ERP Contable", detected_version="12.4.1")
    bank = repos.applications.create("Portal bancario", detected_version="3.2.0")
    mail = repos.applications.create("Correo corporativo", detected_version="2026.2")

    skills = {
        "export_erp": _skill_export_erp(repos, erp, users["designer"]),
        "export_bank": _skill_export_bank(repos, bank, users["designer"]),
        "reconcile": _skill_reconcile(repos, users["designer"]),
        "send_report": _skill_send_report(repos, mail, users["designer"]),
    }
    for skill_id in skills.values():
        repos.job_roles.assign_skill(job_role, skill_id)

    process_id = repos.processes.create(
        "Conciliación mensual",
        description="Exporta el mayor y el extracto, concilia y envía el informe.",
        job_role_id=job_role,
    )
    repos.processes.add_node(process_id, "exportar_erp", skills["export_erp"], position=1)
    repos.processes.add_node(process_id, "exportar_banco", skills["export_bank"], position=2)
    repos.processes.add_node(process_id, "conciliar", skills["reconcile"], position=3)
    repos.processes.add_edge(process_id, "exportar_erp")
    repos.processes.add_edge(process_id, "exportar_banco")
    repos.processes.add_edge(
        process_id, "conciliar", from_node_key="exportar_erp",
        data_mapping={"erp_rows": "exportar_erp.artifacts.files.mayor"},
    )
    repos.processes.add_edge(
        process_id, "conciliar", from_node_key="exportar_banco",
        data_mapping={"banco_rows": "exportar_banco.artifacts.files.extracto"},
    )

    return {
        "organization_id": organization_id,
        "users": users,
        "applications": {"erp": erp, "bank": bank, "mail": mail},
        "skills": skills,
        "process_id": process_id,
        "job_role_id": job_role,
    }


def _skill_export_erp(repos, application_id: str, designer: str) -> str:
    """The first skill to build (§11): the ERP export connector.

    Short, read-only, and its postcondition is trivially checkable — the file
    appeared, it parses, it has plausible rows.
    """
    skill_id = repos.skills.create(
        application_id=application_id,
        key="exportar_mayor",
        name="Exportar mayor del ERP",
        description="Exporta el libro mayor del período al formato de trabajo.",
        executor="computer_use",
        start_anchor="erp.main",
        precondition="screen_is('erp.main')",
        postcondition="file_exists('mayor') and file_rows('mayor') > 0",
        semantic_check="file_rows('mayor') > 0",
        risk_classification="read_only",
        declared_screens=3,
        input_schema_json='{"type": "object", "properties": {"periodo": {"type": "string"}}}',
        created_by=designer,
    )
    _steps(
        repos,
        skill_id,
        [
            (0, "click", "menuReportes", "Reportes", "menuitem", None, None),
            (1, "type_text", "txtPeriodo", "Período", "edit", "{periodo}", None),
            (1, "click", "btnExportar", "Exportar", "button", None, None),
            (2, "click", "btnInicio", "Volver al inicio", "button", None, "screen_is('erp.main')"),
        ],
    )
    return skill_id


def _skill_export_bank(repos, application_id: str, designer: str) -> str:
    """Deliberately without an L4 check: the product has to say out loud that
    this one is assistance with human review, not a guaranteed result (§6)."""
    skill_id = repos.skills.create(
        application_id=application_id,
        key="exportar_extracto",
        name="Descargar extracto bancario",
        description="Descarga el extracto del mes desde el portal del banco.",
        executor="computer_use",
        start_anchor="bank.main",
        precondition="screen_is('bank.main')",
        postcondition="file_exists('extracto') and file_rows('extracto') > 0",
        semantic_check=None,
        risk_classification="read_only",
        declared_screens=2,
        created_by=designer,
    )
    _steps(
        repos,
        skill_id,
        [
            (0, "click", "menuExtractos", "Extractos", "menuitem", None, None),
            (1, "type_text", "txtMes", "Mes", "edit", "{periodo}", None),
            (1, "click", "btnDescargar", "Descargar", "button", None, None),
            (1, "click", "btnInicio", "Inicio", "button", None, "screen_is('bank.main')"),
        ],
    )
    return skill_id


def _skill_reconcile(repos, designer: str) -> str:
    """Reconciliation is one more task: a `data_transform` skill whose L4 check
    is arithmetic, which is exactly what a `computer_use` skill usually lacks."""
    skill_id = repos.skills.create(
        key="conciliar_mayor",
        name="Conciliar mayor contra extracto",
        description="Cruza asientos por número e importe y arma la planilla de diferencias.",
        executor="data_transform",
        precondition="len(erp_rows) > 0 and len(banco_rows) > 0",
        postcondition="left_count > 0 and right_count > 0",
        semantic_check=(
            "matched + differences + only_left == left_count "
            "and matched + differences + only_right == right_count"
        ),
        risk_classification="read_only",
        declared_screens=1,
        created_by=designer,
    )
    repos.skills.add_step(
        skill_id,
        position=1,
        action_type="reconcile",
        value_template=(
            '{{"left": "erp_rows", "right": "banco_rows", '
            '"key": "asiento", "amount": "importe", "tolerance": 0.01}}'
        ),
    )
    repos.skills.add_step(
        skill_id,
        position=2,
        action_type="aggregate",
        value_template='{{"column": "difference", "as": "total_diferencias"}}',
    )
    return skill_id


def _skill_send_report(repos, application_id: str, designer: str) -> str:
    """The irreversible write of the demo: it never runs without approval."""
    skill_id = repos.skills.create(
        application_id=application_id,
        key="enviar_conciliacion",
        name="Enviar informe de conciliación",
        description="Envía por correo el informe con las diferencias detectadas.",
        executor="computer_use",
        start_anchor="mail.main",
        precondition="screen_is('mail.main')",
        postcondition="file_exists('enviado')",
        semantic_check=None,
        risk_classification="irreversible_write",
        declared_screens=3,
        max_runs_per_hour=10,
        created_by=designer,
    )
    _steps(
        repos,
        skill_id,
        [
            (0, "click", "btnRedactar", "Redactar", "button", None, None),
            (1, "type_text", "txtPara", "Para", "edit", "{destinatario}", None),
            (1, "type_text", "txtAsunto", "Asunto", "edit", "{asunto}", None),
            (1, "click", "btnEnviar", "Enviar", "button", None, None),
            (2, "click", "btnInicio", "Inicio", "button", None, "screen_is('mail.main')"),
        ],
    )
    return skill_id


# ------------------------------------------------------------------- Beta ---


def _seed_beta(conn) -> dict[str, Any]:
    bootstrap = RepositorySet(conn, TenantContext("bootstrap"))
    organization_id = bootstrap.organizations.create(
        "Beta Field Services", "beta", default_locale="en"
    )
    admin = TenantContext(organization_id, role=Role.ADMIN, locale="en",
                          organization_name="Beta Field Services")
    repos = RepositorySet(conn, admin)

    users = {
        "admin": repos.users.create("bruno@beta.test", "Bruno Vega", Role.ADMIN.value, "en"),
        "operator": repos.users.create("bianca@beta.test", "Bianca Ortiz", Role.OPERATOR.value, "es"),
    }
    helpdesk = repos.applications.create("Mesa de ayuda", detected_version="5.0.3")
    job_role = repos.job_roles.create(
        "Support reporting",
        description="Weekly ticket export for the operations review.",
        inputs=["week"],
        outputs=["ticket export"],
    )
    skill_id = repos.skills.create(
        application_id=helpdesk,
        key="export_tickets",
        name="Export weekly tickets",
        description="Exports the weekly ticket report from the help desk.",
        executor="computer_use",
        start_anchor="ticket.main",
        precondition="screen_is('ticket.main')",
        postcondition="file_exists('tickets') and file_rows('tickets') > 0",
        semantic_check="file_rows('tickets') > 0",
        risk_classification="read_only",
        declared_screens=2,
        created_by=users["admin"],
    )
    _steps(
        repos,
        skill_id,
        [
            (0, "click", "menuInformes", "Informes", "menuitem", None, None),
            (1, "click", "btnExportar", "Exportar", "button", None, None),
            (1, "click", "btnInicio", "Inicio", "button", None, "screen_is('ticket.main')"),
        ],
    )
    repos.job_roles.assign_skill(job_role, skill_id)
    process_id = repos.processes.create("Weekly ticket report", job_role_id=job_role)
    repos.processes.add_node(process_id, "export", skill_id, position=1)
    repos.processes.add_edge(process_id, "export")

    return {
        "organization_id": organization_id,
        "users": users,
        "applications": {"helpdesk": helpdesk},
        "skills": {"export_tickets": skill_id},
        "process_id": process_id,
        "job_role_id": job_role,
    }


# ------------------------------------------------------------------- runs ---


def _run_acme(conn, info: dict[str, Any]) -> dict[str, Any]:
    tenant = TenantContext(
        info["organization_id"], user_id=info["users"]["admin"], role=Role.ADMIN,
        locale="es", organization_name="Acme Servicios Contables",
    )
    repos = RepositorySet(conn, tenant)
    factory = make_driver_factory(repos)
    certifier = Certifier(repos, driver_factory=factory)

    certified = {}
    for key, skill_id in info["skills"].items():
        cases = RECONCILIATION_CASES if key == "reconcile" else [{
            "periodo": "2026-01",
            "destinatario": "gerencia@acme.test",
            "asunto": "Conciliación enero 2026",
        }]
        report = certifier.certify(skill_id, risk_declared=True, cases=cases)
        if report.publishable:
            certifier.publish(skill_id)
        certified[key] = report.status.value

    runner = Runner(repos, driver_factory=factory)
    process_result = runner.run_process(
        info["process_id"], inputs={"periodo": "2026-01"}, trigger=Trigger.MANUAL
    )

    # One irreversible run, left parked in the approval queue on purpose: the
    # demo should open on a dashboard that has something waiting for a human.
    approval_run = runner.run_skill(
        info["skills"]["send_report"],
        inputs={"destinatario": "gerencia@acme.test", "asunto": "Conciliación enero 2026"},
        trigger=Trigger.MANUAL,
    )

    return {
        "certified": certified,
        "process_execution": process_result.execution_id,
        "process_status": process_result.status.value,
        "pending_approval": approval_run.approval_id,
    }


def _run_beta(conn, info: dict[str, Any]) -> dict[str, Any]:
    tenant = TenantContext(
        info["organization_id"], user_id=info["users"]["admin"], role=Role.ADMIN,
        locale="en", organization_name="Beta Field Services",
    )
    repos = RepositorySet(conn, tenant)
    factory = make_driver_factory(repos)
    certifier = Certifier(repos, driver_factory=factory)
    skill_id = info["skills"]["export_tickets"]
    report = certifier.certify(skill_id, risk_declared=True, cases=[{"week": "2026-W05"}])
    if report.publishable:
        certifier.publish(skill_id)

    runner = Runner(repos, driver_factory=factory)
    result = runner.run_process(info["process_id"], inputs={"week": "2026-W05"})
    return {"certified": report.status.value, "process_status": result.status.value}


# ---------------------------------------------------------------- helpers ---


def _steps(repos, skill_id: str, rows) -> None:
    """Add steps with a full selector cascade for each element.

    Every element gets automation id, accessible role/name and visible label.
    Pixel coordinates are deliberately absent: a demo that shipped them would be
    teaching the wrong habit, and they mark a skill fragile.
    """
    for position, (screen_index, action, automation_id, name, role, value, expected) in enumerate(
        rows, start=1
    ):
        repos.skills.add_step(
            skill_id,
            position=position,
            screen_index=screen_index,
            action_type=action,
            selectors=[
                {"tier": 1, "value": automation_id},
                {"tier": 2, "value": name, "role": role},
                {"tier": 3, "value": name},
            ],
            value_template=value,
            expected_value=expected,
        )


def build_demo_database(path: str = ":memory:"):
    conn = connect(path)
    seed(conn)
    return conn
