"""The reconciliation workspace: uploads, mapping, the run, matches and the queue."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import get_settings
from ...engine.runner import current_verification, refresh_totals, run_reconciliation
from ...enums import (
    ExceptionCategory,
    ExceptionStatus,
    LineState,
    MatchStatus,
    ReconciliationStatus,
    Side,
    UploadKind,
    UploadStatus,
)
from ...ingest import service as ingest
from ...ingest.readers import UnsupportedFileError
from ...models import (
    Account,
    BankMovement,
    Client,
    Entity,
    Exception_,
    LedgerEntry,
    Match,
    Period,
    Reconciliation,
    Run,
    Upload,
    User,
)
from ...review import (
    ReviewError,
    approval_blockers,
    approve_match,
    approve_reconciliation,
    assign_exception,
    defer_exception,
    manual_match,
    mark_reconciling_item,
    reject_match,
)
from ...tenancy import Tenant, get_scoped, scoped
from ..deps import current_tenant, current_user, flash, get_db, redirect, render

router = APIRouter()


def _context(session: Session, tenant: Tenant, reconciliation: Reconciliation) -> dict:
    period = session.get(Period, reconciliation.period_id)
    account = session.get(Account, reconciliation.bank_account_id)
    entity = session.get(Entity, account.entity_id) if account else None
    client = session.get(Client, entity.client_id) if entity else None
    counts = {
        "excepciones": len(
            [
                row
                for row in session.execute(
                    select(Exception_).where(
                        Exception_.reconciliation_id == reconciliation.id,
                        Exception_.status == ExceptionStatus.OPEN,
                    )
                ).scalars()
            ]
        ),
        "propuestas": len(
            [
                row
                for row in session.execute(
                    select(Match).where(
                        Match.reconciliation_id == reconciliation.id,
                        Match.status == MatchStatus.PROPOSED,
                    )
                ).scalars()
            ]
        ),
    }
    return {
        "reconciliation": reconciliation,
        "period": period,
        "account": account,
        "entity": entity,
        "client": client,
        "tab_counts": counts,
        "blockers": approval_blockers(session, reconciliation, tenant),
    }


@router.get("/conciliaciones/{reconciliation_id}")
def overview(
    reconciliation_id: int,
    request: Request,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
):
    reconciliation = get_scoped(session, Reconciliation, reconciliation_id, tenant)
    context = _context(session, tenant, reconciliation)
    context.update(
        active="periodos",
        tab="resumen",
        verification=current_verification(session, reconciliation),
        uploads=list(
            session.execute(
                select(Upload).where(Upload.reconciliation_id == reconciliation.id)
                .order_by(Upload.id)
            ).scalars()
        ),
        runs=list(
            session.execute(
                select(Run).where(Run.reconciliation_id == reconciliation.id)
                .order_by(Run.id.desc()).limit(5)
            ).scalars()
        ),
    )
    return render(request, "reconciliation.html", context,
                  session=session, user=user, tenant=tenant)


# --------------------------------------------------------------------------- #
# Uploads and column mapping
# --------------------------------------------------------------------------- #


@router.post("/conciliaciones/{reconciliation_id}/cargas")
async def upload(
    reconciliation_id: int,
    request: Request,
    tipo: str = Form(...),
    archivo: UploadFile = File(...),
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
):
    reconciliation = get_scoped(session, Reconciliation, reconciliation_id, tenant)
    data = await archivo.read()
    limit = get_settings().max_upload_bytes
    if len(data) > limit:
        flash(request, f"El archivo supera el límite de {limit // (1024 * 1024)} MB.", "error")
        return redirect(f"/conciliaciones/{reconciliation_id}")

    try:
        upload_row, profile = ingest.stage_upload(
            session, tenant, reconciliation,
            kind=UploadKind(tipo), filename=archivo.filename or "archivo", data=data,
        )
    except ingest.DuplicateUploadError as exc:
        flash(request, str(exc), "info")
        return redirect(f"/conciliaciones/{reconciliation_id}")

    if profile is not None:
        # A confirmed profile for this bank exists: no screen, straight in.
        try:
            result = ingest.import_upload(session, tenant, upload_row, profile=profile)
        except (UnsupportedFileError, ValueError) as exc:
            flash(request, f"No se pudo leer el archivo: {exc}", "error")
            return redirect(f"/conciliaciones/{reconciliation_id}")
        flash(
            request,
            f"«{upload_row.filename}»: {len(result.lines)} partidas con el perfil "
            f"«{profile.name}». {len(result.rejects)} fila(s) rechazada(s).",
        )
        return redirect(f"/conciliaciones/{reconciliation_id}")

    return redirect(f"/cargas/{upload_row.id}/mapeo")


@router.get("/cargas/{upload_id}/mapeo")
def mapping_form(
    upload_id: int,
    request: Request,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
):
    upload_row = get_scoped(session, Upload, upload_id, tenant)
    reconciliation = session.get(Reconciliation, upload_row.reconciliation_id)
    try:
        table, proposal = ingest.propose(session, upload_row, currency=reconciliation.currency)
    except UnsupportedFileError as exc:
        flash(request, str(exc), "error")
        return redirect(f"/conciliaciones/{reconciliation.id}")

    context = _context(session, tenant, reconciliation)
    context.update(
        active="periodos",
        upload=upload_row,
        table=table,
        proposal=proposal,
        sample=table.sample(6),
        roles=_ROLES,
    )
    return render(request, "mapping.html", context, session=session, user=user, tenant=tenant)


_ROLES = [
    ("date", "Fecha", True),
    ("amount", "Monto (una columna con signo)", False),
    ("debit", "Débito / cargo", False),
    ("credit", "Crédito / abono", False),
    ("description", "Descripción", False),
    ("reference", "Referencia o documento", False),
    ("balance", "Saldo corriente", False),
    ("gl_account", "Cuenta contable", False),
    ("third_party", "Tercero", False),
]


@router.post("/cargas/{upload_id}/mapeo")
async def mapping_save(
    upload_id: int,
    request: Request,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
):
    upload_row = get_scoped(session, Upload, upload_id, tenant)
    reconciliation = session.get(Reconciliation, upload_row.reconciliation_id)
    form = await request.form()

    mapping = {
        role: str(form[f"col_{role}"])
        for role, _, _ in _ROLES
        if form.get(f"col_{role}")
    }
    if "date" not in mapping:
        flash(request, "Debe indicar cuál columna tiene la fecha.", "error")
        return redirect(f"/cargas/{upload_id}/mapeo")
    sign_convention = "debit_credit" if {"debit", "credit"} <= set(mapping) else "single"
    if sign_convention == "single" and "amount" not in mapping:
        flash(
            request,
            "Indique la columna de monto, o bien las dos columnas de débito y crédito.",
            "error",
        )
        return redirect(f"/cargas/{upload_id}/mapeo")

    header_row = int(form.get("header_row") or 0)
    date_format = str(form.get("date_format") or "") or None
    invert = bool(form.get("invert_sign"))
    account = session.get(Account, reconciliation.bank_account_id)
    entity = session.get(Entity, account.entity_id) if account else None

    profile = None
    if form.get("recordar"):
        profile = ingest.save_profile(
            session, tenant, upload_row,
            name=str(form.get("perfil_nombre") or f"{account.bank_name} — {UploadKind(upload_row.kind).label}"),
            mapping=mapping, header_row=header_row, date_format=date_format,
            sign_convention=sign_convention, invert_sign=invert,
            client_id=entity.client_id if entity else None, account=account,
        )
    try:
        result = ingest.import_upload(
            session, tenant, upload_row,
            profile=profile, mapping=mapping, header_row=header_row,
            date_format=date_format, sign_convention=sign_convention, invert_sign=invert,
        )
    except (UnsupportedFileError, ValueError) as exc:
        flash(request, f"No se pudo importar: {exc}", "error")
        return redirect(f"/cargas/{upload_id}/mapeo")

    message = f"Importadas {len(result.lines)} partidas de «{upload_row.filename}»."
    if result.rejects:
        message += f" {len(result.rejects)} fila(s) rechazada(s), revise el detalle."
    if profile is not None:
        message += " El mapeo quedó guardado para las próximas cargas."
    flash(request, message)
    return redirect(f"/conciliaciones/{reconciliation.id}")


@router.post("/conciliaciones/{reconciliation_id}/saldos")
def set_balances(
    reconciliation_id: int,
    request: Request,
    apertura_banco: str = Form(""),
    cierre_banco: str = Form(""),
    apertura_libros: str = Form(""),
    cierre_libros: str = Form(""),
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
):
    """Balances the export did not carry, typed once by the preparer."""
    from ...money import AmountParseError, parse_amount

    reconciliation = get_scoped(session, Reconciliation, reconciliation_id, tenant)
    fields = {
        "opening_bank_cents": apertura_banco,
        "closing_bank_cents": cierre_banco,
        "opening_ledger_cents": apertura_libros,
        "closing_ledger_cents": cierre_libros,
    }
    for attribute, raw in fields.items():
        if raw.strip() == "":
            continue
        try:
            setattr(reconciliation, attribute, parse_amount(raw, reconciliation.currency))
        except AmountParseError as exc:
            flash(request, f"Saldo inválido: {exc}", "error")
            return redirect(f"/conciliaciones/{reconciliation_id}")
    refresh_totals(session, reconciliation)
    flash(request, "Saldos actualizados.")
    return redirect(f"/conciliaciones/{reconciliation_id}")


@router.post("/conciliaciones/{reconciliation_id}/ejecutar")
def run(
    reconciliation_id: int,
    request: Request,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
):
    reconciliation = get_scoped(session, Reconciliation, reconciliation_id, tenant)
    if reconciliation.status == ReconciliationStatus.PENDING_DATA:
        flash(request, "Cargue el estado de cuenta y el mayor antes de conciliar.", "error")
        return redirect(f"/conciliaciones/{reconciliation_id}")
    try:
        outcome = run_reconciliation(session, tenant, reconciliation)
    except Exception as exc:
        flash(request, f"La conciliación falló: {exc}", "error")
        return redirect(f"/conciliaciones/{reconciliation_id}")

    verification = outcome.verification
    if verification.balanced:
        flash(
            request,
            f"Conciliación ejecutada: cuadra. {verification.match_rate:.0%} de las partidas "
            "se emparejaron solas.",
        )
    else:
        flash(
            request,
            f"Conciliación ejecutada con una diferencia de "
            f"{verification.difference_cents / 100:,.2f}. Revise la cola de excepciones.",
            "error",
        )
    for warning in outcome.warnings:
        flash(request, warning, "info")
    return redirect(f"/conciliaciones/{reconciliation_id}")


# --------------------------------------------------------------------------- #
# Matches
# --------------------------------------------------------------------------- #


@router.get("/conciliaciones/{reconciliation_id}/emparejamientos")
def matches(
    reconciliation_id: int,
    request: Request,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
):
    reconciliation = get_scoped(session, Reconciliation, reconciliation_id, tenant)
    rows = list(
        session.execute(
            select(Match).where(Match.reconciliation_id == reconciliation.id)
            .order_by(Match.status, Match.stage, Match.id)
        ).scalars()
    )
    context = _context(session, tenant, reconciliation)
    context.update(
        active="periodos", tab="emparejamientos", matches=rows,
        free_movements=_free_lines(session, reconciliation, BankMovement),
        free_entries=_free_lines(session, reconciliation, LedgerEntry),
    )
    return render(request, "matches.html", context, session=session, user=user, tenant=tenant)


def _free_lines(session: Session, reconciliation: Reconciliation, model) -> list:
    return list(
        session.execute(
            select(model).where(
                model.reconciliation_id == reconciliation.id,
                model.match_id.is_(None),
            ).order_by(model.value_date, model.id)
        ).scalars()
    )


@router.post("/emparejamientos/{match_id}/aprobar")
def approve_one(
    match_id: int,
    request: Request,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
):
    match = get_scoped(session, Match, match_id, tenant)
    approve_match(session, tenant, match)
    reconciliation = session.get(Reconciliation, match.reconciliation_id)
    refresh_totals(session, reconciliation)
    flash(request, "Emparejamiento aprobado.")
    return redirect(f"/conciliaciones/{match.reconciliation_id}/emparejamientos")


@router.post("/emparejamientos/{match_id}/anular")
def reject_one(
    match_id: int,
    request: Request,
    motivo: str = Form(...),
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
):
    match = get_scoped(session, Match, match_id, tenant)
    try:
        reject_match(session, tenant, match, motivo)
    except ReviewError as exc:
        flash(request, str(exc), "error")
        return redirect(f"/conciliaciones/{match.reconciliation_id}/emparejamientos")
    reconciliation = session.get(Reconciliation, match.reconciliation_id)
    refresh_totals(session, reconciliation)
    flash(request, "Emparejamiento anulado. Las partidas vuelven a la cola y quedó registrado.")
    return redirect(f"/conciliaciones/{match.reconciliation_id}/emparejamientos")


@router.post("/conciliaciones/{reconciliation_id}/manual")
async def create_manual(
    reconciliation_id: int,
    request: Request,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
):
    reconciliation = get_scoped(session, Reconciliation, reconciliation_id, tenant)
    form = await request.form()
    movement_ids = [int(v) for v in form.getlist("movimiento")]
    entry_ids = [int(v) for v in form.getlist("asiento")]
    motivo = str(form.get("motivo") or "").strip() or None
    try:
        match = manual_match(
            session, tenant, reconciliation,
            movement_ids=movement_ids, entry_ids=entry_ids, reason=motivo,
        )
    except ReviewError as exc:
        flash(request, str(exc), "error")
        return redirect(f"/conciliaciones/{reconciliation_id}/emparejamientos")
    refresh_totals(session, reconciliation)
    flash(request, f"Emparejamiento manual {len(match.movements)}:{len(match.entries)} creado.")
    return redirect(f"/conciliaciones/{reconciliation_id}/emparejamientos")


# --------------------------------------------------------------------------- #
# Exception queue
# --------------------------------------------------------------------------- #


@router.get("/conciliaciones/{reconciliation_id}/excepciones")
def queue(
    reconciliation_id: int,
    request: Request,
    estado: str = "OPEN",
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
):
    reconciliation = get_scoped(session, Reconciliation, reconciliation_id, tenant)
    statement = select(Exception_).where(Exception_.reconciliation_id == reconciliation.id)
    if estado != "TODAS":
        statement = statement.where(Exception_.status == estado)
    rows = list(session.execute(statement).scalars())
    # Biggest and oldest first: that is the order somebody would work them in.
    rows.sort(key=lambda row: (-abs(row.amount_cents), -row.age_days))

    context = _context(session, tenant, reconciliation)
    context.update(
        active="periodos", tab="excepciones", exceptions=rows, estado=estado,
        proposals=list(
            session.execute(
                select(Match).where(
                    Match.reconciliation_id == reconciliation.id,
                    Match.status == MatchStatus.PROPOSED,
                ).order_by(Match.confidence.desc())
            ).scalars()
        ),
        categories=list(ExceptionCategory),
        team=list(session.execute(scoped(User, tenant).order_by(User.name)).scalars()),
        aging=get_settings().exception_aging_buckets,
    )
    return render(request, "queue.html", context, session=session, user=user, tenant=tenant)


@router.post("/excepciones/{exception_id}/conciliatoria")
def as_reconciling_item(
    exception_id: int,
    request: Request,
    categoria: str = Form(...),
    motivo: str = Form(""),
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
):
    exception = get_scoped(session, Exception_, exception_id, tenant)
    mark_reconciling_item(
        session, tenant, exception,
        category=ExceptionCategory(categoria), reason=motivo or None,
    )
    reconciliation = session.get(Reconciliation, exception.reconciliation_id)
    refresh_totals(session, reconciliation)
    flash(request, "Partida marcada como conciliatoria legítima.")
    return redirect(f"/conciliaciones/{exception.reconciliation_id}/excepciones")


@router.post("/excepciones/{exception_id}/diferir")
def defer(
    exception_id: int,
    request: Request,
    motivo: str = Form(...),
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
):
    exception = get_scoped(session, Exception_, exception_id, tenant)
    try:
        defer_exception(session, tenant, exception, motivo)
    except ReviewError as exc:
        flash(request, str(exc), "error")
        return redirect(f"/conciliaciones/{exception.reconciliation_id}/excepciones")
    flash(request, "Partida diferida al período siguiente. Sigue viva y sigue envejeciendo.")
    return redirect(f"/conciliaciones/{exception.reconciliation_id}/excepciones")


@router.post("/excepciones/{exception_id}/asignar")
def assign(
    exception_id: int,
    request: Request,
    usuario: str = Form(""),
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
):
    exception = get_scoped(session, Exception_, exception_id, tenant)
    try:
        assign_exception(session, tenant, exception, int(usuario) if usuario else None)
    except ReviewError as exc:
        flash(request, str(exc), "error")
        return redirect(f"/conciliaciones/{exception.reconciliation_id}/excepciones")
    flash(request, "Excepción reasignada.")
    return redirect(f"/conciliaciones/{exception.reconciliation_id}/excepciones")


@router.post("/excepciones/{exception_id}/emparejar")
def match_from_queue(
    exception_id: int,
    request: Request,
    candidato: int = Form(...),
    motivo: str = Form(""),
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
):
    """Pair straight from the queue with one of the candidates on screen."""
    exception = get_scoped(session, Exception_, exception_id, tenant)
    reconciliation = session.get(Reconciliation, exception.reconciliation_id)
    if Side(exception.side) is Side.BANK:
        movement_ids, entry_ids = [exception.movement_id], [candidato]
    else:
        movement_ids, entry_ids = [candidato], [exception.entry_id]
    try:
        manual_match(
            session, tenant, reconciliation,
            movement_ids=movement_ids, entry_ids=entry_ids,
            reason=motivo or "Emparejado desde la cola de excepciones.",
        )
    except ReviewError as exc:
        flash(request, str(exc), "error")
        return redirect(f"/conciliaciones/{reconciliation.id}/excepciones")
    refresh_totals(session, reconciliation)
    flash(request, "Partida emparejada. La corrección quedó registrada.")
    return redirect(f"/conciliaciones/{reconciliation.id}/excepciones")


@router.post("/conciliaciones/{reconciliation_id}/aprobar")
def approve_whole(
    reconciliation_id: int,
    request: Request,
    notas: str = Form(""),
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
):
    reconciliation = get_scoped(session, Reconciliation, reconciliation_id, tenant)
    try:
        approve_reconciliation(session, tenant, reconciliation, notes=notas or None)
    except ReviewError as exc:
        flash(request, str(exc), "error")
        return redirect(f"/conciliaciones/{reconciliation_id}")
    flash(request, "Conciliación aprobada. Ya puede generar los papeles de trabajo.")
    return redirect(f"/conciliaciones/{reconciliation_id}")
