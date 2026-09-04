"""The deliverable: reproducible workpapers with full provenance."""

from __future__ import annotations

from openpyxl import load_workbook

from cuadre.engine.runner import run_reconciliation
from cuadre.enums import ExceptionCategory, ExceptionStatus, MatchStatus
from cuadre.models import Exception_, Match
from cuadre.review import approve_match, mark_reconciling_item, reject_match
from cuadre.workpapers import model as workpaper_model
from cuadre.workpapers.builder import export_reconciliation


def test_the_workpaper_carries_everything_an_auditor_asks_for(session, tenant, loaded):
    run_reconciliation(session, tenant, loaded)
    paper = workpaper_model.build(session, loaded)

    assert paper.balanced and paper.difference_cents == 0
    # The four-column statement, both totals and the difference.
    labels = [label for label, _, _ in paper.statement]
    assert "Saldo según estado de cuenta" in labels
    assert "= Saldo ajustado del banco" in labels
    assert "= Saldo ajustado de libros" in labels
    assert labels[-1] == "Diferencia"

    # Every match with its method, confidence, approver and timestamp.
    assert paper.matches.rows
    method_column = paper.matches.columns.index("Método")
    approver_column = paper.matches.columns.index("Aprobado por")
    for row in paper.matches.rows:
        assert row[method_column]
        assert row[approver_column]

    # Every exception with its age, and the source files with their hashes.
    age_column = paper.exceptions.columns.index("Antigüedad (días)")
    assert all(isinstance(row[age_column], int) for row in paper.exceptions.rows)
    assert len(paper.sources.rows) == 2
    for row in paper.sources.rows:
        assert len(row[2]) == 64  # SHA-256


def test_human_corrections_appear_in_the_paper(session, tenant, loaded):
    run_reconciliation(session, tenant, loaded)
    match = session.query(Match).filter_by(status=MatchStatus.APPROVED).first()
    reject_match(session, tenant, match, "El cheque es de otro proveedor.")
    paper = workpaper_model.build(session, loaded)
    assert len(paper.corrections.rows) == 1
    assert "otro proveedor" in paper.corrections.rows[0][-1]


def test_the_fingerprint_is_stable_and_moves_only_when_the_data_does(
    session, tenant, reviewer_tenant, loaded
):
    run_reconciliation(session, tenant, loaded)
    first = workpaper_model.build(session, loaded).fingerprint
    assert workpaper_model.build(session, loaded).fingerprint == first

    proposal = session.query(Match).filter_by(status=MatchStatus.PROPOSED).first()
    approve_match(session, reviewer_tenant, proposal)
    assert workpaper_model.build(session, loaded).fingerprint != first


def test_the_files_are_written_and_readable(session, tenant, loaded):
    run_reconciliation(session, tenant, loaded)
    for exception in session.query(Exception_).filter_by(status=ExceptionStatus.OPEN).all():
        mark_reconciling_item(session, tenant, exception,
                              category=ExceptionCategory(exception.category))
    paths = export_reconciliation(session, tenant, loaded)
    xlsx = next(path for path in paths if path.suffix == ".xlsx")
    pdf = next(path for path in paths if path.suffix == ".pdf")

    assert xlsx.exists() and xlsx.stat().st_size > 5_000
    assert pdf.exists() and pdf.read_bytes().startswith(b"%PDF")

    workbook = load_workbook(xlsx)
    # One statement sheet for the account, plus the supporting detail.
    assert any("Emparejamientos" in name for name in workbook.sheetnames)
    assert any("Excepciones" in name for name in workbook.sheetnames)
    assert any("Correcciones" in name for name in workbook.sheetnames)
    assert any("origen" in name.lower() for name in workbook.sheetnames)
    statement = workbook[workbook.sheetnames[0]]
    values = [row[0] for row in statement.iter_rows(values_only=True)]
    assert any(value == "LA CONCILIACIÓN CUADRA" for value in values)
    # Amounts are numbers, so a reviewer can foot the column.
    amounts = [
        row[1] for row in statement.iter_rows(values_only=True)
        if isinstance(row[1], (int, float))
    ]
    assert amounts
