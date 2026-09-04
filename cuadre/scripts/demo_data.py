"""Realistic month of data for the demo and the tests.

One January, one bank account, and every case the engine has to handle:
exact matches, a two-day drift, a consolidated deposit that clears three
receipts, a payment the deterministic stages cannot reach, an outstanding
check, a deposit in transit and three bank-side items the books never saw.

The two files are written in different conventions on purpose: the statement
carries a title block, thousands separators and debit/credit columns; the
ledger is a clean export with a single signed amount.
"""

from __future__ import annotations

from dataclasses import dataclass

OPENING_BALANCE = 50_000_000  # 500,000.00 in cents


@dataclass(frozen=True)
class Row:
    date: str
    description: str
    reference: str
    cents: int


LEDGER_ROWS: tuple[Row, ...] = (
    Row("02/01/2026", "Deposito ventas del dia", "CD-0101", 1_500_000),
    Row("03/01/2026", "Pago Distribuidora ACME S.A.", "CK-1234", -425_075),
    Row("06/01/2026", "Cobro cliente Ferreteria Norte", "CD-0102", 2_000_000),
    Row("06/01/2026", "Cobro cliente Comercial Sur", "CD-0103", 1_800_000),
    Row("07/01/2026", "Cobro cliente Almacen Central", "CD-0104", 1_200_000),
    Row("10/01/2026", "Pago planilla primera quincena", "CK-1235", -8_500_000),
    Row("14/01/2026", "Pago Distribuidora La Union S.A.", "", -2_350_000),
    Row("20/01/2026", "Pago alquiler local comercial", "CK-1236", -3_000_000),
    Row("22/01/2026", "Deposito ventas del dia", "CD-0105", 975_000),
    Row("28/01/2026", "Pago servicios basicos", "CK-1237", -640_000),
    Row("31/01/2026", "Deposito ventas del dia", "CD-0106", 1_120_000),
)

BANK_ROWS: tuple[Row, ...] = (
    Row("02/01/2026", "DEPOSITO EN EFECTIVO VENTANILLA", "", 1_500_000),
    Row("03/01/2026", "CHEQUE PAGADO No. 001234", "001234", -425_075),
    Row("05/01/2026", "COMISION MANEJO DE CUENTA", "", -35_000),
    Row("05/01/2026", "IVA SOBRE COMISION", "", -5_250),
    Row("08/01/2026", "DEPOSITO CONSOLIDADO SUCURSAL CENTRO", "", 5_000_000),
    Row("12/01/2026", "CHEQUE PAGADO No. 001235", "001235", -8_500_000),
    Row("23/01/2026", "DEPOSITO EN EFECTIVO VENTANILLA", "", 975_000),
    Row("26/01/2026", "TRANSFERENCIA ACH 4471 PROV DISTR UNION", "", -2_350_000),
    Row("30/01/2026", "CHEQUE PAGADO No. 001237", "001237", -640_000),
    Row("31/01/2026", "INTERESES GANADOS DEL MES", "", 12_500),
)


def _money(cents: int) -> str:
    """Statement style: thousands separator and two decimals, always positive."""
    return f"{abs(cents) / 100:,.2f}"


def bank_statement_csv() -> bytes:
    """Statement with the title block a real export carries above the header."""
    lines = [
        "BANCO DE LA PRODUCCION, S.A.",
        "Estado de Cuenta Corriente No. 1002938475",
        "Periodo: 01/01/2026 al 31/01/2026;;;;;",
        "",
        "Fecha;Descripcion;No. Documento;Debitos;Creditos;Saldo",
    ]
    balance = OPENING_BALANCE
    for row in BANK_ROWS:
        balance += row.cents
        debit = _money(row.cents) if row.cents < 0 else ""
        credit = _money(row.cents) if row.cents > 0 else ""
        lines.append(
            f"{row.date};{row.description};{row.reference};{debit};{credit};{_money(balance)}"
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def ledger_csv() -> bytes:
    """Ledger export: one signed amount column, no thousands separators."""
    lines = ["Fecha,Comprobante,Cuenta,Descripcion,Monto"]
    for row in LEDGER_ROWS:
        lines.append(
            f"{row.date},{row.reference},1101-01,{row.description},{row.cents / 100:.2f}"
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def expected() -> dict:
    """What a correct run has to produce. Used by the tests and the bench."""
    bank_total = sum(row.cents for row in BANK_ROWS)
    ledger_total = sum(row.cents for row in LEDGER_ROWS)
    return {
        "opening_cents": OPENING_BALANCE,
        "bank_closing_cents": OPENING_BALANCE + bank_total,
        "ledger_closing_cents": OPENING_BALANCE + ledger_total,
        # Outstanding check 30,000.00 and deposit in transit 11,200.00.
        "deposits_in_transit_cents": 1_120_000,
        "outstanding_checks_cents": -3_000_000,
        # Commission, its tax and the interest earned.
        "bank_charges_unrecorded_cents": -40_250,
        "bank_credits_unrecorded_cents": 12_500,
        "difference_cents": 0,
        "reconciling_items": 5,
    }
