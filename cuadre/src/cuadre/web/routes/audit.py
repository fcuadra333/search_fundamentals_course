"""Audit trail and the record of human corrections."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import AuditLog, Client, Correction, ModelCall, Rule, User
from ...tenancy import Tenant, scoped
from ..deps import current_tenant, current_user, get_db, render

router = APIRouter()


@router.get("/bitacora")
def audit_log(
    request: Request,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
):
    entries = list(
        session.execute(
            scoped(AuditLog, tenant).order_by(AuditLog.at.desc(), AuditLog.id.desc()).limit(200)
        ).scalars()
    )
    return render(request, "audit.html", {"active": "bitacora", "entries": entries},
                  session=session, user=user, tenant=tenant)


@router.get("/correcciones")
def corrections(
    request: Request,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
):
    """Every override, and the rules they produced.

    In v1 the rules are recorded and shown, not applied: they are the seed of
    the learning loop, and showing them is how a firm sees it accumulating.
    """
    rows = list(
        session.execute(
            scoped(Correction, tenant).order_by(Correction.id.desc()).limit(200)
        ).scalars()
    )
    rules = list(session.execute(scoped(Rule, tenant).order_by(Rule.id.desc())).scalars())
    clients = {client.id: client for client in session.execute(scoped(Client, tenant)).scalars()}
    return render(
        request, "corrections.html",
        {"active": "correcciones", "rows": rows, "rules": rules, "clients": clients},
        session=session, user=user, tenant=tenant,
    )


@router.get("/consumo")
def model_usage(
    request: Request,
    session: Session = Depends(get_db),
    user: User = Depends(current_user),
    tenant: Tenant = Depends(current_tenant),
):
    """Token and cost ledger. Every call, whatever the provider."""
    calls = list(
        session.execute(
            scoped(ModelCall, tenant).order_by(ModelCall.id.desc()).limit(200)
        ).scalars()
    )
    totals = {
        "llamadas": len(calls),
        "entrada": sum(call.input_tokens for call in calls),
        "salida": sum(call.output_tokens for call in calls),
        "costo_micros": sum(call.cost_micros for call in calls),
        "fallidas": sum(1 for call in calls if not call.ok),
    }
    return render(request, "usage.html", {"active": "consumo", "calls": calls, "totals": totals},
                  session=session, user=user, tenant=tenant)
