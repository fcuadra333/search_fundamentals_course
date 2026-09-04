"""Build a demo firm with one full month of real work.

    python scripts/seed_demo.py [--reset]

Creates a firm, three clients, their entities and accounts, loads January 2026
for the first one from the two files in demo_data, runs the five stages and
prints the reconciliation statement.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import demo_data  # noqa: E402

from cuadre.config import get_settings  # noqa: E402
from cuadre.db import create_all, get_engine, session_scope  # noqa: E402
from cuadre.engine.runner import run_reconciliation  # noqa: E402
from cuadre.enums import (  # noqa: E402
    AccountKind,
    PeriodStatus,
    ReconciliationStatus,
    Role,
    UploadKind,
)
from cuadre.ingest import service as ingest  # noqa: E402
from cuadre.models import (  # noqa: E402
    Account,
    Base,
    Client,
    Entity,
    Firm,
    Period,
    Reconciliation,
    User,
)
from cuadre.money import format_amount  # noqa: E402
from cuadre.security import hash_password  # noqa: E402
from cuadre.tenancy import Tenant  # noqa: E402

DEMO_PASSWORD = "cuadre2026"


def reset() -> None:
    Base.metadata.drop_all(get_engine())


def build() -> dict:
    create_all()
    with session_scope() as session:
        firm = Firm(name="Álvarez & Asociados, Contadores Públicos", slug="alvarez",
                    country="NI", plan="FIRMA", entity_quota=15)
        session.add(firm)
        session.flush()

        users = {}
        for name, email, role in (
            ("Fernando Álvarez", "socio@alvarez.ni", Role.ADMIN),
            ("Lucía Mendoza", "preparador@alvarez.ni", Role.PREPARER),
            ("Marta Zeledón", "revisor@alvarez.ni", Role.REVIEWER),
        ):
            user = User(firm_id=firm.id, email=email, name=name, role=role,
                        password_hash=hash_password(DEMO_PASSWORD))
            session.add(user)
            session.flush()
            users[role] = user

        portfolio = [
            ("FER-001", "Ferretería El Progreso, S.A.", "Ferretería El Progreso, S.A.",
             "J0310000012345", "NIO", "Banco de la Producción", "1002938475"),
            ("AGR-002", "Agroindustrias del Norte", "Agroindustrias del Norte, S.A.",
             "J0310000067890", "USD", "Banco LAFISE", "2100455678"),
            ("TRA-003", "Transportes Rivas", "Transportes Rivas, S.A.",
             "J0310000099887", "NIO", "Banpro", "1003344556"),
        ]
        first_account = None
        for code, client_name, entity_name, tax_id, currency, bank, number in portfolio:
            client = Client(firm_id=firm.id, code=code, name=client_name,
                            contact_email=f"contabilidad@{code.lower()}.ni")
            session.add(client)
            session.flush()
            entity = Entity(firm_id=firm.id, client_id=client.id, name=entity_name,
                            tax_id=tax_id, functional_currency=currency, close_day=5)
            session.add(entity)
            session.flush()
            ledger_account = Account(
                firm_id=firm.id, entity_id=entity.id, kind=AccountKind.LEDGER,
                name="Efectivo en banco", gl_code="1101-01", currency=currency,
            )
            session.add(ledger_account)
            session.flush()
            bank_account = Account(
                firm_id=firm.id, entity_id=entity.id, kind=AccountKind.BANK,
                name=f"{bank} — Cuenta corriente", bank_name=bank, number=number,
                currency=currency, counterpart_account_id=ledger_account.id,
            )
            session.add(bank_account)
            session.flush()
            if first_account is None:
                first_account = (bank_account, ledger_account, entity, client)

        bank_account, ledger_account, entity, client = first_account
        period = Period(
            firm_id=firm.id, entity_id=entity.id, year=2026, month=1,
            start_date=dt.date(2026, 1, 1), end_date=dt.date(2026, 1, 31),
            status=PeriodStatus.IN_PROGRESS,
        )
        session.add(period)
        session.flush()

        expected = demo_data.expected()
        reconciliation = Reconciliation(
            firm_id=firm.id, period_id=period.id,
            bank_account_id=bank_account.id, ledger_account_id=ledger_account.id,
            currency=entity.functional_currency,
            opening_bank_cents=expected["opening_cents"],
            opening_ledger_cents=expected["opening_cents"],
            closing_ledger_cents=expected["ledger_closing_cents"],
            status=ReconciliationStatus.PENDING_DATA,
        )
        session.add(reconciliation)
        session.flush()

        tenant = Tenant(firm_id=firm.id, user_id=users[Role.PREPARER].id, role=Role.PREPARER)
        for kind, filename, data in (
            (UploadKind.BANK_STATEMENT, "estado_cuenta_enero_2026.csv",
             demo_data.bank_statement_csv()),
            (UploadKind.LEDGER, "mayor_1101_enero_2026.csv", demo_data.ledger_csv()),
        ):
            upload, profile = ingest.stage_upload(
                session, tenant, reconciliation, kind=kind, filename=filename, data=data
            )
            table, proposal = ingest.propose(
                session, upload, currency=reconciliation.currency
            )
            profile = ingest.save_profile(
                session, tenant, upload,
                name=f"{bank_account.bank_name} — {kind.label}",
                mapping=proposal.mapping, header_row=table.header_row,
                date_format=proposal.date_format, sign_convention=proposal.sign_convention,
                client_id=client.id, account=bank_account,
            )
            ingest.import_upload(session, tenant, upload, profile=profile)

        # The ledger export carries no running balance, so the closing figure
        # comes from the trial balance the client sends with it.
        reconciliation.closing_ledger_cents = expected["ledger_closing_cents"]
        reconciliation.opening_ledger_cents = expected["opening_cents"]
        session.flush()

        outcome = run_reconciliation(session, tenant, reconciliation)
        return {
            "firm": firm.name,
            "reconciliation_id": reconciliation.id,
            "verification": outcome.verification,
            "stats": outcome.stats,
            "currency": reconciliation.currency,
        }


def report(result: dict) -> None:
    verification = result["verification"]
    currency = result["currency"]

    def money(cents: int) -> str:
        return format_amount(cents, currency).rjust(16)

    print(f"\nFirma: {result['firm']}   ·   Conciliación #{result['reconciliation_id']}")
    print("=" * 68)
    print("CONCILIACIÓN BANCARIA — enero 2026")
    print("-" * 68)
    print(f"Saldo según estado de cuenta        {money(verification.bank_closing_cents)}")
    print(f"  (+) Depósitos en tránsito         {money(verification.deposits_in_transit_cents)}")
    print(f"  (−) Cheques en circulación        {money(verification.outstanding_checks_cents)}")
    print(f"  = Saldo ajustado del banco        {money(verification.adjusted_bank_cents)}")
    print("-" * 68)
    print(f"Saldo según libros                  {money(verification.ledger_closing_cents)}")
    print(f"  (+) Notas de crédito no registradas {money(verification.bank_credits_unrecorded_cents)}")
    print(f"  (−) Notas de débito no registradas  {money(verification.bank_charges_unrecorded_cents)}")
    print(f"  = Saldo ajustado de libros        {money(verification.adjusted_ledger_cents)}")
    print("-" * 68)
    print(f"DIFERENCIA                          {money(verification.difference_cents)}"
          f"   {'✓ CUADRA' if verification.balanced else '✗ NO CUADRA'}")
    print("=" * 68)
    print(f"Partidas emparejadas: {verification.matched_movements} de banco, "
          f"{verification.matched_entries} de libros")
    print(f"Partidas en la cola: {verification.open_movements + verification.open_entries}")
    print(f"Tasa de emparejamiento automático: {verification.match_rate:.1%}")
    print(f"Etapas: {result['stats']}")
    if verification.problems:
        print(f"Problemas de integridad: {verification.problems}")
    print(f"\nUsuarios de demostración (contraseña «{DEMO_PASSWORD}»):")
    print("  socio@alvarez.ni · preparador@alvarez.ni · revisor@alvarez.ni")


def main() -> int:
    parser = argparse.ArgumentParser(description="Carga la firma de demostración")
    parser.add_argument("--reset", action="store_true", help="borra y recrea el esquema")
    args = parser.parse_args()
    get_settings().ensure_dirs()
    if args.reset:
        reset()
    report(build())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
