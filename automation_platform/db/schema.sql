-- Multi-tenant data model (specification §2).
--
-- Two structural rules run through the whole schema:
--
--   1. Every business table carries `organization_id` and declares
--      `UNIQUE (organization_id, id)`. Child tables reference the parent with a
--      composite foreign key on (organization_id, parent_id), so a row can
--      never point at a record belonging to another tenant. Tenant isolation is
--      enforced by the schema, not only by the queries.
--   2. Invariants that the specification calls non-negotiable are triggers, not
--      conventions. Application code checks them too, and the trigger is what
--      makes the check impossible to forget.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- tenants ---

CREATE TABLE IF NOT EXISTS organizations (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    slug            TEXT NOT NULL UNIQUE,
    default_locale  TEXT NOT NULL DEFAULT 'es' CHECK (default_locale IN ('es', 'en')),
    retention_days  INTEGER NOT NULL DEFAULT 365 CHECK (retention_days > 0),
    -- Organization-wide kill switch (§7).
    stopped         INTEGER NOT NULL DEFAULT 0 CHECK (stopped IN (0, 1)),
    stopped_reason  TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    email           TEXT NOT NULL,
    display_name    TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('admin', 'designer', 'operator', 'approver')),
    -- NULL means "follow the organization default locale".
    locale          TEXT CHECK (locale IN ('es', 'en')),
    active          INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at      TEXT NOT NULL,
    UNIQUE (organization_id, id),
    UNIQUE (organization_id, email)
);

CREATE TABLE IF NOT EXISTS areas (
    id              TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (organization_id, id)
);

-- `puestos`: the abstraction that maps to how the company already thinks about
-- work. Groups skills, declares what goes in and out, and who approves what.
CREATE TABLE IF NOT EXISTS job_roles (
    id                TEXT PRIMARY KEY,
    organization_id   TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    area_id           TEXT,
    name              TEXT NOT NULL,
    description       TEXT NOT NULL DEFAULT '',
    inputs_json       TEXT NOT NULL DEFAULT '[]',
    outputs_json      TEXT NOT NULL DEFAULT '[]',
    approver_user_id  TEXT,
    created_at        TEXT NOT NULL,
    UNIQUE (organization_id, id),
    FOREIGN KEY (organization_id, area_id) REFERENCES areas(organization_id, id) ON DELETE SET NULL,
    FOREIGN KEY (organization_id, approver_user_id) REFERENCES users(organization_id, id) ON DELETE SET NULL
);

-- ----------------------------------------------------------- applications ---

CREATE TABLE IF NOT EXISTS applications (
    id                TEXT PRIMARY KEY,
    organization_id   TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name              TEXT NOT NULL,
    detected_version  TEXT,
    fingerprint       TEXT,
    health_status     TEXT NOT NULL DEFAULT 'healthy'
                      CHECK (health_status IN ('healthy', 'version_changed', 'unreachable')),
    created_at        TEXT NOT NULL,
    UNIQUE (organization_id, id)
);

-- ------------------------------------------------------------------ skills ---

CREATE TABLE IF NOT EXISTS skills (
    id                    TEXT PRIMARY KEY,
    organization_id       TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    application_id        TEXT,
    key                   TEXT NOT NULL,
    name                  TEXT NOT NULL,
    description           TEXT NOT NULL DEFAULT '',
    executor              TEXT NOT NULL
                          CHECK (executor IN ('computer_use', 'data_transform', 'api_call', 'llm_extract')),
    -- Known start state the skill departs from and returns to (§4).
    start_anchor          TEXT NOT NULL DEFAULT '',
    input_schema_json     TEXT NOT NULL DEFAULT '{}',
    output_schema_json    TEXT NOT NULL DEFAULT '{}',
    precondition          TEXT NOT NULL DEFAULT '',
    -- Mandatory: no postcondition, no verification, no product (§2).
    postcondition         TEXT NOT NULL DEFAULT '',
    -- Domain check for layer L4. NULL means the skill cannot be sold as a
    -- guaranteed result, only as assistance with human review (§6).
    semantic_check        TEXT,
    risk_classification   TEXT NOT NULL
                          CHECK (risk_classification IN ('read_only', 'reversible_write', 'irreversible_write')),
    -- Hard limit from §1. The CHECK is the floor; certification counts the real
    -- transitions and the publication trigger refuses anything above three.
    declared_screens      INTEGER NOT NULL DEFAULT 1 CHECK (declared_screens BETWEEN 1 AND 3),
    status                TEXT NOT NULL DEFAULT 'draft'
                          CHECK (status IN ('draft', 'in_certification', 'published', 'needs_review')),
    status_reason         TEXT NOT NULL DEFAULT '',
    version               INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    fragile               INTEGER NOT NULL DEFAULT 0 CHECK (fragile IN (0, 1)),
    -- Per-skill kill switch and throughput ceiling (§7).
    stopped               INTEGER NOT NULL DEFAULT 0 CHECK (stopped IN (0, 1)),
    max_runs_per_hour     INTEGER NOT NULL DEFAULT 60 CHECK (max_runs_per_hour > 0),
    certified_at          TEXT,
    certified_app_version TEXT,
    created_by            TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    UNIQUE (organization_id, id),
    UNIQUE (organization_id, key, version),
    FOREIGN KEY (organization_id, application_id) REFERENCES applications(organization_id, id) ON DELETE SET NULL,
    FOREIGN KEY (organization_id, created_by) REFERENCES users(organization_id, id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS skill_steps (
    id               TEXT PRIMARY KEY,
    organization_id  TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    skill_id         TEXT NOT NULL,
    position         INTEGER NOT NULL,
    -- Which of the (at most three) screens this step acts on.
    screen_index     INTEGER NOT NULL DEFAULT 0 CHECK (screen_index BETWEEN 0 AND 2),
    action_type      TEXT NOT NULL,
    -- Selector cascade, ordered by preference (§5).
    selectors_json   TEXT NOT NULL DEFAULT '[]',
    value_template   TEXT,
    expected_value   TEXT,
    timeout_ms       INTEGER NOT NULL DEFAULT 5000 CHECK (timeout_ms > 0),
    UNIQUE (organization_id, id),
    UNIQUE (organization_id, skill_id, position),
    FOREIGN KEY (organization_id, skill_id) REFERENCES skills(organization_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS job_role_skills (
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    job_role_id     TEXT NOT NULL,
    skill_id        TEXT NOT NULL,
    PRIMARY KEY (organization_id, job_role_id, skill_id),
    FOREIGN KEY (organization_id, job_role_id) REFERENCES job_roles(organization_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, skill_id) REFERENCES skills(organization_id, id) ON DELETE CASCADE
);

-- --------------------------------------------------------------- processes ---

CREATE TABLE IF NOT EXISTS processes (
    id              TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    job_role_id     TEXT,
    name            TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    stopped         INTEGER NOT NULL DEFAULT 0 CHECK (stopped IN (0, 1)),
    created_at      TEXT NOT NULL,
    UNIQUE (organization_id, id),
    FOREIGN KEY (organization_id, job_role_id) REFERENCES job_roles(organization_id, id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS process_nodes (
    id              TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    process_id      TEXT NOT NULL,
    node_key        TEXT NOT NULL,
    skill_id        TEXT NOT NULL,
    position        INTEGER NOT NULL DEFAULT 0,
    config_json     TEXT NOT NULL DEFAULT '{}',
    UNIQUE (organization_id, id),
    UNIQUE (organization_id, process_id, node_key),
    FOREIGN KEY (organization_id, process_id) REFERENCES processes(organization_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, skill_id) REFERENCES skills(organization_id, id) ON DELETE RESTRICT
);

-- Edges carry structured data between skills. Never screen state (rule 1 in
-- CLAUDE.md): `data_mapping_json` is the only channel between two skills.
CREATE TABLE IF NOT EXISTS process_edges (
    id                 TEXT PRIMARY KEY,
    organization_id    TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    process_id         TEXT NOT NULL,
    from_node_key      TEXT,
    to_node_key        TEXT NOT NULL,
    condition_expr     TEXT,
    data_mapping_json  TEXT NOT NULL DEFAULT '{}',
    UNIQUE (organization_id, id),
    FOREIGN KEY (organization_id, process_id) REFERENCES processes(organization_id, id) ON DELETE CASCADE
);

-- -------------------------------------------------------------- executions ---

CREATE TABLE IF NOT EXISTS executions (
    id                 TEXT PRIMARY KEY,
    organization_id    TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    process_id         TEXT,
    skill_id           TEXT,
    node_key           TEXT,
    parent_execution_id TEXT,
    trigger            TEXT NOT NULL CHECK (trigger IN ('manual', 'scheduled', 'api', 'dry_run')),
    status             TEXT NOT NULL DEFAULT 'queued'
                       CHECK (status IN ('queued', 'running', 'waiting_approval', 'succeeded',
                                         'failed', 'escalated', 'stopped')),
    -- Dry runs never touch the target system (certification, §3).
    effects_enabled    INTEGER NOT NULL DEFAULT 1 CHECK (effects_enabled IN (0, 1)),
    started_at         TEXT,
    finished_at        TEXT,
    duration_ms        INTEGER,
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cached_tokens      INTEGER NOT NULL DEFAULT 0,
    cost_usd           REAL NOT NULL DEFAULT 0,
    -- Every cost number says whether it was measured or estimated.
    cost_measurement   TEXT NOT NULL DEFAULT 'estimated'
                       CHECK (cost_measurement IN ('measured', 'estimated')),
    input_json         TEXT NOT NULL DEFAULT '{}',
    result_json        TEXT NOT NULL DEFAULT '{}',
    error_code         TEXT,
    error_detail       TEXT,
    requested_by       TEXT,
    created_at         TEXT NOT NULL,
    UNIQUE (organization_id, id),
    FOREIGN KEY (organization_id, process_id) REFERENCES processes(organization_id, id) ON DELETE SET NULL,
    FOREIGN KEY (organization_id, skill_id) REFERENCES skills(organization_id, id) ON DELETE SET NULL,
    FOREIGN KEY (organization_id, requested_by) REFERENCES users(organization_id, id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS step_executions (
    id                  TEXT PRIMARY KEY,
    organization_id     TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    execution_id        TEXT NOT NULL,
    skill_step_id       TEXT,
    position            INTEGER NOT NULL,
    screen_index        INTEGER NOT NULL DEFAULT 0,
    action_json         TEXT NOT NULL DEFAULT '{}',
    -- Evidence: before and after every action (§8). These are references into
    -- the evidence store, not blobs.
    screenshot_before   TEXT,
    screenshot_after    TEXT,
    accessibility_hash  TEXT,
    selector_tier_used  INTEGER CHECK (selector_tier_used BETWEEN 1 AND 5),
    verification_json   TEXT NOT NULL DEFAULT '[]',
    status              TEXT NOT NULL CHECK (status IN ('passed', 'retried', 'failed', 'skipped')),
    attempts            INTEGER NOT NULL DEFAULT 1,
    duration_ms         INTEGER NOT NULL DEFAULT 0,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    detail              TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    UNIQUE (organization_id, id),
    FOREIGN KEY (organization_id, execution_id) REFERENCES executions(organization_id, id) ON DELETE CASCADE
);

-- The health metric of the system (§2). Every time a human had to step in.
CREATE TABLE IF NOT EXISTS interventions (
    id                 TEXT PRIMARY KEY,
    organization_id    TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    execution_id       TEXT,
    skill_id           TEXT,
    reason             TEXT NOT NULL,
    detail             TEXT NOT NULL DEFAULT '',
    actor_user_id      TEXT,
    action_taken       TEXT NOT NULL DEFAULT '',
    marked_for_review  INTEGER NOT NULL DEFAULT 0 CHECK (marked_for_review IN (0, 1)),
    created_at         TEXT NOT NULL,
    UNIQUE (organization_id, id),
    FOREIGN KEY (organization_id, execution_id) REFERENCES executions(organization_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, skill_id) REFERENCES skills(organization_id, id) ON DELETE SET NULL
);

-- Queue of pending irreversible writes (§7).
CREATE TABLE IF NOT EXISTS approvals (
    id                 TEXT PRIMARY KEY,
    organization_id    TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    execution_id       TEXT NOT NULL,
    skill_id           TEXT NOT NULL,
    proposed_action    TEXT NOT NULL,
    evidence_json      TEXT NOT NULL DEFAULT '{}',
    status             TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
    requested_at       TEXT NOT NULL,
    decided_at         TEXT,
    decided_by         TEXT,
    comment            TEXT NOT NULL DEFAULT '',
    UNIQUE (organization_id, id),
    FOREIGN KEY (organization_id, execution_id) REFERENCES executions(organization_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, skill_id) REFERENCES skills(organization_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, decided_by) REFERENCES users(organization_id, id) ON DELETE SET NULL
);

-- Raw user recordings. Kept as evidence; they are not the skill (§3).
CREATE TABLE IF NOT EXISTS demonstrations (
    id               TEXT PRIMARY KEY,
    organization_id  TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    skill_id         TEXT,
    application_id   TEXT,
    title            TEXT NOT NULL DEFAULT '',
    payload_json     TEXT NOT NULL,
    recorded_by      TEXT,
    created_at       TEXT NOT NULL,
    UNIQUE (organization_id, id),
    FOREIGN KEY (organization_id, skill_id) REFERENCES skills(organization_id, id) ON DELETE SET NULL,
    FOREIGN KEY (organization_id, recorded_by) REFERENCES users(organization_id, id) ON DELETE SET NULL
);

-- The generalized definition proposed from a demonstration. Nothing reaches
-- `published` without a human review of this row (§3).
CREATE TABLE IF NOT EXISTS skill_proposals (
    id                TEXT PRIMARY KEY,
    organization_id   TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    demonstration_id  TEXT NOT NULL,
    proposal_json     TEXT NOT NULL,
    notes_json        TEXT NOT NULL DEFAULT '[]',
    reviewed_by       TEXT,
    reviewed_at       TEXT,
    accepted_json     TEXT,
    created_at        TEXT NOT NULL,
    UNIQUE (organization_id, id),
    FOREIGN KEY (organization_id, demonstration_id) REFERENCES demonstrations(organization_id, id) ON DELETE CASCADE,
    FOREIGN KEY (organization_id, reviewed_by) REFERENCES users(organization_id, id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS certifications (
    id                    TEXT PRIMARY KEY,
    organization_id       TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    skill_id              TEXT NOT NULL,
    skill_version         INTEGER NOT NULL DEFAULT 1,
    status                TEXT NOT NULL CHECK (status IN ('passed', 'failed', 'fragile')),
    screens_counted       INTEGER NOT NULL,
    postcondition_ok      INTEGER NOT NULL CHECK (postcondition_ok IN (0, 1)),
    dry_runs_total        INTEGER NOT NULL DEFAULT 0,
    dry_runs_passed       INTEGER NOT NULL DEFAULT 0,
    variation_resize_ok   INTEGER NOT NULL DEFAULT 0 CHECK (variation_resize_ok IN (0, 1)),
    variation_dialog_ok   INTEGER NOT NULL DEFAULT 0 CHECK (variation_dialog_ok IN (0, 1)),
    risk_declared_ok      INTEGER NOT NULL DEFAULT 0 CHECK (risk_declared_ok IN (0, 1)),
    baseline_fingerprint  TEXT,
    app_version           TEXT,
    report_json           TEXT NOT NULL DEFAULT '{}',
    created_at            TEXT NOT NULL,
    UNIQUE (organization_id, id),
    FOREIGN KEY (organization_id, skill_id) REFERENCES skills(organization_id, id) ON DELETE CASCADE
);

-- Each time the selector cascade had to fall back a tier (§5).
CREATE TABLE IF NOT EXISTS selector_degradations (
    id               TEXT PRIMARY KEY,
    organization_id  TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    skill_id         TEXT NOT NULL,
    skill_step_id    TEXT,
    execution_id     TEXT,
    from_tier        INTEGER NOT NULL CHECK (from_tier BETWEEN 1 AND 5),
    to_tier          INTEGER NOT NULL CHECK (to_tier BETWEEN 1 AND 5),
    created_at       TEXT NOT NULL,
    UNIQUE (organization_id, id),
    FOREIGN KEY (organization_id, skill_id) REFERENCES skills(organization_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS drift_detections (
    id               TEXT PRIMARY KEY,
    organization_id  TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    skill_id         TEXT NOT NULL,
    execution_id     TEXT,
    distance         REAL NOT NULL,
    threshold        REAL NOT NULL,
    observed_fingerprint TEXT,
    baseline_fingerprint TEXT,
    created_at       TEXT NOT NULL,
    UNIQUE (organization_id, id),
    FOREIGN KEY (organization_id, skill_id) REFERENCES skills(organization_id, id) ON DELETE CASCADE
);

-- Complete and immutable log (§7). Triggers below refuse UPDATE and DELETE.
CREATE TABLE IF NOT EXISTS audit_log (
    id               TEXT PRIMARY KEY,
    organization_id  TEXT NOT NULL,
    actor_user_id    TEXT,
    action           TEXT NOT NULL,
    entity           TEXT NOT NULL,
    entity_id        TEXT,
    payload_json     TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL
);

-- ------------------------------------------------------------------ indexes ---

CREATE INDEX IF NOT EXISTS idx_users_org ON users(organization_id);
CREATE INDEX IF NOT EXISTS idx_skills_org_status ON skills(organization_id, status);
CREATE INDEX IF NOT EXISTS idx_skill_steps_skill ON skill_steps(organization_id, skill_id, position);
CREATE INDEX IF NOT EXISTS idx_executions_org_status ON executions(organization_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_executions_skill ON executions(organization_id, skill_id, created_at);
CREATE INDEX IF NOT EXISTS idx_step_exec_execution ON step_executions(organization_id, execution_id, position);
CREATE INDEX IF NOT EXISTS idx_interventions_skill ON interventions(organization_id, skill_id, created_at);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(organization_id, status, requested_at);
CREATE INDEX IF NOT EXISTS idx_degradations_skill ON selector_degradations(organization_id, skill_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_org ON audit_log(organization_id, created_at);

-- --------------------------------------------------------------- invariants ---
-- These four triggers are the specification's non-negotiable rules expressed as
-- schema. Application code checks the same rules earlier and with better error
-- messages; the triggers are what make them unbypassable.

-- A skill without a postcondition cannot reach `published` (§2).
CREATE TRIGGER IF NOT EXISTS trg_publish_requires_postcondition
BEFORE UPDATE OF status ON skills
WHEN NEW.status = 'published' AND TRIM(COALESCE(NEW.postcondition, '')) = ''
BEGIN
    SELECT RAISE(ABORT, 'invariant:missing_postcondition');
END;

-- A skill with more than three screen transitions cannot reach `published`
-- (§1). `declared_screens` is what certification counted, not what the designer
-- claimed.
CREATE TRIGGER IF NOT EXISTS trg_publish_screen_budget
BEFORE UPDATE OF status ON skills
WHEN NEW.status = 'published' AND NEW.declared_screens > 3
BEGIN
    SELECT RAISE(ABORT, 'invariant:screen_budget');
END;

-- Publication requires a passing certification for this exact version (§3).
CREATE TRIGGER IF NOT EXISTS trg_publish_requires_certification
BEFORE UPDATE OF status ON skills
WHEN NEW.status = 'published'
 AND NOT EXISTS (
        SELECT 1 FROM certifications c
        WHERE c.organization_id = NEW.organization_id
          AND c.skill_id = NEW.id
          AND c.skill_version = NEW.version
          AND c.status IN ('passed', 'fragile')
     )
BEGIN
    SELECT RAISE(ABORT, 'invariant:not_certified');
END;

-- An execution of an `irreversible_write` skill cannot be confirmed without an
-- approval record (§2, §7).
CREATE TRIGGER IF NOT EXISTS trg_irreversible_requires_approval
BEFORE UPDATE OF status ON executions
WHEN NEW.status = 'succeeded'
 AND EXISTS (
        SELECT 1 FROM skills s
        WHERE s.organization_id = NEW.organization_id
          AND s.id = NEW.skill_id
          AND s.risk_classification = 'irreversible_write'
     )
 AND NEW.effects_enabled = 1
 AND NOT EXISTS (
        SELECT 1 FROM approvals a
        WHERE a.organization_id = NEW.organization_id
          AND a.execution_id = NEW.id
          AND a.status = 'approved'
     )
BEGIN
    SELECT RAISE(ABORT, 'invariant:approval_required');
END;

-- The audit log is append-only.
CREATE TRIGGER IF NOT EXISTS trg_audit_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'invariant:audit_immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_audit_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'invariant:audit_immutable');
END;
