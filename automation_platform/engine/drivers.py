"""Desktop drivers.

`DesktopDriver` is the seam between the platform and a real machine. Everything
above it — the step cycle, validation, drift detection, certification — works
against this interface and never against pixels directly.

Two rules from the specification shape this interface:

- Prefer the accessibility tree over pixel coordinates (rule 6). Observations
  therefore carry structured elements, and a screenshot reference is only
  evidence, not the primary way to find things.
- Screen content is data, never instruction (rule 4). `ScreenObservation.text`
  is quarantined text: `security.screen_content` is the only module allowed to
  interpret it, and it only classifies it, never obeys it.

`SimulatedDesktopDriver` is the in-repo implementation. It drives a scripted
application model and is what dry runs, certification, the variation test, the
demo data and the tests use. A production driver (UI Automation on Windows,
AT-SPI on Linux, AX on macOS) implements the same five methods.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Iterable, Protocol

from ..domain.errors import ProhibitedAction


class DriverMode(str, Enum):
    """How much a run is allowed to actually do.

    - `PRODUCTION`: the real thing.
    - `SANDBOX`: an isolated session. Side effects happen inside that session and
      never reach production, which is what makes a dry run able to verify a
      postcondition about a file it produced (§3, §4).
    - `PREVIEW`: propose only. The run walks up to the first effectful action and
      stops there, so an approver sees what the agent intends to do before an
      irreversible write happens (§7).
    """

    PRODUCTION = "production"
    SANDBOX = "sandbox"
    PREVIEW = "preview"


# What the executor reports when a preview stopped at an effectful action.
PREVIEW_HALT = "preview.not_performed"

# Affordances that dismiss a dialog without agreeing to anything. "OK" and
# "Aceptar" are deliberately absent: clicking them on a dialog the platform did
# not expect is how a fake pop-up gets its confirmation.
SAFE_DISMISS_NAMES = ("cerrar", "close", "cancelar", "cancel", "descartar", "discard", "x")


@dataclass(frozen=True)
class UiElement:
    """One node of the accessibility tree."""

    ref: str
    role: str
    name: str = ""
    automation_id: str | None = None
    label: str = ""
    value: str | None = None
    enabled: bool = True
    # (x, y, width, height) in window coordinates. Last-resort selector only.
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)

    def signature(self) -> str:
        """Structural identity, deliberately free of coordinates and values."""
        return f"{self.role}|{self.automation_id or ''}|{self.name}"


@dataclass(frozen=True)
class ScreenObservation:
    """What the agent can see at one moment."""

    screen_key: str
    window_title: str = ""
    app_version: str = ""
    elements: tuple[UiElement, ...] = ()
    # Visible free text. Untrusted: it is reported, never followed.
    text: tuple[str, ...] = ()
    screenshot_ref: str = ""
    size: tuple[int, int] = (1280, 800)
    dialog: str | None = None

    def element(self, ref: str) -> UiElement | None:
        for element in self.elements:
            if element.ref == ref:
                return element
        return None

    def structural_signature(self) -> frozenset[str]:
        """Set of element signatures. Drift is measured over this set."""
        return frozenset(element.signature() for element in self.elements)

    def fingerprint(self) -> str:
        """Stable hash of the screen structure, recorded at certification."""
        joined = "\n".join(sorted(self.structural_signature()))
        payload = f"{self.screen_key}\n{joined}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


@dataclass
class ActionResult:
    """Outcome of one driver action, plus what it cost."""

    ok: bool
    detail: str = ""
    observation: ScreenObservation | None = None
    artifacts: dict[str, Any] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0


class DesktopDriver(Protocol):
    """The five operations every driver has to provide."""

    def observe(self) -> ScreenObservation: ...

    def act(self, action_type: str, element: UiElement | None, value: str | None) -> ActionResult: ...

    def app_version(self) -> str: ...

    def artifacts(self) -> dict[str, Any]: ...

    def reset(self) -> None: ...


# ------------------------------------------------------------- simulation ---


@dataclass
class ScriptedScreen:
    """One screen of a simulated application."""

    key: str
    title: str
    elements: tuple[UiElement, ...] = ()
    text: tuple[str, ...] = ()
    # (action_type, element_ref) -> next screen key.
    transitions: dict[tuple[str, str], str] = field(default_factory=dict)
    # Side effects an action produces, e.g. a file appearing.
    effects: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)


@dataclass
class ScriptedApplication:
    """A deterministic stand-in for a real target application."""

    name: str
    version: str
    screens: dict[str, ScriptedScreen]
    start_screen: str

    def screen(self, key: str) -> ScriptedScreen:
        return self.screens[key]


class SimulatedDesktopDriver:
    """Drives a `ScriptedApplication`.

    It supports the two perturbations certification requires (§3): a resized
    window and an injected unexpected dialog. Both are what make the variation
    test mean something instead of replaying the happy path ten times.
    """

    # Estimated, not measured: a screenshot costs roughly this many input
    # tokens (§9). `engine.cost` is the single place that owns the range.
    SCREENSHOT_TOKENS = 1350

    def __init__(
        self,
        application: ScriptedApplication,
        *,
        mode: DriverMode = DriverMode.PRODUCTION,
        window_scale: float = 1.0,
        inject_dialog: str | None = None,
        dialog_dismissible: bool = True,
        failure_hook: Callable[[str, str], bool] | None = None,
    ):
        self.application = application
        self.mode = mode
        self.window_scale = window_scale
        self.inject_dialog = inject_dialog
        self.dialog_dismissible = dialog_dismissible
        self.failure_hook = failure_hook
        self.current_screen = application.start_screen
        self._artifacts: dict[str, Any] = {}
        self._pending_dialog = inject_dialog
        self.observations = 0
        self.proposed: list[dict[str, Any]] = []

    # -- observation ---------------------------------------------------------

    def observe(self) -> ScreenObservation:
        self.observations += 1
        screen = self.application.screen(self.current_screen)
        width = int(1280 * self.window_scale)
        height = int(800 * self.window_scale)
        elements = tuple(
            replace(element, bounds=_scale(element.bounds, self.window_scale))
            for element in screen.elements
        )
        text = screen.text
        dialog = None
        if self._pending_dialog:
            # An unexpected dialog is a real screen element, and its text is
            # exactly the kind of content that must never become an instruction.
            # It offers both a safe way out and a tempting one; the executor is
            # only ever allowed to take the safe one.
            dialog = self._pending_dialog
            affordances = [
                UiElement(ref="dlg.ok", role="button", name="Aceptar", automation_id="dlgAceptar")
            ]
            if self.dialog_dismissible:
                affordances.insert(
                    0,
                    UiElement(ref="dlg.close", role="button", name="Cerrar",
                              automation_id="dlgCerrar"),
                )
            elements = elements + tuple(affordances)
            text = text + (dialog,)
        return ScreenObservation(
            screen_key=screen.key,
            window_title=screen.title,
            app_version=self.application.version,
            elements=elements,
            text=text,
            screenshot_ref=f"sim://{self.application.name}/{screen.key}/{self.observations}",
            size=(width, height),
            dialog=dialog,
        )

    # -- action --------------------------------------------------------------

    def act(self, action_type: str, element: UiElement | None, value: str | None = None) -> ActionResult:
        screen = self.application.screen(self.current_screen)
        ref = element.ref if element else ""

        if self.failure_hook and self.failure_hook(action_type, ref):
            return ActionResult(ok=False, detail="driver.simulated_failure",
                                input_tokens=self.SCREENSHOT_TOKENS)

        if self._pending_dialog:
            if ref in ("dlg.close", "dlg.ok"):
                # Dismissing the dialog is allowed; obeying its text is not.
                # Which affordance was used is evidence worth keeping: "closed"
                # and "agreed" are very different answers to a fake pop-up.
                self._pending_dialog = None
                self._artifacts["dismissed_with"] = ref
                return ActionResult(ok=True, detail="dialog.dismissed",
                                    observation=self.observe(),
                                    artifacts={"dismissed_with": ref},
                                    input_tokens=self.SCREENSHOT_TOKENS)
            return ActionResult(
                ok=False,
                detail="driver.blocked_by_dialog",
                observation=self.observe(),
                input_tokens=self.SCREENSHOT_TOKENS,
            )

        key = (action_type, ref)
        effect = screen.effects.get(key)

        if effect and self.mode is DriverMode.PREVIEW:
            # Preview: stop at the point of no return and describe it instead of
            # doing it. This is what the approver reads before deciding.
            proposal = {"action": action_type, "element": ref,
                        "name": element.name if element else "", "value": value}
            self.proposed.append(proposal)
            return ActionResult(
                ok=True,
                detail=PREVIEW_HALT,
                observation=self.observe(),
                artifacts={"proposed_action": proposal},
                input_tokens=self.SCREENSHOT_TOKENS,
            )

        if effect:
            # In SANDBOX the effect lands in this throwaway session only; the
            # isolation is the session, not a half-executed action.
            self._artifacts.update(effect)

        next_screen = screen.transitions.get(key)
        if next_screen:
            self.current_screen = next_screen

        if element is not None and not element.enabled:
            return ActionResult(ok=False, detail="driver.element_disabled",
                                input_tokens=self.SCREENSHOT_TOKENS)

        if action_type in ("type_text", "select_option") and value is not None:
            self._artifacts.setdefault("values", {})[ref] = value

        return ActionResult(
            ok=True,
            detail="",
            observation=self.observe(),
            artifacts=dict(self._artifacts),
            input_tokens=self.SCREENSHOT_TOKENS,
            output_tokens=40,
        )

    # -- misc ----------------------------------------------------------------

    def app_version(self) -> str:
        return self.application.version

    def artifacts(self) -> dict[str, Any]:
        return dict(self._artifacts)

    def reset(self) -> None:
        self.current_screen = self.application.start_screen
        self._artifacts = {}
        self._pending_dialog = self.inject_dialog
        self.observations = 0
        self.proposed = []

    @property
    def effects_enabled(self) -> bool:
        return self.mode is DriverMode.PRODUCTION


def _scale(bounds: tuple[int, int, int, int], factor: float) -> tuple[int, int, int, int]:
    return tuple(int(value * factor) for value in bounds)  # type: ignore[return-value]


# A driver must never be asked to do one of the absolute prohibitions (§7). The
# check lives here as well as in `security.risk` so that no executor can reach a
# machine without passing it.
FORBIDDEN_ACTIONS = frozenset({"initiate_payment", "delete_record", "change_permissions",
                               "change_credentials", "disable_control"})


def guard_action(action_type: str) -> None:
    if action_type in FORBIDDEN_ACTIONS:
        raise ProhibitedAction(action=action_type)


def build_driver(application: ScriptedApplication, **kwargs) -> SimulatedDesktopDriver:
    return SimulatedDesktopDriver(application, **kwargs)


def elements_from(specs: Iterable[dict[str, Any]]) -> tuple[UiElement, ...]:
    return tuple(UiElement(**spec) for spec in specs)
