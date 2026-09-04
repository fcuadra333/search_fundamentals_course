"""Sign in and sign out."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import User
from ...security import verify_password
from ...tenancy import audit
from ..deps import flash, get_db, redirect, templates

router = APIRouter()


@router.get("/entrar")
def login_form(request: Request, siguiente: str = "/"):
    if request.session.get("user_id"):
        return redirect("/")
    return templates.TemplateResponse(
        request, "login.html",
        {"request": request, "siguiente": siguiente,
         "flashes": request.session.pop("_flashes", []), "user": None, "counts": {}},
    )


@router.post("/entrar")
def login(
    request: Request,
    correo: str = Form(...),
    clave: str = Form(...),
    siguiente: str = Form("/"),
    session: Session = Depends(get_db),
):
    user = session.execute(
        select(User).where(User.email == correo.strip().lower(), User.active.is_(True))
    ).scalar_one_or_none()
    # The same message for a wrong address and a wrong password: which of the
    # two failed is information we do not owe an anonymous caller.
    if user is None or not verify_password(clave, user.password_hash):
        flash(request, "Correo o contraseña incorrectos.", "error")
        return redirect(f"/entrar?siguiente={siguiente}")

    request.session["user_id"] = user.id
    user.last_login_at = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    from ...enums import Role
    from ...tenancy import Tenant

    audit(
        session,
        Tenant(firm_id=user.firm_id, user_id=user.id, role=Role(user.role)),
        "auth.login",
        object_type="User",
        object_id=user.id,
        ip=request.client.host if request.client else None,
    )
    flash(request, f"Bienvenida, {user.name.split()[0]}.")
    return redirect(siguiente if siguiente.startswith("/") else "/")


@router.get("/salir")
def logout(request: Request):
    request.session.clear()
    return redirect("/entrar")
