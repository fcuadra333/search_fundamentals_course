"""Workpaper preview, export and download."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ...config import get_settings
from ...models import Period, Reconciliation, User
from ...tenancy import Tenant, TenancyError, get_scoped
from ...workpapers import model as workpaper_model
from ...workpapers.builder import export_period, export_reconciliation
from ..deps import current_tenant, current_user, flash, get_db, redirect, render
from .workspace import _context

router = APIRouter()


@router.get("/conciliaciones/{reconciliation_id}/papeles")
def preview(
    reconciliation_id: int,
    request: Request,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
):
    reconciliation = get_scoped(session, Reconciliation, reconciliation_id, tenant)
    workpaper = workpaper_model.build(session, reconciliation)
    context = _context(session, tenant, reconciliation)
    context.update(
        active="periodos", tab="papeles", workpaper=workpaper,
        files=_existing_files(tenant, workpaper),
    )
    return render(request, "workpaper.html", context, session=session, user=user, tenant=tenant)


def _export_dir(tenant: Tenant) -> Path:
    return get_settings().export_dir / str(tenant.firm_id)


def _existing_files(tenant: Tenant, workpaper) -> list[dict]:
    directory = _export_dir(tenant)
    if not directory.exists():
        return []
    stem = f"{workpaper.period.year}{workpaper.period.month:02d}"
    files = [
        {"name": path.name, "size": path.stat().st_size, "kind": path.suffix.lstrip(".")}
        for path in sorted(directory.iterdir())
        if path.is_file() and stem in path.name
    ]
    return files


@router.post("/conciliaciones/{reconciliation_id}/papeles")
def generate(
    reconciliation_id: int,
    request: Request,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
):
    reconciliation = get_scoped(session, Reconciliation, reconciliation_id, tenant)
    paths = export_reconciliation(session, tenant, reconciliation)
    flash(request, f"Papeles de trabajo generados: {', '.join(path.name for path in paths)}.")
    return redirect(f"/conciliaciones/{reconciliation_id}/papeles")


@router.post("/periodos/{period_id}/papeles")
def generate_period(
    period_id: int,
    request: Request,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
):
    period = get_scoped(session, Period, period_id, tenant)
    paths = export_period(session, tenant, period)
    if not paths:
        flash(request, "No hay conciliaciones que exportar en este período.", "info")
    else:
        flash(request, f"Se generaron {len(paths)} archivo(s) para {period.label}.")
    return redirect(f"/periodos/{period_id}")


@router.get("/descargas/{filename}")
def download(
    filename: str,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
):
    """Serve from the firm's own export directory only.

    The name is resolved and then checked to be inside that directory, so a
    crafted path cannot reach another firm's files or the rest of the disk.
    """
    directory = _export_dir(tenant).resolve()
    target = (directory / filename).resolve()
    if not str(target).startswith(str(directory) + "/") or not target.is_file():
        raise TenancyError("Archivo no encontrado")
    return FileResponse(target, filename=target.name)
