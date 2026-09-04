"""HTTP surface: authentication, tenancy over the wire and the upload flow."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import demo_data
from cuadre.engine.runner import run_reconciliation
from cuadre.enums import Role
from cuadre.models import Client, Firm, User
from cuadre.security import hash_password


@pytest.fixture
def client(settings, session, users, loaded, tenant):
    from cuadre.web.app import create_app

    run_reconciliation(session, tenant, loaded)
    session.commit()
    return TestClient(create_app(create_schema=False), follow_redirects=True)


def sign_in(client, email="prep@prueba.ni"):
    return client.post(
        "/entrar", data={"correo": email, "clave": "clave-de-prueba", "siguiente": "/"}
    )


def test_every_page_requires_signing_in(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert "/entrar" in response.headers["location"]


def test_a_wrong_password_says_nothing_useful(client):
    response = client.post(
        "/entrar", data={"correo": "prep@prueba.ni", "clave": "incorrecta", "siguiente": "/"}
    )
    assert "Correo o contraseña incorrectos" in response.text
    # The same wording for an address that does not exist.
    other = client.post(
        "/entrar", data={"correo": "nadie@prueba.ni", "clave": "x", "siguiente": "/"}
    )
    assert "Correo o contraseña incorrectos" in other.text


def test_the_main_pages_render(client, loaded):
    sign_in(client)
    for path in (
        "/",
        "/clientes",
        "/periodos",
        f"/conciliaciones/{loaded.id}",
        f"/conciliaciones/{loaded.id}/emparejamientos",
        f"/conciliaciones/{loaded.id}/excepciones",
        f"/conciliaciones/{loaded.id}/papeles",
        "/bitacora",
        "/correcciones",
        "/consumo",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "Cuadre" in response.text


def test_another_firms_client_reads_as_not_found(client, session):
    other = Firm(name="Ajena", slug="ajena")
    session.add(other)
    session.flush()
    stranger = Client(firm_id=other.id, code="X", name="Cliente ajeno")
    session.add(stranger)
    session.commit()

    sign_in(client)
    response = client.get(f"/clientes/{stranger.id}")
    assert response.status_code == 404
    assert "Cliente ajeno" not in response.text


def test_upload_then_confirm_the_mapping(client, session, loaded, tenant):
    from cuadre.models import Upload

    sign_in(client)
    # A different file: the same bytes would be refused as a re-upload, which is
    # what test_ingest covers.
    subledger = demo_data.ledger_csv().replace(b"Deposito ventas del dia", b"Cobro en caja")
    response = client.post(
        f"/conciliaciones/{loaded.id}/cargas",
        data={"tipo": "SUBLEDGER"},
        files={"archivo": ("auxiliar.csv", subledger, "text/csv")},
    )
    assert response.status_code == 200
    assert "¿Qué significa cada columna?" in response.text

    upload = session.query(Upload).order_by(Upload.id.desc()).first()
    confirmed = client.post(
        f"/cargas/{upload.id}/mapeo",
        data={
            "col_date": "Fecha", "col_amount": "Monto", "col_description": "Descripcion",
            "col_reference": "Comprobante", "header_row": "0", "date_format": "%d/%m/%Y",
        },
    )
    assert confirmed.status_code == 200
    session.expire_all()
    assert session.get(Upload, upload.id).rows_imported == len(demo_data.LEDGER_ROWS)


def test_a_manual_match_whose_sides_differ_is_refused_over_http(client, session, loaded):
    from cuadre.models import BankMovement, LedgerEntry

    sign_in(client)
    movement = session.query(BankMovement).filter(BankMovement.match_id.is_(None)).first()
    entry = session.query(LedgerEntry).filter(LedgerEntry.match_id.is_(None)).first()
    response = client.post(
        f"/conciliaciones/{loaded.id}/manual",
        data={"movimiento": str(movement.id), "asiento": str(entry.id), "motivo": "prueba"},
    )
    assert "no suman igual" in response.text


def test_the_download_path_cannot_escape_the_firms_directory(client):
    sign_in(client)
    for attempt in ("../../etc/passwd", "..%2f..%2fetc%2fpasswd", "/etc/passwd"):
        response = client.get(f"/descargas/{attempt}")
        assert response.status_code in (404, 307, 200)
        assert "root:" not in response.text


def test_the_preparer_cannot_reach_the_team_page(client, session):
    sign_in(client)
    response = client.get("/equipo", follow_redirects=False)
    assert response.status_code == 303  # bounced with a message, not shown


def test_the_reviewer_can(client, session, users):
    sign_in(client, email="rev@prueba.ni")
    response = client.get("/equipo")
    assert response.status_code == 200
    assert "Revisor" in response.text
