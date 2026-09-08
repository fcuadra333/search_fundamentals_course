"""Tenant-scoped data access.

Every repository takes a `TenantContext` and injects `organization_id` into each
statement it builds. Callers never pass an organization id by hand, so they
cannot get it wrong.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Sequence

from ..domain.enums import (
    ApprovalStatus,
    ExecutionStatus,
    Measurement,
    SkillStatus,
)
from ..domain.errors import CrossTenantAccessError
from ..tenancy import TenantContext
from .database import new_id, transaction, utcnow


def loads(raw: str | None, default: Any = None) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Repository:
    """Base class holding the connection and the tenant."""

    table: str = ""
    id_prefix: str = "row"

    def __init__(self, conn: sqlite3.Connection, tenant: TenantContext):
        self.conn = conn
        self.tenant = tenant

    # -- helpers ------------------------------------------------------------

    @property
    def organization_id(self) -> str:
        return self.tenant.organization_id

    def _table_columns(self, table: str) -> set[str]:
        cache = getattr(self.conn, "_column_cache", None)
        if cache is None:
            cache = {}
            setattr(self.conn, "_column_cache", cache)
        if table not in cache:
            rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            cache[table] = {row[1] for row in rows}
        return cache[table]

    def _insert(self, values: dict[str, Any], *, table: str | None = None) -> str:
        table = table or self.table
        known = self._table_columns(table)
        unknown = set(values) - known
        if unknown:
            # A typo in a column name must not be swallowed: it would write a row
            # with a silently missing field.
            raise ValueError(f"unknown columns for {table}: {sorted(unknown)}")
        payload = dict(values)
        payload["organization_id"] = self.organization_id
        if "id" in known:
            payload.setdefault("id", new_id(self.id_prefix))
        if "created_at" in known:
            payload.setdefault("created_at", utcnow())
        columns = ", ".join(payload)
        placeholders = ", ".join(f":{column}" for column in payload)
        self.conn.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", payload
        )
        return payload["id"]

    def _update(self, row_id: str, values: dict[str, Any], *, table: str | None = None) -> None:
        table = table or self.table
        if not values:
            return
        unknown = set(values) - self._table_columns(table)
        if unknown:
            raise ValueError(f"unknown columns for {table}: {sorted(unknown)}")
        payload = dict(values)
        payload["row_id"] = row_id
        payload["organization_id"] = self.organization_id
        assignments = ", ".join(f"{column} = :{column}" for column in values)
        self.conn.execute(
            f"UPDATE {table} SET {assignments} "
            f"WHERE id = :row_id AND organization_id = :organization_id",
            payload,
        )

    def _get(self, row_id: str, *, table: str | None = None) -> sqlite3.Row | None:
        table = table or self.table
        cur = self.conn.execute(
            f"SELECT * FROM {table} WHERE id = ? AND organization_id = ?",
            (row_id, self.organization_id),
        )
        return cur.fetchone()

    def _require(self, row_id: str, *, table: str | None = None) -> sqlite3.Row:
        row = self._get(row_id, table=table)
        if row is None:
            # Deliberately indistinguishable from "does not exist": a tenant must
            # not learn that an id exists somewhere else.
            raise CrossTenantAccessError(entity=table or self.table, entity_id=row_id)
        return row

    def _select(
        self,
        where: str = "",
        params: Sequence[Any] = (),
        *,
        table: str | None = None,
        order: str = "",
        limit: int | None = None,
        columns: str = "*",
    ) -> list[sqlite3.Row]:
        table = table or self.table
        sql = f"SELECT {columns} FROM {table} WHERE organization_id = ?"
        if where:
            sql += f" AND ({where})"
        if order:
            sql += f" ORDER BY {order}"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        cur = self.conn.execute(sql, (self.organization_id, *params))
        return cur.fetchall()

    def _scalar(self, sql: str, params: Sequence[Any] = (), default: Any = 0) -> Any:
        row = self.conn.execute(sql, (self.organization_id, *params)).fetchone()
        if row is None or row[0] is None:
            return default
        return row[0]

    def delete(self, row_id: str) -> None:
        self.conn.execute(
            f"DELETE FROM {self.table} WHERE id = ? AND organization_id = ?",
            (row_id, self.organization_id),
        )


# --------------------------------------------------------------- organizations ---


class OrganizationRepository:
    """The tenant root. The only repository not bound to a single tenant, and
    the only one allowed to list organizations."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create(
        self,
        name: str,
        slug: str,
        *,
        default_locale: str = "es",
        retention_days: int = 365,
    ) -> str:
        organization_id = new_id("org")
        self.conn.execute(
            "INSERT INTO organizations (id, name, slug, default_locale, retention_days, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (organization_id, name, slug, default_locale, retention_days, utcnow()),
        )
        return organization_id

    def get(self, organization_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM organizations WHERE id = ?", (organization_id,)
        ).fetchone()

    def by_slug(self, slug: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM organizations WHERE slug = ?", (slug,)
        ).fetchone()

    def all(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM organizations ORDER BY name"
        ).fetchall()

    def set_stopped(self, organization_id: str, stopped: bool, reason: str = "") -> None:
        self.conn.execute(
            "UPDATE organizations SET stopped = ?, stopped_reason = ? WHERE id = ?",
            (1 if stopped else 0, reason, organization_id),
        )


# ----------------------------------------------------------------------- users ---


class UserRepository(Repository):
    table = "users"
    id_prefix = "usr"

    def create(self, email: str, display_name: str, role: str, locale: str | None = None) -> str:
        return self._insert(
            {
                "email": email,
                "display_name": display_name,
                "role": role,
                "locale": locale,
            }
        )

    def get(self, user_id: str) -> sqlite3.Row | None:
        return self._get(user_id)

    def by_email(self, email: str) -> sqlite3.Row | None:
        rows = self._select("email = ?", (email,), limit=1)
        return rows[0] if rows else None

    def list(self) -> list[sqlite3.Row]:
        return self._select(order="display_name")

    def set_locale(self, user_id: str, locale: str | None) -> None:
        self._update(user_id, {"locale": locale})


# ----------------------------------------------------------- areas / job roles ---


class AreaRepository(Repository):
    table = "areas"
    id_prefix = "area"

    def create(self, name: str) -> str:
        return self._insert({"name": name})

    def list(self) -> list[sqlite3.Row]:
        return self._select(order="name")


class JobRoleRepository(Repository):
    """`puestos`: groups skills and declares who approves what."""

    table = "job_roles"
    id_prefix = "job"

    def create(
        self,
        name: str,
        *,
        area_id: str | None = None,
        description: str = "",
        inputs: Iterable[str] = (),
        outputs: Iterable[str] = (),
        approver_user_id: str | None = None,
    ) -> str:
        return self._insert(
            {
                "name": name,
                "area_id": area_id,
                "description": description,
                "inputs_json": dumps(list(inputs)),
                "outputs_json": dumps(list(outputs)),
                "approver_user_id": approver_user_id,
            }
        )

    def get(self, job_role_id: str) -> sqlite3.Row | None:
        return self._get(job_role_id)

    def list(self) -> list[sqlite3.Row]:
        return self._select(order="name")

    def assign_skill(self, job_role_id: str, skill_id: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO job_role_skills (organization_id, job_role_id, skill_id) "
            "VALUES (?, ?, ?)",
            (self.organization_id, job_role_id, skill_id),
        )

    def skills_of(self, job_role_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT s.* FROM skills s "
            "JOIN job_role_skills j ON j.skill_id = s.id AND j.organization_id = s.organization_id "
            "WHERE s.organization_id = ? AND j.job_role_id = ? ORDER BY s.name",
            (self.organization_id, job_role_id),
        ).fetchall()


# ---------------------------------------------------------------- applications ---


class ApplicationRepository(Repository):
    table = "applications"
    id_prefix = "app"

    def create(self, name: str, *, detected_version: str | None = None, fingerprint: str | None = None) -> str:
        return self._insert(
            {"name": name, "detected_version": detected_version, "fingerprint": fingerprint}
        )

    def get(self, application_id: str) -> sqlite3.Row | None:
        return self._get(application_id)

    def list(self) -> list[sqlite3.Row]:
        return self._select(order="name")

    def set_health(self, application_id: str, health_status: str) -> None:
        self._update(application_id, {"health_status": health_status})

    def set_version(self, application_id: str, version: str) -> None:
        self._update(application_id, {"detected_version": version})


# ---------------------------------------------------------------------- skills ---


class SkillRepository(Repository):
    table = "skills"
    id_prefix = "skl"

    def create(self, **fields: Any) -> str:
        fields.setdefault("updated_at", utcnow())
        return self._insert(fields)

    def get(self, skill_id: str) -> sqlite3.Row | None:
        return self._get(skill_id)

    def require(self, skill_id: str) -> sqlite3.Row:
        return self._require(skill_id)

    def list(self, *, status: str | None = None, application_id: str | None = None) -> list[sqlite3.Row]:
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if application_id:
            clauses.append("application_id = ?")
            params.append(application_id)
        return self._select(" AND ".join(clauses), params, order="name")

    def update(self, skill_id: str, **fields: Any) -> None:
        fields["updated_at"] = utcnow()
        self._update(skill_id, fields)

    def set_status(self, skill_id: str, status: SkillStatus | str, reason: str = "") -> None:
        value = status.value if isinstance(status, SkillStatus) else status
        self.update(skill_id, status=value, status_reason=reason)

    def mark_fragile(self, skill_id: str, fragile: bool = True) -> None:
        self.update(skill_id, fragile=1 if fragile else 0)

    def set_stopped(self, skill_id: str, stopped: bool) -> None:
        self.update(skill_id, stopped=1 if stopped else 0)

    def flag_for_review(self, skill_id: str, reason: str) -> None:
        """Never let a skill fail silently: it moves to `needs_review` (rule 7)."""
        self.set_status(skill_id, SkillStatus.NEEDS_REVIEW, reason)

    def flag_application_for_review(self, application_id: str, reason: str) -> list[str]:
        """A target application changed version: every skill bound to it goes to
        `needs_review` and back into certification (§5)."""
        rows = self._select("application_id = ? AND status = 'published'", (application_id,))
        for row in rows:
            self.flag_for_review(row["id"], reason)
        return [row["id"] for row in rows]

    # -- steps ----------------------------------------------------------------

    def add_step(self, skill_id: str, **fields: Any) -> str:
        fields["skill_id"] = skill_id
        if "selectors" in fields:
            fields["selectors_json"] = dumps(fields.pop("selectors"))
        return self._insert(fields, table="skill_steps")

    def steps(self, skill_id: str) -> list[sqlite3.Row]:
        return self._select(
            "skill_id = ?", (skill_id,), table="skill_steps", order="position"
        )

    def replace_steps(self, skill_id: str, steps: Iterable[dict[str, Any]]) -> None:
        with transaction(self.conn):
            self.conn.execute(
                "DELETE FROM skill_steps WHERE organization_id = ? AND skill_id = ?",
                (self.organization_id, skill_id),
            )
            for position, step in enumerate(steps, start=1):
                payload = dict(step)
                payload.setdefault("position", position)
                self.add_step(skill_id, **payload)

    def screen_count(self, skill_id: str) -> int:
        """Distinct screens the steps touch. Certification compares this to the
        three-screen limit."""
        return int(
            self._scalar(
                "SELECT COUNT(DISTINCT screen_index) FROM skill_steps "
                "WHERE organization_id = ? AND skill_id = ?",
                (skill_id,),
            )
        )


# ------------------------------------------------------------------- processes ---


class ProcessRepository(Repository):
    table = "processes"
    id_prefix = "prc"

    def create(self, name: str, *, description: str = "", job_role_id: str | None = None) -> str:
        return self._insert({"name": name, "description": description, "job_role_id": job_role_id})

    def get(self, process_id: str) -> sqlite3.Row | None:
        return self._get(process_id)

    def list(self) -> list[sqlite3.Row]:
        return self._select(order="name")

    def set_stopped(self, process_id: str, stopped: bool) -> None:
        self._update(process_id, {"stopped": 1 if stopped else 0})

    def add_node(self, process_id: str, node_key: str, skill_id: str, *, position: int = 0, config: dict | None = None) -> str:
        return self._insert(
            {
                "process_id": process_id,
                "node_key": node_key,
                "skill_id": skill_id,
                "position": position,
                "config_json": dumps(config or {}),
            },
            table="process_nodes",
        )

    def add_edge(
        self,
        process_id: str,
        to_node_key: str,
        *,
        from_node_key: str | None = None,
        condition_expr: str | None = None,
        data_mapping: dict | None = None,
    ) -> str:
        return self._insert(
            {
                "process_id": process_id,
                "from_node_key": from_node_key,
                "to_node_key": to_node_key,
                "condition_expr": condition_expr,
                "data_mapping_json": dumps(data_mapping or {}),
            },
            table="process_edges",
        )

    def nodes(self, process_id: str) -> list[sqlite3.Row]:
        return self._select("process_id = ?", (process_id,), table="process_nodes", order="position, node_key")

    def edges(self, process_id: str) -> list[sqlite3.Row]:
        return self._select("process_id = ?", (process_id,), table="process_edges", order="id")


# ------------------------------------------------------------------ executions ---


class ExecutionRepository(Repository):
    table = "executions"
    id_prefix = "exe"

    def create(self, **fields: Any) -> str:
        return self._insert(fields)

    def get(self, execution_id: str) -> sqlite3.Row | None:
        return self._get(execution_id)

    def require(self, execution_id: str) -> sqlite3.Row:
        return self._require(execution_id)

    def update(self, execution_id: str, **fields: Any) -> None:
        self._update(execution_id, fields)

    def set_status(self, execution_id: str, status: ExecutionStatus | str, **fields: Any) -> None:
        fields["status"] = status.value if isinstance(status, ExecutionStatus) else status
        self._update(execution_id, fields)

    def list(
        self,
        *,
        statuses: Sequence[str] | None = None,
        process_id: str | None = None,
        skill_id: str | None = None,
        job_role_id: str | None = None,
        requested_by: str | None = None,
        effects_only: bool = False,
        limit: int = 100,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []
        if effects_only:
            # Certification fires ten dry runs per skill. They belong in the
            # skill's own history, not in an operations list.
            clauses.append("effects_enabled = 1")
        if statuses:
            clauses.append(f"status IN ({', '.join('?' * len(statuses))})")
            params.extend(statuses)
        if process_id:
            clauses.append("process_id = ?")
            params.append(process_id)
        if skill_id:
            clauses.append("skill_id = ?")
            params.append(skill_id)
        if requested_by:
            clauses.append("requested_by = ?")
            params.append(requested_by)
        if job_role_id:
            clauses.append(
                "process_id IN (SELECT id FROM processes "
                "WHERE organization_id = executions.organization_id AND job_role_id = ?)"
            )
            params.append(job_role_id)
        return self._select(
            " AND ".join(clauses), params, order="created_at DESC, id DESC", limit=limit
        )

    def queue_counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS total FROM executions "
            "WHERE organization_id = ? GROUP BY status",
            (self.organization_id,),
        ).fetchall()
        return {row["status"]: row["total"] for row in rows}

    def runs_in_last_hour(self, skill_id: str) -> int:
        """Production runs only.

        The hourly ceiling bounds blast radius on the target system, and a dry
        run touches nothing. Counting dry runs here would make a skill with a low
        ceiling impossible to certify, since certification runs it ten times.
        """
        return int(
            self._scalar(
                "SELECT COUNT(*) FROM executions WHERE organization_id = ? AND skill_id = ? "
                "AND effects_enabled = 1 AND created_at >= datetime('now', '-1 hour')",
                (skill_id,),
            )
        )

    def record_cost(
        self,
        execution_id: str,
        *,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
        cost_usd: float,
        measurement: Measurement,
    ) -> None:
        self._update(
            execution_id,
            {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_tokens": cached_tokens,
                "cost_usd": cost_usd,
                "cost_measurement": measurement.value,
            },
        )

    # -- steps ----------------------------------------------------------------

    def add_step(self, execution_id: str, **fields: Any) -> str:
        fields["execution_id"] = execution_id
        return self._insert(fields, table="step_executions")

    def steps(self, execution_id: str) -> list[sqlite3.Row]:
        return self._select(
            "execution_id = ?", (execution_id,), table="step_executions", order="position"
        )

    def children(self, execution_id: str) -> list[sqlite3.Row]:
        return self._select("parent_execution_id = ?", (execution_id,), order="created_at")

    def in_range(self, start: str, end: str) -> list[sqlite3.Row]:
        return self._select(
            "created_at >= ? AND created_at <= ?", (start, end), order="created_at"
        )


# --------------------------------------------------------------- interventions ---


class InterventionRepository(Repository):
    table = "interventions"
    id_prefix = "itv"

    def create(
        self,
        *,
        reason: str,
        skill_id: str | None = None,
        execution_id: str | None = None,
        detail: str = "",
        actor_user_id: str | None = None,
        action_taken: str = "",
        marked_for_review: bool = False,
    ) -> str:
        return self._insert(
            {
                "reason": reason,
                "skill_id": skill_id,
                "execution_id": execution_id,
                "detail": detail,
                "actor_user_id": actor_user_id,
                "action_taken": action_taken,
                "marked_for_review": 1 if marked_for_review else 0,
            }
        )

    def list(self, *, skill_id: str | None = None, limit: int = 100) -> list[sqlite3.Row]:
        where, params = ("skill_id = ?", (skill_id,)) if skill_id else ("", ())
        return self._select(where, params, order="created_at DESC", limit=limit)

    def count_for_skill(self, skill_id: str) -> int:
        return int(
            self._scalar(
                "SELECT COUNT(*) FROM interventions WHERE organization_id = ? AND skill_id = ?",
                (skill_id,),
            )
        )


# ------------------------------------------------------------------- approvals ---


class ApprovalRepository(Repository):
    table = "approvals"
    id_prefix = "apr"

    def request(
        self, *, execution_id: str, skill_id: str, proposed_action: str, evidence: dict | None = None
    ) -> str:
        return self._insert(
            {
                "execution_id": execution_id,
                "skill_id": skill_id,
                "proposed_action": proposed_action,
                "evidence_json": dumps(evidence or {}),
                "requested_at": utcnow(),
            }
        )

    def get(self, approval_id: str) -> sqlite3.Row | None:
        return self._get(approval_id)

    def pending(self) -> list[sqlite3.Row]:
        return self._select("status = 'pending'", order="requested_at")

    def for_execution(self, execution_id: str) -> list[sqlite3.Row]:
        return self._select("execution_id = ?", (execution_id,), order="requested_at")

    def decide(self, approval_id: str, *, approved: bool, decided_by: str, comment: str = "") -> None:
        status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
        self._update(
            approval_id,
            {
                "status": status.value,
                "decided_at": utcnow(),
                "decided_by": decided_by,
                "comment": comment,
            },
        )

    def has_approval(self, execution_id: str) -> bool:
        return bool(
            self._scalar(
                "SELECT COUNT(*) FROM approvals WHERE organization_id = ? "
                "AND execution_id = ? AND status = 'approved'",
                (execution_id,),
            )
        )


# ------------------------------------------------ demonstrations and proposals ---


class DemonstrationRepository(Repository):
    table = "demonstrations"
    id_prefix = "dem"

    def create(self, *, payload: dict, title: str = "", application_id: str | None = None,
               skill_id: str | None = None, recorded_by: str | None = None) -> str:
        return self._insert(
            {
                "payload_json": dumps(payload),
                "title": title,
                "application_id": application_id,
                "skill_id": skill_id,
                "recorded_by": recorded_by,
            }
        )

    def get(self, demonstration_id: str) -> sqlite3.Row | None:
        return self._get(demonstration_id)

    def list(self) -> list[sqlite3.Row]:
        return self._select(order="created_at DESC")


class ProposalRepository(Repository):
    table = "skill_proposals"
    id_prefix = "prp"

    def create(self, *, demonstration_id: str, proposal: dict, notes: list | None = None) -> str:
        return self._insert(
            {
                "demonstration_id": demonstration_id,
                "proposal_json": dumps(proposal),
                "notes_json": dumps(notes or []),
            }
        )

    def get(self, proposal_id: str) -> sqlite3.Row | None:
        return self._get(proposal_id)

    def list(self) -> list[sqlite3.Row]:
        return self._select(order="created_at DESC")

    def accept(self, proposal_id: str, *, reviewed_by: str, accepted: dict) -> None:
        self._update(
            proposal_id,
            {
                "reviewed_by": reviewed_by,
                "reviewed_at": utcnow(),
                "accepted_json": dumps(accepted),
            },
        )


# -------------------------------------------------------------- certifications ---


class CertificationRepository(Repository):
    table = "certifications"
    id_prefix = "crt"

    def create(self, **fields: Any) -> str:
        return self._insert(fields)

    def latest(self, skill_id: str) -> sqlite3.Row | None:
        rows = self._select("skill_id = ?", (skill_id,), order="created_at DESC", limit=1)
        return rows[0] if rows else None

    def history(self, skill_id: str) -> list[sqlite3.Row]:
        return self._select("skill_id = ?", (skill_id,), order="created_at DESC")


# ------------------------------------------------------ degradations and drift ---


class DegradationRepository(Repository):
    table = "selector_degradations"
    id_prefix = "deg"

    def record(self, *, skill_id: str, from_tier: int, to_tier: int,
               skill_step_id: str | None = None, execution_id: str | None = None) -> str:
        return self._insert(
            {
                "skill_id": skill_id,
                "skill_step_id": skill_step_id,
                "execution_id": execution_id,
                "from_tier": from_tier,
                "to_tier": to_tier,
            }
        )

    def count_last_week(self, skill_id: str) -> int:
        """Three degradations in a week mark the skill for review (§5)."""
        return int(
            self._scalar(
                "SELECT COUNT(*) FROM selector_degradations WHERE organization_id = ? "
                "AND skill_id = ? AND created_at >= datetime('now', '-7 days')",
                (skill_id,),
            )
        )

    def list(self, skill_id: str, limit: int = 50) -> list[sqlite3.Row]:
        return self._select("skill_id = ?", (skill_id,), order="created_at DESC", limit=limit)


class DriftRepository(Repository):
    table = "drift_detections"
    id_prefix = "drf"

    def record(self, *, skill_id: str, distance: float, threshold: float,
               execution_id: str | None = None, observed: str = "", baseline: str = "") -> str:
        return self._insert(
            {
                "skill_id": skill_id,
                "execution_id": execution_id,
                "distance": distance,
                "threshold": threshold,
                "observed_fingerprint": observed,
                "baseline_fingerprint": baseline,
            }
        )

    def count_for_skill(self, skill_id: str) -> int:
        return int(
            self._scalar(
                "SELECT COUNT(*) FROM drift_detections WHERE organization_id = ? AND skill_id = ?",
                (skill_id,),
            )
        )


# ------------------------------------------------------------------- audit log ---


class AuditRepository(Repository):
    table = "audit_log"
    id_prefix = "aud"

    def record(self, action: str, entity: str, entity_id: str | None = None, **payload: Any) -> str:
        return self._insert(
            {
                "actor_user_id": self.tenant.user_id,
                "action": action,
                "entity": entity,
                "entity_id": entity_id,
                "payload_json": dumps(payload),
            }
        )

    def list(self, limit: int = 200) -> list[sqlite3.Row]:
        return self._select(order="created_at DESC, id DESC", limit=limit)


class RepositorySet:
    """One handle for all repositories bound to the same tenant and connection."""

    def __init__(self, conn: sqlite3.Connection, tenant: TenantContext):
        self.conn = conn
        self.tenant = tenant
        self.organizations = OrganizationRepository(conn)
        self.users = UserRepository(conn, tenant)
        self.areas = AreaRepository(conn, tenant)
        self.job_roles = JobRoleRepository(conn, tenant)
        self.applications = ApplicationRepository(conn, tenant)
        self.skills = SkillRepository(conn, tenant)
        self.processes = ProcessRepository(conn, tenant)
        self.executions = ExecutionRepository(conn, tenant)
        self.interventions = InterventionRepository(conn, tenant)
        self.approvals = ApprovalRepository(conn, tenant)
        self.demonstrations = DemonstrationRepository(conn, tenant)
        self.proposals = ProposalRepository(conn, tenant)
        self.certifications = CertificationRepository(conn, tenant)
        self.degradations = DegradationRepository(conn, tenant)
        self.drift = DriftRepository(conn, tenant)
        self.audit = AuditRepository(conn, tenant)
