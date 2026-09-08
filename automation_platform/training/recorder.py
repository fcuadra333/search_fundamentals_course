"""Training mode 1: recording (§3).

The designer performs the task once with the recorder on. What is captured:
the sequence of actions, screenshots, the accessibility tree at every step, and
the values typed in.

**The recording is not the skill.** It is evidence. The generalizer proposes a
skill definition from it, the designer reviews that proposal, and only then does
anything become publishable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ..security import screen_content


@dataclass
class RecordedEvent:
    """One observed action, with the screen it happened on."""

    index: int
    action_type: str
    screen_key: str
    window_title: str = ""
    element_ref: str = ""
    element_role: str = ""
    element_name: str = ""
    automation_id: str | None = None
    element_label: str = ""
    element_bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    value: str | None = None
    screenshot_ref: str = ""
    accessibility: list[dict[str, Any]] = field(default_factory=list)
    screen_text: list[str] = field(default_factory=list)
    suspicious_text: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Recorder:
    """Collects events while the designer works.

    Screen text is quarantined at capture time. Text that reads like an
    instruction is flagged here so the designer sees it during review, rather
    than being discovered later by an agent that might act on it.
    """

    def __init__(self, *, application_id: str | None = None, title: str = ""):
        self.application_id = application_id
        self.title = title
        self.events: list[RecordedEvent] = []
        self.app_version: str = ""

    def capture(self, observation, *, action_type: str, element=None, value: str | None = None) -> RecordedEvent:
        quarantined = screen_content.quarantine(observation.text)
        event = RecordedEvent(
            index=len(self.events) + 1,
            action_type=action_type,
            screen_key=observation.screen_key,
            window_title=observation.window_title,
            element_ref=getattr(element, "ref", "") or "",
            element_role=getattr(element, "role", "") or "",
            element_name=getattr(element, "name", "") or "",
            automation_id=getattr(element, "automation_id", None),
            element_label=getattr(element, "label", "") or "",
            element_bounds=tuple(getattr(element, "bounds", (0, 0, 0, 0))),
            value=value,
            screenshot_ref=observation.screenshot_ref,
            accessibility=[
                {
                    "ref": node.ref,
                    "role": node.role,
                    "name": node.name,
                    "automation_id": node.automation_id,
                    "label": node.label,
                    "bounds": list(node.bounds),
                }
                for node in observation.elements
            ],
            screen_text=list(observation.text),
            suspicious_text=quarantined.suspicious,
        )
        self.app_version = observation.app_version or self.app_version
        self.events.append(event)
        return event

    def payload(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "application_id": self.application_id,
            "app_version": self.app_version,
            "events": [event.to_dict() for event in self.events],
            "screens": self.screens(),
        }

    def screens(self) -> list[str]:
        """Distinct screens in order of first appearance."""
        seen: list[str] = []
        for event in self.events:
            if event.screen_key not in seen:
                seen.append(event.screen_key)
        return seen

    def save(self, repos, *, skill_id: str | None = None) -> str:
        return repos.demonstrations.create(
            payload=self.payload(),
            title=self.title,
            application_id=self.application_id,
            skill_id=skill_id,
            recorded_by=repos.tenant.user_id,
        )


def replay_into_recorder(driver, script: list[dict[str, Any]], *, title: str = "",
                         application_id: str | None = None) -> Recorder:
    """Drive a driver through a scripted sequence and record what happens.

    This is how the demo data and the tests produce realistic recordings without
    a human at a keyboard.
    """
    recorder = Recorder(application_id=application_id, title=title)
    for entry in script:
        observation = driver.observe()
        element = observation.element(entry.get("ref", "")) if entry.get("ref") else None
        recorder.capture(
            observation,
            action_type=entry["action"],
            element=element,
            value=entry.get("value"),
        )
        driver.act(entry["action"], element, entry.get("value"))
    return recorder
