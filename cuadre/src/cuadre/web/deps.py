"""Request plumbing: database session, current user, templates and flashes."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..enums import (
    ExceptionCategory,
    ExceptionStatus,
    MatchMethod,
    MatchStatus,
    PeriodStatus,
    ReconciliationStatus,
    Role,
    UploadKind,
    UploadStatus,
)
from ..db import get_session_factory
from ..models import Exception_, Match, Reconciliation, User
from ..money import format_amount
from ..tenancy import Tenant

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_MONTHS = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


def money(cents: int | None, currency: str = "NIO") -> str:
    return format_amount(cents or 0, currency)


def fecha(value: dt.date | dt.datetime | None) -> str:
    if value is None:
        return "—"
    return f"{value.day:02d}/{value.month:02d}/{value.year}"


def fecha_larga(value: dt.date | None) -> str:
    if value is None:
        return "—"
    return f"{value.day} de {_MONTHS[value.month - 1]} de {value.year}"


def fechahora(value: dt.datetime | None) -> str:
    if value is None:
        return "—"
    return f"{fecha(value)} {value.hour:02d}:{value.minute:02d}"


def iniciales(name: str | None) -> str:
    parts = [p for p in (name or "").split() if p]
    return "".join(p[0].upper() for p in parts[:2]) or "?"


def antiguedad(days: int) -> str:
    if days <= 0:
        return "hoy"
    if days == 1:
        return "1 día"
    return f"{days} días"


templates.env.filters.update(
    money=money, fecha=fecha, fecha_larga=fecha_larga, fechahora=fechahora,
    iniciales=iniciales, antiguedad=antiguedad,
)
templates.env.globals.update(
    PeriodStatus=PeriodStatus,
    ReconciliationStatus=ReconciliationStatus,
    ExceptionStatus=ExceptionStatus,
    ExceptionCategory=ExceptionCategory,
    MatchStatus=MatchStatus,
    MatchMethod=MatchMethod,
    Role=Role,
    UploadKind=UploadKind,
    UploadStatus=UploadStatus,
)


def get_db() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


class NotAuthenticated(Exception):
    """Raised by the dependency; the handler turns it into a redirect."""


def current_user(request: Request, session: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise NotAuthenticated()
    user = session.get(User, user_id)
    if user is None or not user.active:
        request.session.clear()
        raise NotAuthenticated()
    return user


def current_tenant(user: User = Depends(current_user)) -> Tenant:
    return Tenant(firm_id=user.firm_id, user_id=user.id, role=Role(user.role))


def flash(request: Request, message: str, level: str = "ok") -> None:
    request.session.setdefault("_flashes", []).append({"level": level, "text": message})


def _pop_flashes(request: Request) -> list[dict[str, str]]:
    flashes = request.session.pop("_flashes", [])
    return flashes


def _sidebar_counts(session: Session, tenant: Tenant) -> dict[str, int]:
    open_exceptions = session.execute(
        select(func.count(Exception_.id)).where(
            Exception_.firm_id == tenant.firm_id,
            Exception_.status == ExceptionStatus.OPEN,
        )
    ).scalar_one()
    proposals = session.execute(
        select(func.count(Match.id)).where(
            Match.firm_id == tenant.firm_id, Match.status == MatchStatus.PROPOSED
        )
    ).scalar_one()
    in_review = session.execute(
        select(func.count(Reconciliation.id)).where(
            Reconciliation.firm_id == tenant.firm_id,
            Reconciliation.status.in_(
                [ReconciliationStatus.IN_REVIEW, ReconciliationStatus.READY,
                 ReconciliationStatus.PENDING_DATA]
            ),
        )
    ).scalar_one()
    return {"excepciones": open_exceptions, "propuestas": proposals, "trabajo": in_review}


def render(
    request: Request,
    template: str,
    context: dict[str, Any],
    *,
    session: Session | None = None,
    user: User | None = None,
    tenant: Tenant | None = None,
    status_code: int = 200,
):
    base: dict[str, Any] = {
        "request": request,
        "user": user,
        "tenant": tenant,
        "flashes": _pop_flashes(request),
        "counts": _sidebar_counts(session, tenant) if (session and tenant) else {},
        "active": context.get("active", ""),
    }
    base.update(context)
    return templates.TemplateResponse(request, template, base, status_code=status_code)


def redirect(url: str) -> RedirectResponse:
    """303 so a POST that succeeded is never re-submitted by a refresh."""
    return RedirectResponse(url, status_code=303)


def not_found(message: str = "No encontrado") -> HTTPException:
    return HTTPException(status_code=404, detail=message)
