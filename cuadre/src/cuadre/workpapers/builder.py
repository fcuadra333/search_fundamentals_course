"""Producing the workpaper files."""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Account, Period, Reconciliation
from ..tenancy import Tenant, audit
from . import excel, model, pdf


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return cleaned or "papel"


def export_reconciliation(
    session: Session, tenant: Tenant, reconciliation: Reconciliation
) -> list[Path]:
    workpaper = model.build(session, reconciliation)
    directory = get_settings().export_dir / str(tenant.firm_id)
    stem = (
        f"{_slug(workpaper.client.name)}-{_slug(workpaper.account.name)}"
        f"-{workpaper.period.year}{workpaper.period.month:02d}"
    )
    xlsx = excel.write_workbook([workpaper], directory / f"{stem}.xlsx")
    pdf_path = pdf.write_pdf(workpaper, directory / f"{stem}.pdf")
    audit(
        session, tenant, "workpaper.exported", object_type="Reconciliation",
        object_id=reconciliation.id,
        detail={"huella": workpaper.fingerprint, "archivos": [xlsx.name, pdf_path.name]},
    )
    return [xlsx, pdf_path]


def export_period(session: Session, tenant: Tenant, period: Period) -> list[Path]:
    """One workbook for the whole entity: one statement sheet per account."""
    reconciliations = list(
        session.execute(
            select(Reconciliation)
            .join(Account, Account.id == Reconciliation.bank_account_id)
            .where(Reconciliation.period_id == period.id)
            .order_by(Account.name)
        ).scalars()
    )
    workpapers = [model.build(session, item) for item in reconciliations]
    if not workpapers:
        return []
    directory = get_settings().export_dir / str(tenant.firm_id)
    stem = f"{_slug(workpapers[0].entity.name)}-{period.year}{period.month:02d}"
    paths = [excel.write_workbook(workpapers, directory / f"{stem}.xlsx")]
    for workpaper in workpapers:
        paths.append(
            pdf.write_pdf(
                workpaper,
                directory / f"{stem}-{_slug(workpaper.account.name)}.pdf",
            )
        )
    audit(session, tenant, "workpaper.exported_period", object_type="Period",
          object_id=period.id, detail={"cuentas": len(workpapers)})
    return paths
