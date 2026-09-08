"""Database access with a tenant-scope guard.

The guard is defence in depth. Repositories already add `organization_id` to
every statement; the guard makes a missing filter fail loudly in development and
in tests instead of quietly returning another tenant's rows.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..domain.errors import InvariantViolation, TenantScopeError

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Tables that hold tenant data. `audit_log` is here too: it is append-only, but
# reads still have to be scoped.
TENANT_SCOPED_TABLES = frozenset(
    {
        "users",
        "areas",
        "job_roles",
        "job_role_skills",
        "applications",
        "skills",
        "skill_steps",
        "processes",
        "process_nodes",
        "process_edges",
        "executions",
        "step_executions",
        "interventions",
        "approvals",
        "demonstrations",
        "skill_proposals",
        "certifications",
        "selector_degradations",
        "drift_detections",
        "audit_log",
    }
)

# `organizations` is the tenant root itself, so it is exempt from the guard.
UNSCOPED_TABLES = frozenset({"organizations", "sqlite_master", "sqlite_temp_master"})

_TABLE_REFERENCE = re.compile(
    r"\b(?:from|join|into|update|table)\s+[\"'`\[]?(\w+)[\"'`\]]?", re.IGNORECASE
)
_ORGANIZATION_PREDICATE = re.compile(r"\borganization_id\b", re.IGNORECASE)
_WRITE_STATEMENT = re.compile(r"^\s*(insert|update|delete|replace)\b", re.IGNORECASE)

# SQLite RAISE(ABORT, 'invariant:<name>') messages surface as IntegrityError.
_INVARIANT_MESSAGE = re.compile(r"invariant:(\w+)")

INVARIANT_MESSAGE_KEYS = {
    "missing_postcondition": "error.missing_postcondition",
    "screen_budget": "error.screen_budget",
    "not_certified": "error.not_certified",
    "approval_required": "error.approval_required",
    "audit_immutable": "error.audit_immutable",
}


def utcnow() -> str:
    """Timestamps are ISO-8601 UTC strings everywhere."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    """Readable identifiers: `skl_3f2a...`. The prefix is for humans reading logs."""
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def referenced_tables(sql: str) -> set[str]:
    return {match.lower() for match in _TABLE_REFERENCE.findall(sql)}


def statement_needs_scope(sql: str) -> bool:
    """True when the statement reaches a tenant table and must name the tenant."""
    tables = referenced_tables(sql) - UNSCOPED_TABLES
    return bool(tables & TENANT_SCOPED_TABLES)


class TenantGuardedCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=(), /):  # noqa: D102 - sqlite3 signature
        _assert_scoped(sql)
        try:
            return super().execute(sql, parameters)
        except sqlite3.IntegrityError as exc:
            raise _translate_integrity_error(exc) from exc

    def executemany(self, sql, seq_of_parameters, /):  # noqa: D102
        _assert_scoped(sql)
        try:
            return super().executemany(sql, seq_of_parameters)
        except sqlite3.IntegrityError as exc:
            raise _translate_integrity_error(exc) from exc


class TenantGuardedConnection(sqlite3.Connection):
    """Connection whose cursors refuse unscoped statements."""

    def cursor(self, factory=TenantGuardedCursor):  # noqa: D102
        return super().cursor(factory)

    def execute(self, sql, parameters=(), /):  # noqa: D102
        cur = self.cursor()
        return cur.execute(sql, parameters)

    def executemany(self, sql, parameters, /):  # noqa: D102
        cur = self.cursor()
        return cur.executemany(sql, parameters)


def _assert_scoped(sql: str) -> None:
    if not statement_needs_scope(sql):
        return
    if _ORGANIZATION_PREDICATE.search(sql):
        return
    # A write always has to say which tenant it belongs to; a read always has to
    # say which tenant it is allowed to see.
    kind = "write" if _WRITE_STATEMENT.match(sql) else "read"
    raise TenantScopeError(statement=" ".join(sql.split())[:160], kind=kind)


def _translate_integrity_error(exc: sqlite3.IntegrityError) -> Exception:
    match = _INVARIANT_MESSAGE.search(str(exc))
    if not match:
        return exc
    name = match.group(1)
    error = InvariantViolation(invariant=name)
    error.message_key = INVARIANT_MESSAGE_KEYS.get(name, "error.invariant")
    return error


def connect(database_path: str | Path, *, guard: bool = True) -> sqlite3.Connection:
    """Open a connection with foreign keys on and rows as mappings."""
    path = str(database_path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        path,
        factory=TenantGuardedConnection if guard else sqlite3.Connection,
        detect_types=0,
        isolation_level=None,  # explicit transactions, no implicit BEGIN
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def initialize(conn: sqlite3.Connection) -> None:
    """Create the schema. Safe to run against an existing database."""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


class transaction:
    """Context manager for an explicit transaction.

    `isolation_level=None` means sqlite3 never begins one for us, so every write
    path states its own boundaries.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._owned = False

    def __enter__(self) -> sqlite3.Connection:
        if not self.conn.in_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
            self._owned = True
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        if not self._owned:
            return False
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False
