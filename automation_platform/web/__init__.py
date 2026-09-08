"""Flask application factory."""

from __future__ import annotations

from pathlib import Path

from flask import Flask

from ..config import Config
from . import views
from .context import close_connection, get_connection, load_context


def create_app(config: Config | None = None, *, database_path: str | Path | None = None) -> Flask:
    config = config or Config.from_env()
    if database_path is not None:
        config.database_path = Path(database_path)

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["PLATFORM"] = config
    app.secret_key = config.secret_key

    @app.before_request
    def _before():
        get_connection(app)
        load_context(app)

    app.teardown_appcontext(close_connection)

    @app.context_processor
    def _globals():
        return views.template_globals()

    app.register_blueprint(views.auth)
    app.register_blueprint(views.main)
    views.register_error_handlers(app)

    # Filters used across the templates. They format, they never translate:
    # translation always goes through `t(...)`.
    app.jinja_env.filters["money"] = lambda value: f"{float(value or 0):,.4f}"
    app.jinja_env.filters["percent"] = lambda value: f"{float(value or 0) * 100:.1f}%"
    app.jinja_env.filters["short"] = lambda value, size=8: str(value or "")[-size:]
    app.jinja_env.filters["stamp"] = lambda value: str(value or "").replace("T", " ")[:19]
    app.jinja_env.filters["duration"] = _duration
    app.jinja_env.filters["fromjson"] = _fromjson

    return app


def _duration(milliseconds) -> str:
    value = int(milliseconds or 0)
    if value < 1000:
        return f"{value} ms"
    seconds = value / 1000
    if seconds < 60:
        return f"{seconds:.1f} s"
    return f"{int(seconds // 60)} min {int(seconds % 60)} s"


def _fromjson(raw):
    from ..db import loads

    return loads(raw, [])
