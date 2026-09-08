"""Views.

Three views carry the product, and they map to the three questions an operator
asks (§8): what is happening now (queue), what happened in this run (execution),
and what is degrading (health).

No view builds a user-visible string. Every label goes through `t(...)`, so the
same page serves Spanish and English without a second template.
"""

from __future__ import annotations

from datetime import datetime, timezone

from flask import (
    Blueprint,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from .. import health as health_module
from ..certification import Certifier
from ..db import loads
from ..domain.enums import (
    ApprovalStatus,
    ExecutionStatus,
    RiskClass,
    Role,
    SelectorTier,
    SkillStatus,
    Trigger,
)
from ..domain.errors import PlatformError
from ..engine.cost import estimate_skill_run
from ..engine.runner import Runner
from ..export import EvidenceRange, EvidenceReport
from ..i18n import LOCALE_NAMES, SUPPORTED_LOCALES
from ..security import authz
from ..simulation import make_driver_factory
from ..tenancy import TenantContext
from ..training import create_skill_from_proposal, generalize, split_plan
from ..training.recorder import replay_into_recorder
from .context import (
    SESSION_ORGANIZATION,
    SESSION_USER,
    require_session,
    switch_locale,
)

auth = Blueprint("auth", __name__)
main = Blueprint("main", __name__)


# ------------------------------------------------------------------- auth ---


@auth.route("/login", methods=["GET", "POST"])
def login():
    from ..db import RepositorySet

    connection = g.connection
    bootstrap = RepositorySet(connection, TenantContext("bootstrap"))
    organizations = bootstrap.organizations.all()

    selected = request.values.get("organization_id") or (
        organizations[0]["id"] if organizations else None
    )
    users = []
    if selected:
        tenant = TenantContext(selected, role=Role.ADMIN)
        users = RepositorySet(connection, tenant).users.list()

    if request.method == "POST" and request.form.get("user_id"):
        session[SESSION_ORGANIZATION] = selected
        session[SESSION_USER] = request.form["user_id"]
        session.pop("locale", None)
        return redirect(request.args.get("next") or url_for("main.dashboard"))

    return render_template(
        "login.jinja2", organizations=organizations, users=users, selected=selected
    )


@auth.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@auth.route("/language/<locale>")
def language(locale: str):
    switch_locale(locale)
    flash("flash.language_changed", "info")
    return redirect(request.referrer or url_for("main.dashboard"))


# -------------------------------------------------------------- dashboard ---


@main.before_request
def _guard():
    return require_session()


@main.route("/")
def dashboard():
    repos = g.repos
    summary = health_module.organization_summary(repos)
    recent = repos.executions.list(effects_only=True, limit=8)
    review = repos.skills.list(status=SkillStatus.NEEDS_REVIEW.value)
    approvals = repos.approvals.pending()
    return render_template(
        "dashboard.jinja2",
        summary=summary,
        recent=recent,
        review=review,
        approvals=approvals,
        alerts=health_module.application_alerts(repos),
        estimate=estimate_skill_run(current_app.config["PLATFORM"].prices),
        skill_names=_skill_names(repos),
    )


# ------------------------------------------------------------------ queue ---


@main.route("/queue")
def queue():
    repos = g.repos
    authz.require(g.tenant, "execution.view")
    statuses = request.args.getlist("status") or None
    executions = repos.executions.list(
        statuses=statuses,
        process_id=request.args.get("process_id") or None,
        job_role_id=request.args.get("job_role_id") or None,
        requested_by=request.args.get("user_id") or None,
        limit=200,
    )
    return render_template(
        "queue.jinja2",
        executions=executions,
        processes=repos.processes.list(),
        job_roles=repos.job_roles.list(),
        users=repos.users.list(),
        skill_names=_skill_names(repos),
        statuses=statuses or [],
        all_statuses=[status for status in ExecutionStatus],
    )


# ----------------------------------------------------------------- skills ---


@main.route("/skills")
def skills():
    repos = g.repos
    authz.require(g.tenant, "skill.view")
    rows = repos.skills.list(status=request.args.get("status") or None)
    return render_template(
        "skills.jinja2",
        skills=rows,
        applications={row["id"]: row for row in repos.applications.list()},
        screen_counts={row["id"]: repos.skills.screen_count(row["id"]) for row in rows},
        statuses=[status for status in SkillStatus],
    )


@main.route("/skills/<skill_id>")
def skill_detail(skill_id: str):
    repos = g.repos
    authz.require(g.tenant, "skill.view")
    skill = repos.skills.require(skill_id)
    certification = repos.certifications.latest(skill_id)
    return render_template(
        "skill_detail.jinja2",
        skill=skill,
        steps=repos.skills.steps(skill_id),
        certification=certification,
        certification_report=loads(certification["report_json"], {}) if certification else {},
        history=repos.certifications.history(skill_id),
        application=repos.applications.get(skill["application_id"]) if skill["application_id"] else None,
        executions=repos.executions.list(skill_id=skill_id, limit=10),
        health=health_module.skill_health(repos, skill),
        degradations=repos.degradations.list(skill_id, limit=10),
        screen_count=repos.skills.screen_count(skill_id),
        risk=RiskClass(skill["risk_classification"]),
        tiers=[tier for tier in SelectorTier],
    )


@main.route("/skills/<skill_id>/certify", methods=["POST"])
def certify(skill_id: str):
    repos = g.repos
    config = current_app.config["PLATFORM"]
    certifier = Certifier(
        repos,
        driver_factory=make_driver_factory(repos),
        dry_runs=config.dry_runs,
        success_threshold=config.dry_run_threshold,
    )
    cases = _demo_cases(repos, skill_id)
    report = certifier.certify(
        skill_id,
        risk_declared=request.form.get("risk_declared") == "on",
        cases=cases,
    )
    if report.publishable and request.form.get("publish") == "on":
        certifier.publish(skill_id)
        flash("flash.published", "success")
    else:
        flash("flash.certified", "info")
    return redirect(url_for("main.skill_detail", skill_id=skill_id))


@main.route("/skills/<skill_id>/publish", methods=["POST"])
def publish(skill_id: str):
    repos = g.repos
    certifier = Certifier(repos, driver_factory=make_driver_factory(repos))
    certifier.publish(skill_id)
    flash("flash.published", "success")
    return redirect(url_for("main.skill_detail", skill_id=skill_id))


@main.route("/skills/<skill_id>/stop", methods=["POST"])
def stop_skill(skill_id: str):
    authz.require(g.tenant, "skill.stop")
    stopped = request.form.get("stopped") == "on"
    g.repos.skills.set_stopped(skill_id, stopped)
    g.repos.audit.record("skill.stop", "skills", skill_id, stopped=stopped)
    flash("flash.stopped" if stopped else "flash.resumed", "info")
    return redirect(url_for("main.skill_detail", skill_id=skill_id))


@main.route("/skills/<skill_id>/run", methods=["POST"])
def run_skill(skill_id: str):
    repos = g.repos
    runner = Runner(repos, driver_factory=make_driver_factory(repos),
                    prices=current_app.config["PLATFORM"].prices)
    result = runner.run_skill(skill_id, inputs=_demo_cases(repos, skill_id)[0])
    flash("error.approval_required" if result.waiting_approval else "flash.execution_queued", "info")
    return redirect(url_for("main.execution_detail", execution_id=result.execution_id))


# -------------------------------------------------------------- processes ---


@main.route("/processes")
def processes():
    repos = g.repos
    authz.require(g.tenant, "process.view")
    rows = repos.processes.list()
    return render_template(
        "processes.jinja2",
        processes=rows,
        nodes={row["id"]: repos.processes.nodes(row["id"]) for row in rows},
        edges={row["id"]: repos.processes.edges(row["id"]) for row in rows},
        skill_names=_skill_names(repos),
        job_roles={row["id"]: row for row in repos.job_roles.list()},
    )


@main.route("/processes/<process_id>/run", methods=["POST"])
def run_process(process_id: str):
    repos = g.repos
    runner = Runner(repos, driver_factory=make_driver_factory(repos),
                    prices=current_app.config["PLATFORM"].prices)
    result = runner.run_process(process_id, inputs={"periodo": _current_period()})
    flash("flash.execution_queued", "info")
    return redirect(url_for("main.execution_detail", execution_id=result.execution_id))


@main.route("/processes/<process_id>/stop", methods=["POST"])
def stop_process(process_id: str):
    authz.require(g.tenant, "process.stop")
    stopped = request.form.get("stopped") == "on"
    g.repos.processes.set_stopped(process_id, stopped)
    g.repos.audit.record("process.stop", "processes", process_id, stopped=stopped)
    flash("flash.stopped" if stopped else "flash.resumed", "info")
    return redirect(url_for("main.processes"))


# ------------------------------------------------------------- executions ---


@main.route("/executions/<execution_id>")
def execution_detail(execution_id: str):
    repos = g.repos
    authz.require(g.tenant, "execution.view")
    execution = repos.executions.require(execution_id)
    result = loads(execution["result_json"], {}) or {}
    steps = []
    for row in repos.executions.steps(execution_id):
        steps.append(
            {
                "row": row,
                "action": loads(row["action_json"], {}) or {},
                "verification": loads(row["verification_json"], []) or [],
            }
        )
    return render_template(
        "execution.jinja2",
        execution=execution,
        steps=steps,
        trace=result.get("trace", []),
        outputs=result.get("outputs", {}),
        skill=repos.skills.get(execution["skill_id"]) if execution["skill_id"] else None,
        process=repos.processes.get(execution["process_id"]) if execution["process_id"] else None,
        children=repos.executions.children(execution_id),
        approvals=repos.approvals.for_execution(execution_id),
        skill_names=_skill_names(repos),
    )


# -------------------------------------------------------------- approvals ---


@main.route("/approvals")
def approvals():
    repos = g.repos
    authz.require(g.tenant, "approval.view")
    pending = repos.approvals.pending()
    return render_template(
        "approvals.jinja2",
        approvals=[
            {
                "row": row,
                "evidence": loads(row["evidence_json"], {}) or {},
                "skill": repos.skills.get(row["skill_id"]),
            }
            for row in pending
        ],
    )


@main.route("/approvals/<approval_id>/decide", methods=["POST"])
def decide_approval(approval_id: str):
    repos = g.repos
    runner = Runner(repos, driver_factory=make_driver_factory(repos),
                    prices=current_app.config["PLATFORM"].prices)
    approved = request.form.get("decision") == "approve"
    result = runner.decide_approval(
        approval_id, approved=approved, comment=request.form.get("comment", "")
    )
    flash("flash.approved" if approved else "flash.rejected", "success" if approved else "warning")
    return redirect(url_for("main.execution_detail", execution_id=result.execution_id))


# ----------------------------------------------------------------- health ---


@main.route("/health")
def health():
    repos = g.repos
    authz.require(g.tenant, "health.view")
    entries = health_module.organization_health(repos)
    return render_template(
        "health.jinja2",
        entries=sorted(entries, key=lambda item: (not item.needs_attention, item.name)),
        alerts=health_module.application_alerts(repos),
        summary=health_module.organization_summary(repos),
    )


# --------------------------------------------------------------- training ---


@main.route("/training")
def training():
    repos = g.repos
    authz.require(g.tenant, "training.review")
    proposals = []
    for row in repos.proposals.list():
        proposals.append(
            {
                "row": row,
                "proposal": loads(row["proposal_json"], {}) or {},
                "notes": loads(row["notes_json"], []) or [],
            }
        )
    return render_template(
        "training.jinja2",
        demonstrations=repos.demonstrations.list(),
        proposals=proposals,
        applications=repos.applications.list(),
    )


@main.route("/training/record", methods=["POST"])
def record_demonstration():
    """Record a demonstration against a scripted application.

    In a deployment this is where the desktop recorder hands its capture over.
    The rest of the pipeline — generalize, review, certify — is identical.
    """
    repos = g.repos
    authz.require(g.tenant, "training.record")
    application_id = request.form["application_id"]
    application = repos.applications.get(application_id)
    from ..engine.drivers import DriverMode, SimulatedDesktopDriver
    from ..simulation import application_for

    scripted = (
        application_for(application["name"], version=application["detected_version"])
        if application is not None
        else None
    )
    if scripted is None:
        flash("error.not_found", "warning")
        return redirect(url_for("main.training"))

    driver = SimulatedDesktopDriver(scripted, mode=DriverMode.SANDBOX)
    script = _demo_script(scripted.start_screen)
    recorder = replay_into_recorder(
        driver, script, title=application["name"], application_id=application_id
    )
    demonstration_id = recorder.save(repos)
    proposal = generalize(recorder.payload(), name=request.form.get("name", ""))
    repos.proposals.create(
        demonstration_id=demonstration_id,
        proposal=proposal.to_dict(),
        notes=proposal.notes,
    )
    flash("flash.saved", "success")
    return redirect(url_for("main.training"))


@main.route("/training/proposals/<proposal_id>")
def proposal_detail(proposal_id: str):
    repos = g.repos
    authz.require(g.tenant, "training.review")
    row = repos.proposals.get(proposal_id)
    if row is None:
        flash("error.not_found", "warning")
        return redirect(url_for("main.training"))
    proposal = loads(row["proposal_json"], {}) or {}
    demonstration = repos.demonstrations.get(row["demonstration_id"])
    payload = loads(demonstration["payload_json"], {}) if demonstration else {}
    return render_template(
        "proposal.jinja2",
        row=row,
        proposal=proposal,
        notes=loads(row["notes_json"], []) or [],
        demonstration=demonstration,
        events=payload.get("events", []),
        split=_split_for(proposal),
    )


@main.route("/training/proposals/<proposal_id>/review", methods=["POST"])
def review_proposal(proposal_id: str):
    repos = g.repos
    authz.require(g.tenant, "training.review")
    row = repos.proposals.get(proposal_id)
    proposal = loads(row["proposal_json"], {}) or {}
    # The reviewer's edits are what gets stored, never the raw proposal.
    proposal["name"] = request.form.get("name") or proposal.get("name", "")
    proposal["precondition"] = request.form.get("precondition", "")
    proposal["postcondition"] = request.form.get("postcondition", "")
    proposal["semantic_check"] = request.form.get("semantic_check") or None
    proposal["risk_classification"] = request.form.get(
        "risk_classification", proposal.get("risk_classification")
    )
    repos.proposals.accept(proposal_id, reviewed_by=g.tenant.user_id or "", accepted=proposal)
    flash("flash.saved", "success")
    return redirect(url_for("main.proposal_detail", proposal_id=proposal_id))


@main.route("/training/proposals/<proposal_id>/accept", methods=["POST"])
def accept_proposal(proposal_id: str):
    skill_id = create_skill_from_proposal(g.repos, proposal_id)
    flash("flash.proposal_accepted", "success")
    return redirect(url_for("main.skill_detail", skill_id=skill_id))


# ----------------------------------------------------- applications, roles ---


@main.route("/applications")
def applications():
    repos = g.repos
    rows = repos.applications.list()
    return render_template(
        "applications.jinja2",
        applications=rows,
        skills={row["id"]: repos.skills.list(application_id=row["id"]) for row in rows},
    )


@main.route("/applications/<application_id>/version", methods=["POST"])
def change_version(application_id: str):
    """Record a new detected version and send bound skills to recertification.

    Exposed in the interface because this is the failure classic RPA never
    handled: the application moved and nobody noticed.
    """
    repos = g.repos
    authz.require(g.tenant, "organization.manage")
    application = repos.applications.get(application_id)
    previous = application["detected_version"]
    version = request.form.get("version", "").strip()
    if version and version != previous:
        repos.applications.set_version(application_id, version)
        repos.applications.set_health(application_id, "version_changed")
        affected = repos.skills.flag_application_for_review(
            application_id, "certification.app_version_changed"
        )
        repos.audit.record("application.version_changed", "applications", application_id,
                           previous=previous, current=version, affected=len(affected))
        flash("certification.app_version_changed", "warning")
    return redirect(url_for("main.applications"))


@main.route("/job-roles")
def job_roles():
    repos = g.repos
    rows = repos.job_roles.list()
    return render_template(
        "job_roles.jinja2",
        job_roles=rows,
        skills={row["id"]: repos.job_roles.skills_of(row["id"]) for row in rows},
        users={row["id"]: row for row in repos.users.list()},
    )


@main.route("/audit")
def audit():
    authz.require(g.tenant, "audit.view")
    return render_template("audit.jinja2", entries=g.repos.audit.list(limit=200))


@main.route("/organization/stop", methods=["POST"])
def stop_organization():
    authz.require(g.tenant, "organization.manage")
    stopped = request.form.get("stopped") == "on"
    g.repos.organizations.set_stopped(
        g.tenant.organization_id, stopped, request.form.get("reason", "")
    )
    g.repos.audit.record("organization.stop", "organizations", g.tenant.organization_id,
                         stopped=stopped)
    flash("flash.stopped" if stopped else "flash.resumed", "warning" if stopped else "info")
    return redirect(url_for("main.dashboard"))


# ----------------------------------------------------------------- export ---


@main.route("/export.<fmt>")
def export(fmt: str):
    repos = g.repos
    config = current_app.config["PLATFORM"]
    config.export_dir.mkdir(parents=True, exist_ok=True)
    span = EvidenceRange(
        request.args.get("from") or EvidenceRange.all_time().start,
        request.args.get("to") or EvidenceRange.all_time().end,
    )
    report = EvidenceReport(repos, g.t, span=span)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    if fmt == "pdf":
        path = report.to_pdf(str(config.export_dir / f"evidencia-{stamp}.pdf"))
        return send_file(path, as_attachment=True, download_name=f"evidencia-{stamp}.pdf")
    path = report.to_xlsx(str(config.export_dir / f"evidencia-{stamp}.xlsx"))
    return send_file(path, as_attachment=True, download_name=f"evidencia-{stamp}.xlsx")


# ---------------------------------------------------------------- helpers ---


def _skill_names(repos) -> dict[str, str]:
    return {row["id"]: row["name"] for row in repos.skills.list()}


def _current_period() -> str:
    now = datetime.now(timezone.utc)
    return f"{now.year}-{now.month:02d}"


def _demo_cases(repos, skill_id: str) -> list[dict]:
    """Known cases used for dry runs and manual runs from the interface."""
    from ..simulation import BANK_ROWS, LEDGER_ROWS

    skill = repos.skills.require(skill_id)
    if skill["executor"] == "data_transform":
        return [{"erp_rows": LEDGER_ROWS, "banco_rows": BANK_ROWS}]
    return [
        {
            "periodo": _current_period(),
            "destinatario": "gerencia@example.test",
            "asunto": "Conciliación",
        }
    ]


def _demo_script(start_screen: str) -> list[dict]:
    """A short scripted demonstration per application family."""
    scripts = {
        "erp.main": [
            {"action": "click", "ref": "menu.reports"},
            {"action": "type_text", "ref": "txt.period", "value": "2026-01"},
            {"action": "click", "ref": "btn.export"},
            {"action": "click", "ref": "btn.home"},
        ],
        "bank.main": [
            {"action": "click", "ref": "menu.statements"},
            {"action": "type_text", "ref": "txt.period", "value": "2026-01"},
            {"action": "click", "ref": "btn.download"},
            {"action": "click", "ref": "btn.back"},
        ],
        "mail.main": [
            {"action": "click", "ref": "btn.compose"},
            {"action": "type_text", "ref": "txt.to", "value": "gerencia@example.test"},
            {"action": "click", "ref": "btn.send"},
            {"action": "click", "ref": "btn.home"},
        ],
        "ticket.main": [
            {"action": "click", "ref": "menu.reports"},
            {"action": "click", "ref": "btn.export"},
            {"action": "click", "ref": "btn.back"},
        ],
    }
    return scripts.get(start_screen, [])


def _split_for(proposal: dict) -> list[dict]:
    """Only compute a split plan when the recording actually exceeds the limit."""
    if not proposal.get("exceeds_screen_budget"):
        return []
    from ..training.generalizer import Proposal, ProposedStep

    rebuilt = Proposal(
        name=proposal.get("name", ""),
        start_anchor=proposal.get("start_anchor", ""),
        screens=proposal.get("screens", []),
        steps=[ProposedStep(**step) for step in proposal.get("steps", [])],
    )
    return split_plan(rebuilt)


def register_error_handlers(app) -> None:
    @app.errorhandler(PlatformError)
    def _platform_error(error: PlatformError):
        message = error.localized(g.t) if g.get("t") else error.message_key
        return render_template("error.jinja2", message=message, error=error), error.http_status

    @app.errorhandler(404)
    def _not_found(_error):
        message = g.t("error.page_not_found") if g.get("t") else "error.page_not_found"
        return render_template("error.jinja2", message=message, error=None), 404


def template_globals() -> dict:
    """Values every template can use without a view passing them."""
    repos = g.get("repos")
    queue_total = 0
    approval_total = 0
    if repos is not None:
        counts = repos.executions.queue_counts()
        queue_total = counts.get(ExecutionStatus.QUEUED.value, 0) + counts.get(
            ExecutionStatus.RUNNING.value, 0
        )
        approval_total = counts.get(ExecutionStatus.WAITING_APPROVAL.value, 0)
    return {
        "queue_total": queue_total,
        "approval_total": approval_total,
        "t": g.get("t"),
        "locale": g.get("locale"),
        "locales": SUPPORTED_LOCALES,
        "locale_names": LOCALE_NAMES,
        "organization": g.get("organization"),
        "current_user": g.get("user"),
        "tenant": g.get("tenant"),
        "ExecutionStatus": ExecutionStatus,
        "SkillStatus": SkillStatus,
        "RiskClass": RiskClass,
        "ApprovalStatus": ApprovalStatus,
        "Trigger": Trigger,
        "SelectorTier": SelectorTier,
        "has_permission": lambda permission: (
            authz.has_permission(g.tenant.role, permission) if g.get("tenant") else False
        ),
    }
