"""Shared fixtures.

Every test runs against its own SQLite file and its own blob directory, so the
suite never touches the demo database and tests cannot leak into each other.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))


@pytest.fixture
def settings(tmp_path, monkeypatch):
    from cuadre import config, db

    monkeypatch.setenv("CUADRE_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("CUADRE_BLOB_DIR", str(tmp_path / "blobs"))
    monkeypatch.setenv("CUADRE_EXPORT_DIR", str(tmp_path / "exports"))
    monkeypatch.setenv("CUADRE_LLM_PROVIDER", "stub")
    config.reset_settings()
    db.reset_engine()
    current = config.get_settings()
    yield current
    db.reset_engine()
    config.reset_settings()


@pytest.fixture
def session(settings):
    from cuadre.db import create_all, get_session_factory

    create_all()
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def firm(session):
    from cuadre.models import Firm

    firm = Firm(name="Firma de prueba", slug="prueba", country="NI")
    session.add(firm)
    session.flush()
    return firm


@pytest.fixture
def users(session, firm):
    from cuadre.enums import Role
    from cuadre.models import User
    from cuadre.security import hash_password

    made = {}
    for role, name, email in (
        (Role.ADMIN, "Admin", "admin@prueba.ni"),
        (Role.PREPARER, "Preparador", "prep@prueba.ni"),
        (Role.REVIEWER, "Revisor", "rev@prueba.ni"),
    ):
        user = User(firm_id=firm.id, email=email, name=name, role=role,
                    password_hash=hash_password("clave-de-prueba"))
        session.add(user)
        session.flush()
        made[role] = user
    return made


@pytest.fixture
def tenant(users):
    from cuadre.enums import Role
    from cuadre.tenancy import Tenant

    user = users[Role.PREPARER]
    return Tenant(firm_id=user.firm_id, user_id=user.id, role=Role.PREPARER)


@pytest.fixture
def reviewer_tenant(users):
    from cuadre.enums import Role
    from cuadre.tenancy import Tenant

    user = users[Role.REVIEWER]
    return Tenant(firm_id=user.firm_id, user_id=user.id, role=Role.REVIEWER)


@pytest.fixture
def entity(session, firm):
    from cuadre.models import Client, Entity

    client = Client(firm_id=firm.id, code="C-1", name="Cliente de prueba")
    session.add(client)
    session.flush()
    entity = Entity(firm_id=firm.id, client_id=client.id, name="Entidad de prueba",
                    functional_currency="NIO")
    session.add(entity)
    session.flush()
    return entity


@pytest.fixture
def accounts(session, firm, entity):
    from cuadre.enums import AccountKind
    from cuadre.models import Account

    ledger = Account(firm_id=firm.id, entity_id=entity.id, kind=AccountKind.LEDGER,
                     name="Efectivo en banco", gl_code="1101-01", currency="NIO")
    session.add(ledger)
    session.flush()
    bank = Account(firm_id=firm.id, entity_id=entity.id, kind=AccountKind.BANK,
                   name="Banco de prueba", bank_name="Banco de prueba", number="1002938475",
                   currency="NIO", counterpart_account_id=ledger.id)
    session.add(bank)
    session.flush()
    return {"bank": bank, "ledger": ledger}


def make_period(session, firm, entity, year=2026, month=1):
    import calendar

    from cuadre.enums import PeriodStatus
    from cuadre.models import Period

    last = calendar.monthrange(year, month)[1]
    period = Period(firm_id=firm.id, entity_id=entity.id, year=year, month=month,
                    start_date=dt.date(year, month, 1), end_date=dt.date(year, month, last),
                    status=PeriodStatus.IN_PROGRESS)
    session.add(period)
    session.flush()
    return period


def make_reconciliation(session, firm, period, accounts, **balances):
    from cuadre.enums import ReconciliationStatus
    from cuadre.models import Reconciliation

    reconciliation = Reconciliation(
        firm_id=firm.id, period_id=period.id,
        bank_account_id=accounts["bank"].id, ledger_account_id=accounts["ledger"].id,
        currency="NIO", status=ReconciliationStatus.PENDING_DATA, **balances,
    )
    session.add(reconciliation)
    session.flush()
    return reconciliation


@pytest.fixture
def period(session, firm, entity):
    return make_period(session, firm, entity)


@pytest.fixture
def reconciliation(session, firm, period, accounts):
    return make_reconciliation(session, firm, period, accounts)


@pytest.fixture
def loaded(session, tenant, reconciliation):
    """A reconciliation with the demo month already imported."""
    import demo_data
    from cuadre.enums import UploadKind
    from cuadre.ingest import service as ingest

    expected = demo_data.expected()
    reconciliation.opening_bank_cents = expected["opening_cents"]
    reconciliation.opening_ledger_cents = expected["opening_cents"]
    reconciliation.closing_ledger_cents = expected["ledger_closing_cents"]

    for kind, filename, data in (
        (UploadKind.BANK_STATEMENT, "banco.csv", demo_data.bank_statement_csv()),
        (UploadKind.LEDGER, "mayor.csv", demo_data.ledger_csv()),
    ):
        upload, _ = ingest.stage_upload(
            session, tenant, reconciliation, kind=kind, filename=filename, data=data
        )
        table, proposal = ingest.propose(session, upload, currency="NIO")
        ingest.import_upload(
            session, tenant, upload, mapping=proposal.mapping, header_row=table.header_row,
            date_format=proposal.date_format, sign_convention=proposal.sign_convention,
        )
    reconciliation.closing_ledger_cents = expected["ledger_closing_cents"]
    session.flush()
    return reconciliation


def line(id_, side, date, cents, description="", reference=None, carried=False):
    from cuadre.engine.values import Line

    return Line(id=id_, side=side, date=date, amount_cents=cents,
                description=description, reference=reference, carried=carried)
