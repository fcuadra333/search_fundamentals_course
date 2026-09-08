"""Selector cascade (§5).

Every element is identified by several criteria in order of preference. If the
first one fails the next is tried and the degradation is recorded. Three
degradations in a week mark the skill for review; falling all the way to pixel
coordinates marks it fragile.

This is the single largest reliability lever the platform has: the documented
drop from 50% to 19,1% under interface variation is what the cascade exists to
soften.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from ..domain.enums import SelectorTier
from ..domain.errors import SelectorNotFound
from .drivers import ScreenObservation, UiElement

# Three degradations inside a week send the skill to review (§5).
DEGRADATION_REVIEW_THRESHOLD = 3


@dataclass(frozen=True)
class Selector:
    """One criterion of the cascade."""

    tier: SelectorTier
    value: str
    role: str | None = None
    anchor_ref: str | None = None
    offset: tuple[int, int] | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Selector":
        offset = raw.get("offset")
        return cls(
            tier=SelectorTier(int(raw["tier"])),
            value=str(raw.get("value", "")),
            role=raw.get("role"),
            anchor_ref=raw.get("anchor_ref"),
            offset=tuple(offset) if offset else None,  # type: ignore[arg-type]
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"tier": int(self.tier), "value": self.value}
        if self.role:
            payload["role"] = self.role
        if self.anchor_ref:
            payload["anchor_ref"] = self.anchor_ref
        if self.offset:
            payload["offset"] = list(self.offset)
        return payload


@dataclass(frozen=True)
class Resolution:
    """Result of walking the cascade."""

    element: UiElement
    tier_used: SelectorTier
    best_tier: SelectorTier
    tried: tuple[SelectorTier, ...]

    @property
    def degraded(self) -> bool:
        return int(self.tier_used) > int(self.best_tier)

    @property
    def marks_fragile(self) -> bool:
        return self.tier_used.marks_skill_fragile


def parse_selectors(raw: Iterable[dict[str, Any]] | None) -> list[Selector]:
    """Parse and sort by preference. A malformed entry is dropped, not guessed."""
    selectors: list[Selector] = []
    for entry in raw or ():
        try:
            selectors.append(Selector.from_dict(entry))
        except (KeyError, ValueError, TypeError):
            continue
    return sorted(selectors, key=lambda selector: int(selector.tier))


def resolve(observation: ScreenObservation, selectors: Sequence[Selector]) -> Resolution:
    """Walk the cascade in order and return the first element that matches.

    Raises `SelectorNotFound` when no criterion matches. Failing cleanly is the
    point: guessing an element is how an agent clicks the wrong button.
    """
    if not selectors:
        raise SelectorNotFound(reason="no_selectors_declared")

    best_tier = selectors[0].tier
    tried: list[SelectorTier] = []
    for selector in selectors:
        tried.append(selector.tier)
        element = _match(observation, selector)
        if element is not None:
            return Resolution(
                element=element,
                tier_used=selector.tier,
                best_tier=best_tier,
                tried=tuple(tried),
            )
    raise SelectorNotFound(
        reason="no_match",
        tiers=", ".join(str(int(tier)) for tier in tried),
    )


def _match(observation: ScreenObservation, selector: Selector) -> UiElement | None:
    if selector.tier is SelectorTier.AUTOMATION_ID:
        return _first(observation, lambda el: el.automation_id == selector.value)

    if selector.tier is SelectorTier.ACCESSIBLE_ROLE_NAME:
        role = (selector.role or "").lower()
        return _first(
            observation,
            lambda el: el.name.strip().lower() == selector.value.strip().lower()
            and (not role or el.role.lower() == role),
        )

    if selector.tier is SelectorTier.VISIBLE_LABEL:
        needle = selector.value.strip().lower()
        return _first(
            observation,
            lambda el: needle in (el.label or "").strip().lower()
            or needle in (el.name or "").strip().lower(),
        )

    if selector.tier is SelectorTier.RELATIVE_TO_ANCHOR:
        anchor = observation.element(selector.anchor_ref or "")
        if anchor is None:
            return None
        dx, dy = selector.offset or (0, 0)
        target = (anchor.bounds[0] + dx, anchor.bounds[1] + dy)
        return _nearest(observation, target, exclude=anchor.ref)

    if selector.tier is SelectorTier.PIXEL_COORDINATES:
        try:
            x_text, _, y_text = selector.value.partition(",")
            point = (int(x_text), int(y_text))
        except ValueError:
            return None
        return _containing(observation, point) or _nearest(observation, point)

    return None


def _first(observation: ScreenObservation, predicate) -> UiElement | None:
    for element in observation.elements:
        if predicate(element):
            return element
    return None


def _containing(observation: ScreenObservation, point: tuple[int, int]) -> UiElement | None:
    x, y = point
    for element in observation.elements:
        left, top, width, height = element.bounds
        if width and height and left <= x <= left + width and top <= y <= top + height:
            return element
    return None


def _nearest(observation: ScreenObservation, point: tuple[int, int], exclude: str = "") -> UiElement | None:
    best: UiElement | None = None
    best_distance = float("inf")
    for element in observation.elements:
        if element.ref == exclude or element.bounds == (0, 0, 0, 0):
            continue
        left, top, width, height = element.bounds
        centre = (left + width / 2, top + height / 2)
        distance = (centre[0] - point[0]) ** 2 + (centre[1] - point[1]) ** 2
        if distance < best_distance:
            best, best_distance = element, distance
    return best
