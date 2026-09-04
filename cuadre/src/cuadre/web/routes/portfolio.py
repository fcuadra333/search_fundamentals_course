"""Work queue, portfolio and periods."""

from __future__ import annotations

import calendar
import datetime as dt

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...enums import (
    AccountKind,
    ExceptionStatus,
    MatchStatus,
    PeriodStatus,
    ReconciliationStatus,
    Role,
)
from ...models import (
    Account,
    Client,
    Entity,
    Exception_,
    Match,
    Period,
    Reconciliation,
    User,
)
from ...review import close_period
from ...tenancy import Tenant, get_scoped, require_role, scoped
from ..deps import current_tenant, current_user, flash, get_db, redirect, render

router = APIRouter()


@router.get("/")
def home(
    request: Request,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
):
    """The work queue. Not a dashboard: what is on this desk, in order."""
    reconciliations = list(
        session.execute(
            scoped(Reconciliation, tenant)
            .join(Period, Period.id == Reconciliation.period_id)
            .where(Period.status != PeriodStatus.CLOSED)
            .order_by(Period.end_date.desc(), Reconciliation.id)
        ).scalars()
    )
    rows = [_reconciliation_row(session, item) for item in reconciliations]
    by_status: dict[str, int] = {}
    for row in rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1

    open_exceptions = session.execute(
        select(func.count(Exception_.id), func.coalesce(func.sum(Exception_.amount_cents), 0))
        .where(Exception_.firm_id == tenant.firm_id, Exception_.status == ExceptionStatus.OPEN)
    ).one()
    aged = session.execute(
        select(func.count(Exception_.id)).where(
            Exception_.firm_id == tenant.firm_id,
            Exception_.status.in_([ExceptionStatus.OPEN, ExceptionStatus.RECONCILING_ITEM]),
            Exception_.age_days > 90,
        )
    ).scalar_one()
    proposals = session.execute(
        select(func.count(Match.id)).where(
            Match.firm_id == tenant.firm_id, Match.status == MatchStatus.PROPOSED
        )
    ).scalar_one()

    return render(
        request, "home.html",
        {
            "active": "home",
            "rows": rows,
            "by_status": by_status,
            "open_exceptions": open_exceptions[0],
            "open_exceptions_amount": open_exceptions[1],
            "aged": aged,
            "proposals": proposals,
        },
        session=session, user=user, tenant=tenant,
    )


def _reconciliation_row(session: Session, reconciliation: Reconciliation) -> dict:
    period = session.get(Period, reconciliation.period_id)
    account = session.get(Account, reconciliation.bank_account_id)
    entity = session.get(Entity, account.entity_id) if account else None
    client = session.get(Client, entity.client_id) if entity else None
    open_exceptions = session.execute(
        select(func.count(Exception_.id)).where(
            Exception_.reconciliation_id == reconciliation.id,
            Exception_.status == ExceptionStatus.OPEN,
        )
    ).scalar_one()
    pending = session.execute(
        select(func.count(Match.id)).where(
            Match.reconciliation_id == reconciliation.id, Match.status == MatchStatus.PROPOSED
        )
    ).scalar_one()
    return {
        "reconciliation": reconciliation,
        "period": period,
        "account": account,
        "entity": entity,
        "client": client,
        "status": ReconciliationStatus(reconciliation.status).label,
        "open_exceptions": open_exceptions,
        "pending": pending,
    }


@router.get("/clientes")
def clients(
    request: Request,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
):
    rows = []
    for client in session.execute(
        scoped(Client, tenant).order_by(Client.name)
    ).scalars():
        entities = list(
            session.execute(select(Entity).where(Entity.client_id == client.id)).scalars()
        )
        accounts = session.execute(
            select(func.count(Account.id)).where(
                Account.entity_id.in_([e.id for e in entities] or [0]),
                Account.kind == AccountKind.BANK,
            )
        ).scalar_one()
        rows.append({"client": client, "entities": entities, "accounts": accounts})
    return render(request, "clients.html", {"active": "clientes", "rows": rows},
                  session=session, user=user, tenant=tenant)


@router.get("/clientes/{client_id}")
def client_detail(
    client_id: int,
    request: Request,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
):
    client = get_scoped(session, Client, client_id, tenant)
    entities = list(
        session.execute(select(Entity).where(Entity.client_id == client.id)).scalars()
    )
    blocks = []
    for entity in entities:
        accounts = list(
            session.execute(
                select(Account).where(Account.entity_id == entity.id, Account.kind == AccountKind.BANK)
            ).scalars()
        )
        periods = list(
            session.execute(
                select(Period).where(Period.entity_id == entity.id)
                .order_by(Period.year.desc(), Period.month.desc())
            ).scalars()
        )
        blocks.append({"entity": entity, "accounts": accounts, "periods": periods})
    return render(request, "client_detail.html",
                  {"active": "clientes", "client": client, "blocks": blocks},
                  session=session, user=user, tenant=tenant)


@router.post("/entidades/{entity_id}/periodos")
def create_period(
    entity_id: int,
    request: Request,
    anio: int = Form(...),
    mes: int = Form(...),
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
):
    """Open a month and lay out one reconciliation per bank account."""
    entity = get_scoped(session, Entity, entity_id, tenant)
    existing = session.execute(
        select(Period).where(
            Period.entity_id == entity.id, Period.year == anio, Period.month == mes
        )
    ).scalar_one_or_none()
    if existing is not None:
        flash(request, f"El período {existing.label} ya existe.", "info")
        return redirect(f"/periodos/{existing.id}")

    last_day = calendar.monthrange(anio, mes)[1]
    period = Period(
        firm_id=tenant.firm_id, entity_id=entity.id, year=anio, month=mes,
        start_date=dt.date(anio, mes, 1), end_date=dt.date(anio, mes, last_day),
        status=PeriodStatus.OPEN,
    )
    session.add(period)
    session.flush()

    accounts = list(
        session.execute(
            select(Account).where(
                Account.entity_id == entity.id,
                Account.kind == AccountKind.BANK,
                Account.active.is_(True),
            )
        ).scalars()
    )
    previous = session.execute(
        select(Period).where(Period.entity_id == entity.id, Period.end_date < period.start_date)
        .order_by(Period.end_date.desc()).limit(1)
    ).scalar_one_or_none()

    for account in accounts:
        opening_bank = opening_ledger = 0
        if previous is not None:
            prior = session.execute(
                select(Reconciliation).where(
                    Reconciliation.period_id == previous.id,
                    Reconciliation.bank_account_id == account.id,
                )
            ).scalar_one_or_none()
            if prior is not None:
                # The closing balances of the month just approved are this
                # month's openings; typing them again is how errors get in.
                opening_bank = prior.closing_bank_cents
                opening_ledger = prior.closing_ledger_cents
        session.add(
            Reconciliation(
                firm_id=tenant.firm_id, period_id=period.id, bank_account_id=account.id,
                ledger_account_id=account.counterpart_account_id,
                currency=account.currency,
                opening_bank_cents=opening_bank, opening_ledger_cents=opening_ledger,
                status=ReconciliationStatus.PENDING_DATA,
            )
        )
    flash(request, f"Período {period.label} abierto con {len(accounts)} cuenta(s).")
    return redirect(f"/periodos/{period.id}")


@router.get("/periodos")
def periods(
    request: Request,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
):
    rows = []
    for period in session.execute(
        scoped(Period, tenant).order_by(Period.year.desc(), Period.month.desc(), Period.id)
    ).scalars():
        entity = session.get(Entity, period.entity_id)
        client = session.get(Client, entity.client_id) if entity else None
        reconciliations = period.reconciliations
        rows.append(
            {
                "period": period, "entity": entity, "client": client,
                "total": len(reconciliations),
                "approved": sum(
                    1 for r in reconciliations if r.status == ReconciliationStatus.APPROVED
                ),
                "balanced": sum(1 for r in reconciliations if r.balanced),
            }
        )
    return render(request, "periods.html", {"active": "periodos", "rows": rows},
                  session=session, user=user, tenant=tenant)


@router.get("/periodos/{period_id}")
def period_detail(
    period_id: int,
    request: Request,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
):
    period = get_scoped(session, Period, period_id, tenant)
    entity = session.get(Entity, period.entity_id)
    client = session.get(Client, entity.client_id) if entity else None
    rows = [_reconciliation_row(session, item) for item in period.reconciliations]
    return render(
        request, "period_detail.html",
        {"active": "periodos", "period": period, "entity": entity, "client": client, "rows": rows},
        session=session, user=user, tenant=tenant,
    )


@router.post("/periodos/{period_id}/cerrar")
def close(
    period_id: int,
    request: Request,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
):
    period = get_scoped(session, Period, period_id, tenant)
    from ...review import ReviewError

    try:
        close_period(session, tenant, period)
        flash(request, f"Período {period.label} cerrado. Queda inmutable.")
    except ReviewError as exc:
        flash(request, str(exc), "error")
    return redirect(f"/periodos/{period_id}")


@router.get("/equipo")
def team(
    request: Request,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
):
    require_role(tenant, Role.ADMIN, Role.REVIEWER)
    users = list(session.execute(scoped(User, tenant).order_by(User.name)).scalars())
    return render(request, "team.html", {"active": "equipo", "users": users},
                  session=session, user=user, tenant=tenant)
