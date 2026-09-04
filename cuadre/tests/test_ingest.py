"""Stage 1: reading a file and deciding what its columns mean."""

from __future__ import annotations

import pytest

import demo_data
from cuadre.enums import UploadKind, UploadStatus
from cuadre.ingest import service as ingest
from cuadre.ingest.profiles import apply_mapping, detect_mapping
from cuadre.ingest.readers import UnsupportedFileError, read_table


def test_finds_the_header_under_a_title_block():
    table = read_table(demo_data.bank_statement_csv(), "estado.csv")
    # Three title lines and a blank one sit above the header.
    assert table.header_row == 3
    assert table.row_numbers[0] == 6  # first data row, counted in the real file
    assert table.headers[:3] == ["Fecha", "Descripcion", "No. Documento"]
    assert len(table.rows) == len(demo_data.BANK_ROWS)


def test_detects_a_debit_credit_statement():
    table = read_table(demo_data.bank_statement_csv(), "estado.csv")
    proposal = detect_mapping(table, UploadKind.BANK_STATEMENT)
    assert proposal.is_usable
    assert proposal.sign_convention == "debit_credit"
    assert proposal.mapping["date"] == "Fecha"
    assert proposal.mapping["debit"] == "Debitos"
    assert proposal.mapping["credit"] == "Creditos"
    assert proposal.date_format == "%d/%m/%Y"
    assert proposal.overall_confidence > 0.7


def test_detects_a_single_signed_column_ledger():
    table = read_table(demo_data.ledger_csv(), "mayor.csv")
    proposal = detect_mapping(table, UploadKind.LEDGER)
    assert proposal.sign_convention == "single"
    assert proposal.mapping["amount"] == "Monto"
    assert proposal.mapping["gl_account"] == "Cuenta"


def test_both_sides_end_up_in_the_same_sign_convention():
    """A deposit is positive on the statement and on the ledger, which is what
    lets the matching stages compare the two directly."""
    bank = read_table(demo_data.bank_statement_csv(), "estado.csv")
    bank_map = detect_mapping(bank, UploadKind.BANK_STATEMENT)
    bank_rows = apply_mapping(bank, bank_map.mapping, kind=UploadKind.BANK_STATEMENT,
                              date_format=bank_map.date_format,
                              sign_convention=bank_map.sign_convention)
    ledger = read_table(demo_data.ledger_csv(), "mayor.csv")
    ledger_map = detect_mapping(ledger, UploadKind.LEDGER)
    ledger_rows = apply_mapping(ledger, ledger_map.mapping, kind=UploadKind.LEDGER,
                                date_format=ledger_map.date_format,
                                sign_convention=ledger_map.sign_convention)

    assert bank_rows.lines[0].amount_cents == 1_500_000     # deposit, positive
    assert bank_rows.lines[1].amount_cents == -425_075      # check, negative
    assert ledger_rows.lines[0].amount_cents == 1_500_000
    assert ledger_rows.lines[1].amount_cents == -425_075
    assert not bank_rows.rejects and not ledger_rows.rejects


def test_running_balance_gives_opening_and_closing():
    table = read_table(demo_data.bank_statement_csv(), "estado.csv")
    proposal = detect_mapping(table, UploadKind.BANK_STATEMENT)
    result = apply_mapping(table, proposal.mapping, kind=UploadKind.BANK_STATEMENT,
                           date_format=proposal.date_format,
                           sign_convention=proposal.sign_convention)
    expected = demo_data.expected()
    assert result.opening_balance_cents == expected["opening_cents"]
    assert result.closing_balance_cents == expected["bank_closing_cents"]


def test_bad_rows_are_rejected_with_a_reason_never_dropped():
    data = (
        "Fecha,Descripcion,Monto\n"
        "02/01/2026,Buena,100.00\n"
        "no-es-fecha,Mala fecha,50.00\n"
        "03/01/2026,Monto vacio,\n"
        "04/01/2026,Monto cero,0.00\n"
    ).encode()
    table = read_table(data, "x.csv")
    result = apply_mapping(table, {"date": "Fecha", "description": "Descripcion", "amount": "Monto"},
                           kind=UploadKind.LEDGER)
    assert len(result.lines) == 1
    assert len(result.rejects) == 3
    assert {reject.row_number for reject in result.rejects} == {3, 4, 5}  # real file lines
    assert all(reject.reason for reject in result.rejects)


def test_unsupported_extension_is_refused():
    with pytest.raises(UnsupportedFileError):
        read_table(b"contenido", "estado.pdf")


def test_reuploading_the_same_bytes_is_refused(session, tenant, reconciliation):
    data = demo_data.bank_statement_csv()
    upload, _ = ingest.stage_upload(session, tenant, reconciliation,
                                    kind=UploadKind.BANK_STATEMENT, filename="a.csv", data=data)
    table, proposal = ingest.propose(session, upload, currency="NIO")
    ingest.import_upload(session, tenant, upload, mapping=proposal.mapping,
                         header_row=table.header_row, date_format=proposal.date_format,
                         sign_convention=proposal.sign_convention)
    with pytest.raises(ingest.DuplicateUploadError):
        ingest.stage_upload(session, tenant, reconciliation, kind=UploadKind.BANK_STATEMENT,
                            filename="otro-nombre.csv", data=data)


def test_a_confirmed_profile_skips_the_mapping_screen(session, tenant, reconciliation, accounts, entity):
    first, profile = ingest.stage_upload(
        session, tenant, reconciliation, kind=UploadKind.BANK_STATEMENT,
        filename="enero.csv", data=demo_data.bank_statement_csv())
    assert profile is None  # nothing learned yet
    table, proposal = ingest.propose(session, first, currency="NIO")
    saved = ingest.save_profile(
        session, tenant, first, name="Banco de prueba", mapping=proposal.mapping,
        header_row=table.header_row, date_format=proposal.date_format,
        sign_convention=proposal.sign_convention, client_id=entity.client_id,
        account=accounts["bank"])
    ingest.import_upload(session, tenant, first, profile=saved)

    # A different file from the same bank now comes in without asking.
    second, found = ingest.stage_upload(
        session, tenant, reconciliation, kind=UploadKind.BANK_STATEMENT,
        filename="febrero.csv", data=demo_data.bank_statement_csv().replace(b"02/01/2026", b"02/02/2026"))
    assert found is not None and found.id == saved.id
    result = ingest.import_upload(session, tenant, second, profile=found)
    assert result.lines
    assert second.status == UploadStatus.IMPORTED


def test_reimporting_replaces_only_its_own_lines(session, tenant, reconciliation):
    from cuadre.models import BankMovement

    upload, _ = ingest.stage_upload(session, tenant, reconciliation,
                                    kind=UploadKind.BANK_STATEMENT, filename="a.csv",
                                    data=demo_data.bank_statement_csv())
    table, proposal = ingest.propose(session, upload, currency="NIO")
    for _ in range(2):
        ingest.import_upload(session, tenant, upload, mapping=proposal.mapping,
                             header_row=table.header_row, date_format=proposal.date_format,
                             sign_convention=proposal.sign_convention)
    rows = session.query(BankMovement).filter_by(reconciliation_id=reconciliation.id).count()
    assert rows == len(demo_data.BANK_ROWS)
