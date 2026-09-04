"""The workpaper's content, read from the database and nothing else.

Everything a workpaper shows is derived here, so an export made in November
from an approved period in January reproduces January exactly. The fingerprint
over the rows is what lets two exports be proved identical.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..engine.runner import current_verification
from ..enums import ExceptionCategory, ExceptionStatus, MatchMethod, MatchStatus, Side
from ..models import (
    Account,
    BankMovement,
    Client,
    Correction,
    Entity,
    Exception_,
    Firm,
    LedgerEntry,
    Match,
    ModelCall,
    Period,
    Reconciliation,
    Run,
    Upload,
    User,
)


@dataclass
class WorkpaperSection:
    title: str
    columns: list[str]
    rows: list[list[Any]] = field(default_factory=list)


@dataclass
class Workpaper:
    firm: Firm
    client: Client
    entity: Entity
    account: Account
    period: Period
    reconciliation: Reconciliation
    statement: list[tuple[str, int, str]]
    matches: WorkpaperSection
    exceptions: WorkpaperSection
    corrections: WorkpaperSection
    sources: WorkpaperSection
    model_usage: WorkpaperSection
    prepared_by: User | None
    reviewed_by: User | None
    balanced: bool
    difference_cents: int
    match_rate: float
    fingerprint: str

    @property
    def title(self) -> str:
        return f"Conciliación bancaria — {self.account.name} — {self.period.label}"


def build(session: Session, reconciliation: Reconciliation) -> Workpaper:
    account = session.get(Account, reconciliation.bank_account_id)
    entity = session.get(Entity, account.entity_id)
    client = session.get(Client, entity.client_id)
    firm = session.get(Firm, reconciliation.firm_id)
    period = session.get(Period, reconciliation.period_id)
    verification = current_verification(session, reconciliation)

    statement = [
        ("Saldo según estado de cuenta", verification.bank_closing_cents, "bank"),
        ("(+) Depósitos en tránsito", verification.deposits_in_transit_cents, "bank"),
        ("(−) Cheques en circulación", verification.outstanding_checks_cents, "bank"),
        ("= Saldo ajustado del banco", verification.adjusted_bank_cents, "bank_total"),
        ("Saldo según libros", verification.ledger_closing_cents, "ledger"),
        ("(+) Notas de crédito no registradas", verification.bank_credits_unrecorded_cents, "ledger"),
        ("(−) Notas de débito no registradas", verification.bank_charges_unrecorded_cents, "ledger"),
        ("= Saldo ajustado de libros", verification.adjusted_ledger_cents, "ledger_total"),
        ("Diferencia", verification.difference_cents, "difference"),
    ]

    return Workpaper(
        firm=firm,
        client=client,
        entity=entity,
        account=account,
        period=period,
        reconciliation=reconciliation,
        statement=statement,
        matches=_matches_section(session, reconciliation),
        exceptions=_exceptions_section(session, reconciliation),
        corrections=_corrections_section(session, reconciliation),
        sources=_sources_section(session, reconciliation),
        model_usage=_model_section(session, reconciliation),
        prepared_by=session.get(User, reconciliation.prepared_by_id)
        if reconciliation.prepared_by_id
        else None,
        reviewed_by=session.get(User, reconciliation.reviewed_by_id)
        if reconciliation.reviewed_by_id
        else None,
        balanced=verification.balanced,
        difference_cents=verification.difference_cents,
        match_rate=verification.match_rate,
        fingerprint=_fingerprint(session, reconciliation, verification.difference_cents),
    )


def _line_label(line) -> str:
    return f"{line.value_date:%d/%m/%Y} · {line.raw_description[:60]}"


def _matches_section(session: Session, reconciliation: Reconciliation) -> WorkpaperSection:
    """Every match with its provenance: method, confidence, who approved, when."""
    section = WorkpaperSection(
        title="Emparejamientos",
        columns=[
            "N°", "Método", "Etapa", "Cardinalidad", "Monto", "Confianza", "Estado",
            "Movimientos del banco", "Asientos del libro", "Justificación",
            "Aprobado por", "Fecha de aprobación",
        ],
    )
    matches = session.execute(
        select(Match).where(Match.reconciliation_id == reconciliation.id).order_by(Match.id)
    ).scalars()
    for index, match in enumerate(matches, start=1):
        approver = session.get(User, match.approved_by_id) if match.approved_by_id else None
        section.rows.append(
            [
                index,
                MatchMethod(match.method).label,
                match.stage,
                f"{len(match.movements)}:{len(match.entries)}",
                match.amount_cents / 100,
                round(match.confidence, 3),
                MatchStatus(match.status).label,
                " | ".join(_line_label(line) for line in match.movements),
                " | ".join(_line_label(line) for line in match.entries),
                match.rationale or "",
                approver.name if approver else "Sistema (regla determinista)",
                match.approved_at.strftime("%d/%m/%Y %H:%M") if match.approved_at else "",
            ]
        )
    return section


def _exceptions_section(session: Session, reconciliation: Reconciliation) -> WorkpaperSection:
    section = WorkpaperSection(
        title="Excepciones",
        columns=[
            "Lado", "Fecha", "Descripción", "Referencia", "Monto", "Categoría",
            "Estado", "Antigüedad (días)", "Primer período", "Asignada a", "Motivo",
        ],
    )
    rows = session.execute(
        select(Exception_).where(Exception_.reconciliation_id == reconciliation.id)
        .order_by(Exception_.side, Exception_.value_date)
    ).scalars()
    for exception in rows:
        line = (
            session.get(BankMovement, exception.movement_id)
            if Side(exception.side) is Side.BANK
            else session.get(LedgerEntry, exception.entry_id)
        )
        first_period = (
            session.get(Period, exception.first_seen_period_id)
            if exception.first_seen_period_id
            else None
        )
        assignee = (
            session.get(User, exception.assigned_to_id) if exception.assigned_to_id else None
        )
        section.rows.append(
            [
                Side(exception.side).label,
                exception.value_date.strftime("%d/%m/%Y") if exception.value_date else "",
                (line.raw_description if line else "")[:90],
                (line.reference if line else "") or "",
                exception.amount_cents / 100,
                ExceptionCategory(exception.category).label,
                ExceptionStatus(exception.status).label,
                exception.age_days,
                first_period.label if first_period else "",
                assignee.name if assignee else "",
                exception.reason or "",
            ]
        )
    return section


def _corrections_section(session: Session, reconciliation: Reconciliation) -> WorkpaperSection:
    """Every human override of a system proposal. Never omitted from a workpaper:
    it is what tells a reviewer where judgement was applied."""
    section = WorkpaperSection(
        title="Correcciones humanas",
        columns=["Fecha", "Usuario", "Acción", "Lo que propuso el sistema",
                 "Lo que quedó", "Motivo"],
    )
    rows = session.execute(
        select(Correction).where(Correction.reconciliation_id == reconciliation.id)
        .order_by(Correction.id)
    ).scalars()
    for correction in rows:
        user = session.get(User, correction.user_id) if correction.user_id else None
        section.rows.append(
            [
                correction.created_at.strftime("%d/%m/%Y %H:%M"),
                user.name if user else "",
                correction.action,
                json.dumps(correction.proposed, ensure_ascii=False)[:300],
                json.dumps(correction.corrected, ensure_ascii=False)[:300],
                correction.reason or "",
            ]
        )
    return section


def _sources_section(session: Session, reconciliation: Reconciliation) -> WorkpaperSection:
    """The files this reconciliation was built from, by hash."""
    section = WorkpaperSection(
        title="Archivos de origen",
        columns=["Archivo", "Tipo", "SHA-256", "Bytes", "Filas leídas",
                 "Importadas", "Rechazadas", "Cargado por", "Fecha"],
    )
    from ..enums import UploadKind

    uploads = session.execute(
        select(Upload).where(Upload.reconciliation_id == reconciliation.id).order_by(Upload.id)
    ).scalars()
    for upload in uploads:
        user = session.get(User, upload.uploaded_by_id) if upload.uploaded_by_id else None
        section.rows.append(
            [
                upload.filename,
                UploadKind(upload.kind).label,
                upload.content_hash,
                upload.byte_size,
                upload.rows_read,
                upload.rows_imported,
                upload.rows_rejected,
                user.name if user else "",
                upload.created_at.strftime("%d/%m/%Y %H:%M"),
            ]
        )
    return section


def _model_section(session: Session, reconciliation: Reconciliation) -> WorkpaperSection:
    """What the model was asked, what it cost, and whether it validated."""
    section = WorkpaperSection(
        title="Uso de modelos",
        columns=["Corrida", "Proveedor", "Modelo", "Por lotes", "Tokens entrada",
                 "Tokens salida", "Costo (US$)", "Intentos", "Válida", "Error de esquema"],
    )
    run_ids = [
        run.id
        for run in session.execute(
            select(Run).where(Run.reconciliation_id == reconciliation.id)
        ).scalars()
    ]
    if not run_ids:
        return section
    calls = session.execute(
        select(ModelCall).where(ModelCall.run_id.in_(run_ids)).order_by(ModelCall.id)
    ).scalars()
    for call in calls:
        section.rows.append(
            [
                call.run_id, call.provider, call.model, "Sí" if call.batched else "No",
                call.input_tokens, call.output_tokens, round(call.cost_micros / 1_000_000, 6),
                call.attempts, "Sí" if call.ok else "No", call.schema_error or "",
            ]
        )
    return section


def _fingerprint(
    session: Session, reconciliation: Reconciliation, difference_cents: int
) -> str:
    """Hash over what the workpaper asserts. Two exports of the same approved
    period produce the same value; if it moves, the underlying data moved."""
    matches = session.execute(
        select(Match).where(Match.reconciliation_id == reconciliation.id).order_by(Match.id)
    ).scalars()
    payload = {
        "reconciliation": reconciliation.id,
        "bank_closing": reconciliation.closing_bank_cents,
        "ledger_closing": reconciliation.closing_ledger_cents,
        "difference": difference_cents,
        "matches": [
            {
                "method": match.method,
                "status": match.status,
                "movements": sorted(line.id for line in match.movements),
                "entries": sorted(line.id for line in match.entries),
                "amount": match.amount_cents,
            }
            for match in matches
        ],
        "uploads": sorted(
            upload.content_hash
            for upload in session.execute(
                select(Upload).where(Upload.reconciliation_id == reconciliation.id)
            ).scalars()
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def generated_stamp() -> str:
    return dt.datetime.now().strftime("%d/%m/%Y %H:%M")
