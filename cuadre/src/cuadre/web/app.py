"""FastAPI application factory."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .. import PRODUCT_NAME, __version__
from ..config import get_settings
from ..db import create_all
from ..tenancy import PeriodClosedError, RoleError, TenancyError
from .deps import NotAuthenticated, flash, templates
from .routes import audit, auth, portfolio, workpapers, workspace

STATIC_DIR = Path(__file__).parent / "static"


def create_app(*, create_schema: bool = True) -> FastAPI:
    settings = get_settings()
    settings.ensure_dirs()
    if create_schema:
        create_all()

    app = FastAPI(title=f"{PRODUCT_NAME} — conciliaciones contables", version=__version__)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="cuadre_session",
        max_age=settings.session_hours * 3600,
        same_site="lax",
        https_only=False,
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(auth.router)
    app.include_router(portfolio.router)
    app.include_router(workspace.router)
    app.include_router(workpapers.router)
    app.include_router(audit.router)

    @app.exception_handler(NotAuthenticated)
    async def _unauthenticated(request: Request, _exc: NotAuthenticated):
        return RedirectResponse(f"/entrar?siguiente={request.url.path}", status_code=303)

    @app.exception_handler(TenancyError)
    async def _tenancy(request: Request, exc: TenancyError):
        # Cross-firm access is reported as not found: the existence of another
        # firm's object is itself information.
        return templates.TemplateResponse(
            request, "error.html",
            {"request": request, "code": 404, "message": str(exc), "user": None,
             "flashes": [], "counts": {}},
            status_code=404,
        )

    @app.exception_handler(PeriodClosedError)
    async def _closed(request: Request, exc: PeriodClosedError):
        flash(request, str(exc), "error")
        return RedirectResponse(request.headers.get("referer", "/"), status_code=303)

    @app.exception_handler(RoleError)
    async def _role(request: Request, exc: RoleError):
        flash(request, str(exc), "error")
        return RedirectResponse(request.headers.get("referer", "/"), status_code=303)

    return app


app = create_app()
