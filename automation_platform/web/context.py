"""Per-request context: which tenant, which user, which language.

Every request resolves all three before a view runs, and views receive them
through `g`. A view that forgets to scope a query still cannot leak another
tenant's rows, because the repositories and the connection guard both refuse.
"""

from __future__ import annotations

import sqlite3

from flask import g, redirect, request, session, url_for

from ..db import RepositorySet, connect, initialize
from ..domain.enums import Role
from ..i18n import DEFAULT_LOCALE, SUPPORTED_LOCALES, get_translator, resolve_locale
from ..tenancy import TenantContext

SESSION_ORGANIZATION = "organization_id"
SESSION_USER = "user_id"
SESSION_LOCALE = "locale"


def get_connection(app) -> sqlite3.Connection:
    if "connection" not in g:
        g.connection = connect(app.config["PLATFORM"].database_path)
        initialize(g.connection)
    return g.connection


def close_connection(_exception=None) -> None:
    connection = g.pop("connection", None)
    if connection is not None:
        connection.close()


def load_context(app) -> None:
    """Resolve tenant, user and locale for this request."""
    connection = get_connection(app)
    bootstrap = RepositorySet(connection, TenantContext("bootstrap"))

    organization_id = session.get(SESSION_ORGANIZATION)
    organization = bootstrap.organizations.get(organization_id) if organization_id else None

    user = None
    if organization is not None and session.get(SESSION_USER):
        tenant = TenantContext(organization["id"], role=Role.ADMIN)
        user = RepositorySet(connection, tenant).users.get(session[SESSION_USER])

    locale = resolve_locale(
        explicit=request.args.get("lang") or session.get(SESSION_LOCALE),
        user_locale=user["locale"] if user else None,
        organization_locale=organization["default_locale"] if organization else None,
        accept_language=request.headers.get("Accept-Language"),
    )

    g.locale = locale
    g.t = get_translator(locale)
    g.organization = organization
    g.user = user
    g.tenant = (
        TenantContext(
            organization_id=organization["id"],
            user_id=user["id"] if user else None,
            role=Role(user["role"]) if user else Role.OPERATOR,
            locale=locale,
            organization_name=organization["name"],
        )
        if organization is not None
        else None
    )
    g.repos = RepositorySet(connection, g.tenant) if g.tenant else None


def require_session():
    """Redirect to the sign-in page when there is no tenant in session."""
    if g.get("tenant") is None:
        return redirect(url_for("auth.login", next=request.path))
    return None


def switch_locale(locale: str) -> str:
    locale = locale if locale in SUPPORTED_LOCALES else DEFAULT_LOCALE
    session[SESSION_LOCALE] = locale
    return locale
