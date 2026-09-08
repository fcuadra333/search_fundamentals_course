"""Command line entry points.

    python -m automation_platform.cli demo      # build the demo database
    python -m automation_platform.cli serve     # run the dashboard
    python -m automation_platform.cli export    # write an evidence report
    python -m automation_platform.cli health    # print health per skill
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import health as health_module
from .config import Config
from .db import RepositorySet, connect, initialize
from .domain.enums import Role
from .i18n import SUPPORTED_LOCALES, Translator
from .seed import seed
from .tenancy import TenantContext


def _repos(config: Config, slug: str | None, locale: str):
    conn = connect(config.database_path)
    initialize(conn)
    bootstrap = RepositorySet(conn, TenantContext("bootstrap"))
    organizations = bootstrap.organizations.all()
    if not organizations:
        raise SystemExit("no organizations: run `demo` first")
    row = bootstrap.organizations.by_slug(slug) if slug else organizations[0]
    if row is None:
        raise SystemExit(f"unknown organization: {slug}")
    tenant = TenantContext(
        row["id"], role=Role.ADMIN, locale=locale, organization_name=row["name"]
    )
    return conn, RepositorySet(conn, tenant), row


def command_demo(args) -> int:
    config = Config.from_env()
    path = Path(args.database or config.database_path)
    if path.exists() and not args.force:
        raise SystemExit(f"{path} already exists; pass --force to rebuild")
    if path.exists():
        path.unlink()
    conn = connect(path)
    summary = seed(conn)
    conn.close()
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\ndatabase: {path}")
    return 0


def command_serve(args) -> int:
    from .web import create_app

    config = Config.from_env()
    if args.database:
        config.database_path = Path(args.database)
    app = create_app(config)
    app.run(host=args.host, port=args.port, debug=args.debug)
    return 0


def command_export(args) -> int:
    from .export import EvidenceRange, EvidenceReport

    config = Config.from_env()
    conn, repos, _row = _repos(config, args.organization, args.locale)
    report = EvidenceReport(
        repos,
        Translator(args.locale),
        span=EvidenceRange(args.start, args.end) if args.start and args.end else None,
    )
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    path = report.to_pdf(str(target)) if target.suffix == ".pdf" else report.to_xlsx(str(target))
    conn.close()
    print(path)
    return 0


def command_health(args) -> int:
    config = Config.from_env()
    conn, repos, row = _repos(config, args.organization, args.locale)
    translate = Translator(args.locale)
    print(f"{row['name']}\n")
    summary = health_module.organization_summary(repos)
    print(f"{translate('dashboard.intervention_rate')}: {summary['intervention_rate']:.1%}")
    print(f"{translate('dashboard.success_rate')}: {summary['success_rate']:.1%}\n")
    for entry in health_module.organization_health(repos):
        print(
            f"  {entry.name[:38]:38} "
            f"{translate('health.success_rate')} {entry.success_rate:6.1%}  "
            f"{translate('health.intervention_rate')} {entry.intervention_rate:6.1%}  "
            f"{translate(entry.trend_key)}"
        )
    conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="automation_platform")
    parser.add_argument("--locale", default="es", choices=SUPPORTED_LOCALES)
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser("demo", help="build the demo database")
    demo.add_argument("--database")
    demo.add_argument("--force", action="store_true")
    demo.set_defaults(handler=command_demo)

    serve = subparsers.add_parser("serve", help="run the dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=4000)
    serve.add_argument("--database")
    serve.add_argument("--debug", action="store_true")
    serve.set_defaults(handler=command_serve)

    export = subparsers.add_parser("export", help="write an evidence report")
    export.add_argument("output")
    export.add_argument("--organization")
    export.add_argument("--start")
    export.add_argument("--end")
    export.set_defaults(handler=command_export)

    health = subparsers.add_parser("health", help="print health per skill")
    health.add_argument("--organization")
    health.set_defaults(handler=command_health)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
