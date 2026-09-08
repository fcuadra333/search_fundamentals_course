"""Domain errors.

Every error carries a translation key so the same failure renders in Spanish or
in English without the engine knowing which locale is active.
"""


class PlatformError(Exception):
    """Base error. `message_key` is resolved by the i18n catalog."""

    message_key = "error.generic"
    http_status = 400

    def __init__(self, message: str = "", **params):
        super().__init__(message or self.message_key)
        self.params = params

    def localized(self, translate) -> str:
        return translate(self.message_key, **self.params)


class TenantScopeError(PlatformError):
    """A query reached the database without an organization filter (§2)."""

    message_key = "error.tenant_scope"
    http_status = 500


class CrossTenantAccessError(PlatformError):
    """A record from another organization was requested."""

    message_key = "error.cross_tenant"
    http_status = 404


class InvariantViolation(PlatformError):
    """A data-model invariant was violated (§2)."""

    message_key = "error.invariant"


class ScreenBudgetExceeded(InvariantViolation):
    """More than three screens in a single skill (§1). Publication is refused."""

    message_key = "error.screen_budget"


class MissingPostcondition(InvariantViolation):
    message_key = "error.missing_postcondition"


class ApprovalRequired(PlatformError):
    """An irreversible write was reached without a prior approval (§7)."""

    message_key = "error.approval_required"
    http_status = 409


class ProhibitedAction(PlatformError):
    """One of the absolute prohibitions in §7."""

    message_key = "error.prohibited_action"
    http_status = 403


class PermissionDenied(PlatformError):
    message_key = "error.permission_denied"
    http_status = 403


class NotCertified(PlatformError):
    message_key = "error.not_certified"


class ExecutorUnavailable(PlatformError):
    """Executor declared in the data model but out of scope for v1 (§10)."""

    message_key = "error.executor_unavailable"


class DriftDetected(PlatformError):
    """The screen no longer matches the certified fingerprint (§5)."""

    message_key = "error.drift_detected"


class SelectorNotFound(PlatformError):
    message_key = "error.selector_not_found"


class PreconditionFailed(PlatformError):
    message_key = "error.precondition_failed"


class PostconditionFailed(PlatformError):
    message_key = "error.postcondition_failed"


class ExecutionStopped(PlatformError):
    """The kill switch is engaged for this organization, process or skill (§7)."""

    message_key = "error.execution_stopped"


class RateLimitExceeded(PlatformError):
    message_key = "error.rate_limited"
    http_status = 429


class UnreviewedProposal(PlatformError):
    """A generalized proposal cannot be published without human review (§3)."""

    message_key = "error.unreviewed_proposal"
