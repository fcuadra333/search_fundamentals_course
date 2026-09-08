"""Scripted target applications.

The platform talks to a real desktop through `engine.drivers.DesktopDriver`. In
this repository the concrete implementation is the simulator, and these are the
applications it runs: an accounting ERP, a bank portal, a corporate mail client
and a ticket system.

They are here for four reasons, all of them load-bearing rather than decorative:
dry runs during certification, the variation test, the demo tenant, and the test
suite. A production deployment swaps `driver_for` for a driver that attaches to
a real application, and nothing above this module changes.
"""

from __future__ import annotations

from typing import Any

from .engine.drivers import (
    DriverMode,
    ScriptedApplication,
    ScriptedScreen,
    SimulatedDesktopDriver,
    UiElement,
)

# Rows the ERP export produces. Small on purpose: the point is the mechanism.
LEDGER_ROWS = [
    {"asiento": "A-1001", "fecha": "2026-01-05", "importe": 15200.00, "cuenta": "1.1.01"},
    {"asiento": "A-1002", "fecha": "2026-01-11", "importe": 8430.50, "cuenta": "1.1.01"},
    {"asiento": "A-1003", "fecha": "2026-01-18", "importe": 22750.75, "cuenta": "1.1.01"},
    {"asiento": "A-1004", "fecha": "2026-01-27", "importe": 4110.00, "cuenta": "1.1.01"},
]

# The bank statement: one amount differs and one entry is missing, so the
# reconciliation has something real to find.
BANK_ROWS = [
    {"asiento": "A-1001", "fecha": "2026-01-05", "importe": 15200.00},
    {"asiento": "A-1002", "fecha": "2026-01-11", "importe": 8430.50},
    {"asiento": "A-1003", "fecha": "2026-01-18", "importe": 22700.75},
    {"asiento": "A-1009", "fecha": "2026-01-30", "importe": 990.00},
]


def erp_application(version: str = "12.4.1") -> ScriptedApplication:
    """Accounting ERP: main screen, report screen, export confirmation."""
    main = ScriptedScreen(
        key="erp.main",
        title="ERP Contable",
        elements=(
            UiElement(ref="menu.reports", role="menuitem", name="Reportes",
                      automation_id="menuReportes", label="Reportes", bounds=(20, 60, 120, 32)),
            UiElement(ref="menu.entries", role="menuitem", name="Asientos",
                      automation_id="menuAsientos", label="Asientos", bounds=(160, 60, 120, 32)),
            UiElement(ref="lbl.company", role="text", name="Empresa demo",
                      label="Empresa demo", bounds=(20, 20, 300, 24)),
        ),
        text=("ERP Contable — ejercicio 2026", "Empresa demo"),
        transitions={("click", "menu.reports"): "erp.reports"},
    )
    reports = ScriptedScreen(
        key="erp.reports",
        title="ERP Contable — Reportes",
        elements=(
            UiElement(ref="txt.period", role="edit", name="Período",
                      automation_id="txtPeriodo", label="Período", bounds=(40, 120, 200, 28)),
            UiElement(ref="btn.export", role="button", name="Exportar",
                      automation_id="btnExportar", label="Exportar", bounds=(40, 180, 140, 36)),
            UiElement(ref="btn.back", role="button", name="Volver",
                      automation_id="btnVolver", label="Volver", bounds=(200, 180, 140, 36)),
        ),
        text=("Seleccione el período a exportar",),
        transitions={
            ("click", "btn.export"): "erp.export_done",
            ("click", "btn.back"): "erp.main",
        },
        effects={("click", "btn.export"): {"files": {"mayor": LEDGER_ROWS}}},
    )
    done = ScriptedScreen(
        key="erp.export_done",
        title="ERP Contable — Exportación",
        elements=(
            UiElement(ref="lbl.done", role="text", name="Exportación completa",
                      label="Exportación completa", bounds=(40, 120, 320, 24)),
            UiElement(ref="btn.home", role="button", name="Volver al inicio",
                      automation_id="btnInicio", label="Volver al inicio", bounds=(40, 180, 180, 36)),
        ),
        text=("Exportación completa: 4 asientos",),
        transitions={("click", "btn.home"): "erp.main"},
    )
    return ScriptedApplication(
        name="ERP Contable",
        version=version,
        screens={screen.key: screen for screen in (main, reports, done)},
        start_screen="erp.main",
    )


def bank_application(version: str = "3.2.0") -> ScriptedApplication:
    """Bank portal: statement download in two screens."""
    main = ScriptedScreen(
        key="bank.main",
        title="Portal bancario",
        elements=(
            UiElement(ref="menu.statements", role="menuitem", name="Extractos",
                      automation_id="menuExtractos", label="Extractos", bounds=(20, 60, 140, 32)),
        ),
        text=("Portal bancario — cuenta corriente",),
        transitions={("click", "menu.statements"): "bank.statements"},
    )
    statements = ScriptedScreen(
        key="bank.statements",
        title="Portal bancario — Extractos",
        elements=(
            UiElement(ref="txt.period", role="edit", name="Mes",
                      automation_id="txtMes", label="Mes", bounds=(40, 120, 200, 28)),
            UiElement(ref="btn.download", role="button", name="Descargar",
                      automation_id="btnDescargar", label="Descargar", bounds=(40, 180, 140, 36)),
            UiElement(ref="btn.back", role="button", name="Inicio",
                      automation_id="btnInicio", label="Inicio", bounds=(200, 180, 140, 36)),
        ),
        text=("Elegí el mes del extracto",),
        transitions={
            ("click", "btn.download"): "bank.statements",
            ("click", "btn.back"): "bank.main",
        },
        effects={("click", "btn.download"): {"files": {"extracto": BANK_ROWS}}},
    )
    return ScriptedApplication(
        name="Portal bancario",
        version=version,
        screens={screen.key: screen for screen in (main, statements)},
        start_screen="bank.main",
    )


def mail_application(version: str = "2026.2") -> ScriptedApplication:
    """Corporate mail: the irreversible write of the demo."""
    main = ScriptedScreen(
        key="mail.main",
        title="Correo corporativo",
        elements=(
            UiElement(ref="btn.compose", role="button", name="Redactar",
                      automation_id="btnRedactar", label="Redactar", bounds=(20, 60, 140, 32)),
        ),
        text=("Bandeja de entrada",),
        transitions={("click", "btn.compose"): "mail.compose"},
    )
    compose = ScriptedScreen(
        key="mail.compose",
        title="Correo corporativo — Redactar",
        elements=(
            UiElement(ref="txt.to", role="edit", name="Para",
                      automation_id="txtPara", label="Para", bounds=(40, 100, 300, 28)),
            UiElement(ref="txt.subject", role="edit", name="Asunto",
                      automation_id="txtAsunto", label="Asunto", bounds=(40, 140, 300, 28)),
            UiElement(ref="btn.send", role="button", name="Enviar",
                      automation_id="btnEnviar", label="Enviar", bounds=(40, 200, 120, 36)),
            UiElement(ref="btn.discard", role="button", name="Descartar",
                      automation_id="btnDescartar", label="Descartar", bounds=(180, 200, 120, 36)),
        ),
        text=("Redactar mensaje",),
        transitions={
            ("click", "btn.send"): "mail.sent",
            ("click", "btn.discard"): "mail.main",
        },
        effects={("click", "btn.send"): {"files": {"enviado": [{"estado": "enviado"}]}}},
    )
    sent = ScriptedScreen(
        key="mail.sent",
        title="Correo corporativo — Enviado",
        elements=(
            UiElement(ref="btn.home", role="button", name="Inicio",
                      automation_id="btnInicio", label="Inicio", bounds=(40, 180, 120, 36)),
        ),
        text=("Mensaje enviado",),
        transitions={("click", "btn.home"): "mail.main"},
    )
    return ScriptedApplication(
        name="Correo corporativo",
        version=version,
        screens={screen.key: screen for screen in (main, compose, sent)},
        start_screen="mail.main",
    )


def ticket_application(version: str = "5.0.3") -> ScriptedApplication:
    """Ticket system used by the second tenant, so the demo has two different
    worlds and tenant isolation is visible rather than asserted."""
    main = ScriptedScreen(
        key="ticket.main",
        title="Mesa de ayuda",
        elements=(
            UiElement(ref="menu.reports", role="menuitem", name="Informes",
                      automation_id="menuInformes", label="Informes", bounds=(20, 60, 140, 32)),
        ),
        text=("Mesa de ayuda — tickets abiertos",),
        transitions={("click", "menu.reports"): "ticket.reports"},
    )
    reports = ScriptedScreen(
        key="ticket.reports",
        title="Mesa de ayuda — Informes",
        elements=(
            UiElement(ref="btn.export", role="button", name="Exportar",
                      automation_id="btnExportar", label="Exportar", bounds=(40, 160, 140, 36)),
            UiElement(ref="btn.back", role="button", name="Inicio",
                      automation_id="btnInicio", label="Inicio", bounds=(200, 160, 140, 36)),
        ),
        text=("Informe semanal de tickets",),
        transitions={
            ("click", "btn.export"): "ticket.reports",
            ("click", "btn.back"): "ticket.main",
        },
        effects={
            ("click", "btn.export"): {
                "files": {"tickets": [{"ticket": "T-1", "estado": "cerrado"}]}
            }
        },
    )
    return ScriptedApplication(
        name="Mesa de ayuda",
        version=version,
        screens={screen.key: screen for screen in (main, reports)},
        start_screen="ticket.main",
    )


CATALOG = {
    "ERP Contable": erp_application,
    "Portal bancario": bank_application,
    "Correo corporativo": mail_application,
    "Mesa de ayuda": ticket_application,
}


def application_for(name: str, *, version: str | None = None) -> ScriptedApplication | None:
    builder = CATALOG.get(name)
    if builder is None:
        return None
    return builder(version) if version else builder()


def driver_for(repos, skill, **kwargs: Any) -> SimulatedDesktopDriver | None:
    """Driver factory bound to a tenant's application registry.

    `kwargs` carry the certification perturbations (`window_scale`,
    `inject_dialog`), which is how the variation test reaches the driver.
    """
    application_id = skill["application_id"]
    if not application_id:
        return None
    row = repos.applications.get(application_id)
    if row is None:
        return None
    application = application_for(row["name"], version=row["detected_version"])
    if application is None:
        return None
    kwargs.setdefault("mode", DriverMode.PRODUCTION)
    return SimulatedDesktopDriver(application, **kwargs)


def make_driver_factory(repos):
    def factory(skill, **kwargs):
        return driver_for(repos, skill, **kwargs)

    return factory
