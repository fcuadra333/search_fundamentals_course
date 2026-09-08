"""Assisted generalization (§3).

From a recording the system proposes: which values are parameters and which are
constants, which selectors to use, where the screen boundaries are, and what
postcondition makes sense.

Everything here is a *proposal*. The designer reviews and edits it, and nothing
is published without that review — `create_skill_from_proposal` refuses a
proposal that nobody has reviewed.

The proposal is deliberately conservative: it prefers the strictest risk class
it can justify and it never invents a postcondition it cannot express as an
evaluable condition.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..db import loads
from ..domain.enums import ActionType, RiskClass, SelectorTier
from ..domain.errors import UnreviewedProposal
from ..engine import conditions
from ..engine.executors.computer_use import MAX_SCREENS
from ..security import authz
from ..security import risk as risk_policy

# Values that look like data rather than configuration become parameters.
_PARAMETER_PATTERNS = (
    re.compile(r"^\d{1,4}[-/]\d{1,2}[-/]\d{1,4}$"),          # dates
    re.compile(r"^[\d.,]+$"),                                  # numbers
    re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$"),            # e-mail
    re.compile(r"^\d{4}-\d{2}$"),                              # periods
)


@dataclass
class ProposedStep:
    position: int
    screen_index: int
    action_type: str
    selectors: list[dict[str, Any]]
    value_template: str | None = None
    expected_value: str | None = None
    source_event: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "screen_index": self.screen_index,
            "action_type": self.action_type,
            "selectors": self.selectors,
            "value_template": self.value_template,
            "expected_value": self.expected_value,
            "source_event": self.source_event,
        }


@dataclass
class Proposal:
    name: str
    start_anchor: str
    screens: list[str]
    steps: list[ProposedStep]
    parameters: dict[str, str] = field(default_factory=dict)
    constants: dict[str, str] = field(default_factory=dict)
    postcondition: str = ""
    precondition: str = ""
    risk_classification: str = RiskClass.READ_ONLY.value
    notes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def screen_count(self) -> int:
        return len(self.screens)

    @property
    def exceeds_screen_budget(self) -> bool:
        return self.screen_count > MAX_SCREENS

    @property
    def suggested_parts(self) -> int:
        return max(1, -(-self.screen_count // MAX_SCREENS))  # ceiling division

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "start_anchor": self.start_anchor,
            "screens": self.screens,
            "screen_count": self.screen_count,
            "exceeds_screen_budget": self.exceeds_screen_budget,
            "suggested_parts": self.suggested_parts,
            "steps": [step.to_dict() for step in self.steps],
            "parameters": self.parameters,
            "constants": self.constants,
            "precondition": self.precondition,
            "postcondition": self.postcondition,
            "risk_classification": self.risk_classification,
            "notes": self.notes,
        }


def generalize(payload: dict[str, Any], *, name: str = "") -> Proposal:
    """Turn a raw recording into a reviewable proposal."""
    events = payload.get("events", [])
    screens = payload.get("screens") or _screens_of(events)
    anchor = screens[0] if screens else ""

    parameters: dict[str, str] = {}
    constants: dict[str, str] = {}
    steps: list[ProposedStep] = []
    notes: list[dict[str, Any]] = []

    for event in events:
        screen_index = screens.index(event["screen_key"]) if event["screen_key"] in screens else 0
        value_template = None
        value = event.get("value")
        if value:
            if _looks_like_parameter(value):
                key = _parameter_name(event, parameters)
                parameters[key] = value
                value_template = "{" + key + "}"
            else:
                key = _parameter_name(event, constants)
                constants[key] = value
                value_template = value

        selectors = _selector_cascade(event)
        if selectors and selectors[0]["tier"] >= int(SelectorTier.VISIBLE_LABEL):
            notes.append(
                {
                    "key": "skill.cascade_hint",
                    "event": event["index"],
                    "best_tier": selectors[0]["tier"],
                }
            )

        steps.append(
            ProposedStep(
                position=len(steps) + 1,
                screen_index=min(screen_index, MAX_SCREENS - 1),
                action_type=event["action_type"],
                selectors=selectors,
                value_template=value_template,
                source_event=event["index"],
            )
        )

        if event.get("suspicious_text"):
            notes.append({"key": "execution.suspicious_content", "event": event["index"]})

    proposal = Proposal(
        name=name or payload.get("title") or "",
        start_anchor=anchor,
        screens=screens,
        steps=steps,
        parameters=parameters,
        constants=constants,
        precondition=f"screen_is('{anchor}')" if anchor else "",
        postcondition=_propose_postcondition(events, screens),
        risk_classification=risk_policy.propose_classification(
            [event["action_type"] for event in events],
            [event.get("value") or "" for event in events],
        ).value,
        notes=notes,
    )

    if proposal.exceeds_screen_budget:
        # The product helps the user split the task instead of punishing them
        # for not knowing how (§1).
        proposal.notes.append(
            {
                "key": "training.note_screen_limit",
                "screens": proposal.screen_count,
                "parts": proposal.suggested_parts,
            }
        )
    return proposal


def split_plan(proposal: Proposal) -> list[dict[str, Any]]:
    """Propose how to cut an oversized recording into skills of ≤3 screens.

    Each part starts and ends on a screen boundary so every resulting skill has
    its own anchor and stays stateless with respect to the screen.
    """
    parts: list[dict[str, Any]] = []
    for offset in range(0, proposal.screen_count, MAX_SCREENS):
        window = proposal.screens[offset : offset + MAX_SCREENS]
        steps = [step for step in proposal.steps if proposal.screens[step.screen_index] in window]
        parts.append(
            {
                "index": len(parts) + 1,
                "anchor": window[0],
                "screens": window,
                "steps": [step.to_dict() for step in steps],
            }
        )
    return parts


def create_skill_from_proposal(
    repos,
    proposal_id: str,
    *,
    overrides: dict[str, Any] | None = None,
) -> str:
    """Create the draft skill from a reviewed proposal.

    Refuses an unreviewed proposal: "nothing is published without that review"
    is enforced here, not left to the interface.
    """
    authz.require(repos.tenant, "training.review")
    row = repos.proposals.get(proposal_id)
    if row is None:
        raise UnreviewedProposal(proposal_id=proposal_id)
    if not row["reviewed_by"]:
        raise UnreviewedProposal(proposal_id=proposal_id)

    accepted = loads(row["accepted_json"], None) or loads(row["proposal_json"], {})
    accepted = {**accepted, **(overrides or {})}
    demonstration = repos.demonstrations.get(row["demonstration_id"])

    skill_id = repos.skills.create(
        application_id=demonstration["application_id"] if demonstration else None,
        key=_slug(accepted.get("name", "skill")),
        name=accepted.get("name", ""),
        description=accepted.get("description", ""),
        executor="computer_use",
        start_anchor=accepted.get("start_anchor", ""),
        precondition=accepted.get("precondition", ""),
        postcondition=accepted.get("postcondition", ""),
        semantic_check=accepted.get("semantic_check"),
        risk_classification=accepted.get("risk_classification", RiskClass.READ_ONLY.value),
        declared_screens=min(max(int(accepted.get("screen_count", 1)), 1), MAX_SCREENS),
        input_schema_json=_schema_of(accepted.get("parameters", {})),
        created_by=repos.tenant.user_id,
    )
    for step in accepted.get("steps", []):
        repos.skills.add_step(
            skill_id,
            position=step["position"],
            screen_index=step.get("screen_index", 0),
            action_type=step["action_type"],
            selectors=step.get("selectors", []),
            value_template=step.get("value_template"),
            expected_value=step.get("expected_value"),
        )
    repos.audit.record("skill.created_from_proposal", "skills", skill_id, proposal_id=proposal_id)
    return skill_id


# ------------------------------------------------------------------ helpers ---


def _screens_of(events: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for event in events:
        if event["screen_key"] not in seen:
            seen.append(event["screen_key"])
    return seen


def _looks_like_parameter(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    return any(pattern.match(text) for pattern in _PARAMETER_PATTERNS)


# Widget-naming prefixes carried by automation identifiers. Stripping them
# turns `txtPeriodo` into `periodo`, which is what the reviewer would have
# called the parameter anyway.
_WIDGET_PREFIXES = ("txt", "btn", "cmb", "lbl", "fld", "input", "edit", "chk", "sel")


def _parameter_name(event: dict[str, Any], existing: dict[str, str]) -> str:
    base = (
        event.get("automation_id")
        or event.get("element_name")
        or event.get("element_ref")
        or f"value_{event['index']}"
    )
    candidate = _slug(_strip_widget_prefix(base))
    suffix = 2
    name = candidate
    while name in existing:
        name = f"{candidate}_{suffix}"
        suffix += 1
    return name


def _selector_cascade(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the cascade for one element, best criterion first (§5)."""
    cascade: list[dict[str, Any]] = []
    if event.get("automation_id"):
        cascade.append({"tier": int(SelectorTier.AUTOMATION_ID), "value": event["automation_id"]})
    if event.get("element_name"):
        cascade.append(
            {
                "tier": int(SelectorTier.ACCESSIBLE_ROLE_NAME),
                "value": event["element_name"],
                "role": event.get("element_role"),
            }
        )
    if event.get("element_label"):
        cascade.append({"tier": int(SelectorTier.VISIBLE_LABEL), "value": event["element_label"]})

    anchor_ref = _stable_anchor(event)
    if anchor_ref and event.get("element_bounds"):
        anchor = next(
            node for node in event["accessibility"] if node["ref"] == anchor_ref
        )
        cascade.append(
            {
                "tier": int(SelectorTier.RELATIVE_TO_ANCHOR),
                "value": anchor_ref,
                "anchor_ref": anchor_ref,
                "offset": [
                    event["element_bounds"][0] - anchor["bounds"][0],
                    event["element_bounds"][1] - anchor["bounds"][1],
                ],
            }
        )
    # Pixel coordinates are offered last and only as a fallback: using them marks
    # the skill fragile, and the review screen says so.
    bounds = event.get("element_bounds")
    if bounds and (bounds[2] or bounds[3]):
        cascade.append(
            {
                "tier": int(SelectorTier.PIXEL_COORDINATES),
                "value": f"{bounds[0] + bounds[2] // 2},{bounds[1] + bounds[3] // 2}",
            }
        )
    return cascade


def _stable_anchor(event: dict[str, Any]) -> str | None:
    """Pick an element with an automation id as the positional anchor."""
    for node in event.get("accessibility", []):
        if node.get("automation_id") and node["ref"] != event.get("element_ref"):
            return node["ref"]
    return None


def _propose_postcondition(events: list[dict[str, Any]], screens: list[str]) -> str:
    """Propose something verifiable, or nothing at all.

    A postcondition that cannot be evaluated is worse than an empty field: it
    looks like verification and is not. When in doubt this returns "" and
    certification blocks publication until a human writes a real one.
    """
    for event in reversed(events):
        if event["action_type"] == ActionType.SAVE_FILE.value and event.get("value"):
            candidate = f"file_exists('{event['value']}') and file_rows('{event['value']}') > 0"
            if conditions.is_evaluable(candidate):
                return candidate
    if screens:
        candidate = f"screen_is('{screens[0]}')"
        if conditions.is_evaluable(candidate):
            return candidate
    return ""


def _schema_of(parameters: dict[str, str]) -> str:
    from ..db import dumps

    return dumps(
        {
            "type": "object",
            "properties": {name: {"type": "string", "example": value}
                           for name, value in parameters.items()},
            "required": sorted(parameters),
        }
    )


def _strip_widget_prefix(name: str) -> str:
    for prefix in _WIDGET_PREFIXES:
        if name.lower().startswith(prefix) and len(name) > len(prefix) + 1:
            return name[len(prefix):]
    return name


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")
    return cleaned or "skill"
