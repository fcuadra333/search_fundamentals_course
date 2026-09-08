"""Domain vocabularies.

The specification (`docs/ESPECIFICACION.md`) is written in Spanish and the code
is written in English. `docs/GLOSARIO.md` holds the normative mapping between
both vocabularies; the Spanish label of every member below lives in the i18n
catalogs, never in the code.
"""

from enum import Enum


class LocalizedEnum(str, Enum):
    """Enum whose members carry a translation key instead of a display name."""

    @property
    def label_key(self) -> str:
        return f"enum.{type(self).__name__}.{self.value}"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]

    @classmethod
    def parse(cls, raw: str | None):
        if raw is None:
            return None
        try:
            return cls(raw)
        except ValueError:
            return None


class Executor(LocalizedEnum):
    """`ejecutor` in the specification (§2, §4)."""

    COMPUTER_USE = "computer_use"
    DATA_TRANSFORM = "data_transform"
    API_CALL = "api_call"
    LLM_EXTRACT = "llm_extract"

    @property
    def available_in_v1(self) -> bool:
        return self in (Executor.COMPUTER_USE, Executor.DATA_TRANSFORM)


class RiskClass(LocalizedEnum):
    """`clasificacion_riesgo` (§7). Ordering matters: higher is more dangerous."""

    READ_ONLY = "read_only"
    REVERSIBLE_WRITE = "reversible_write"
    IRREVERSIBLE_WRITE = "irreversible_write"

    @property
    def requires_prior_approval(self) -> bool:
        return self is RiskClass.IRREVERSIBLE_WRITE


class SkillStatus(LocalizedEnum):
    """`estado` of a skill (§2)."""

    DRAFT = "draft"
    IN_CERTIFICATION = "in_certification"
    PUBLISHED = "published"
    NEEDS_REVIEW = "needs_review"


class Role(LocalizedEnum):
    """User roles (§2). Permissions live in `security/authz.py`."""

    ADMIN = "admin"
    DESIGNER = "designer"
    OPERATOR = "operator"
    APPROVER = "approver"


class ExecutionStatus(LocalizedEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ESCALATED = "escalated"
    STOPPED = "stopped"

    @property
    def is_terminal(self) -> bool:
        return self in (
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.FAILED,
            ExecutionStatus.ESCALATED,
            ExecutionStatus.STOPPED,
        )


class StepStatus(LocalizedEnum):
    PASSED = "passed"
    RETRIED = "retried"
    FAILED = "failed"
    SKIPPED = "skipped"


class SelectorTier(int, Enum):
    """Selector cascade (§5). Lower is preferred."""

    AUTOMATION_ID = 1
    ACCESSIBLE_ROLE_NAME = 2
    VISIBLE_LABEL = 3
    RELATIVE_TO_ANCHOR = 4
    PIXEL_COORDINATES = 5

    @property
    def label_key(self) -> str:
        return f"enum.SelectorTier.{self.name.lower()}"

    @property
    def marks_skill_fragile(self) -> bool:
        # Falling all the way to pixel coordinates marks the skill fragile (§5).
        return self is SelectorTier.PIXEL_COORDINATES


class ValidationLayer(LocalizedEnum):
    """Layered validation (§6)."""

    L1_PRECONDITION = "l1_precondition"
    L2_ACTION = "l2_action"
    L3_POSTCONDITION = "l3_postcondition"
    L4_SEMANTIC = "l4_semantic"
    L5_HUMAN = "l5_human"


class FailurePolicy(LocalizedEnum):
    """What the engine does when a validation layer fails (§6)."""

    ABORT = "abort"
    RETRY_ONCE = "retry_once"
    ESCALATE = "escalate"
    REVIEW_QUEUE = "review_queue"
    APPROVAL_QUEUE = "approval_queue"


class ApprovalStatus(LocalizedEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class CertificationStatus(LocalizedEnum):
    PASSED = "passed"
    FAILED = "failed"
    FRAGILE = "fragile"


class Measurement(LocalizedEnum):
    """Every cost or reliability number states whether it is measured or
    estimated (convention in `CLAUDE.md`)."""

    MEASURED = "measured"
    ESTIMATED = "estimated"


class Trigger(LocalizedEnum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"
    API = "api"
    DRY_RUN = "dry_run"


class ActionType(LocalizedEnum):
    """Action kinds a `computer_use` step can take."""

    CLICK = "click"
    TYPE_TEXT = "type_text"
    SELECT_OPTION = "select_option"
    KEY_PRESS = "key_press"
    WAIT_FOR = "wait_for"
    READ_VALUE = "read_value"
    OPEN_MENU = "open_menu"
    SAVE_FILE = "save_file"


class InterventionReason(LocalizedEnum):
    PRECONDITION_FAILED = "precondition_failed"
    POSTCONDITION_FAILED = "postcondition_failed"
    SELECTOR_NOT_FOUND = "selector_not_found"
    DRIFT_DETECTED = "drift_detected"
    SEMANTIC_CHECK_FAILED = "semantic_check_failed"
    APPROVAL_REJECTED = "approval_rejected"
    UNEXPECTED_DIALOG = "unexpected_dialog"
    SCREEN_INSTRUCTION_ATTEMPT = "screen_instruction_attempt"
    OPERATOR_STOP = "operator_stop"


class ApplicationHealth(LocalizedEnum):
    HEALTHY = "healthy"
    VERSION_CHANGED = "version_changed"
    UNREACHABLE = "unreachable"
