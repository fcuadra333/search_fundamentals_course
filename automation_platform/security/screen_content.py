"""Screen content is data, never instruction (rule 4, §7).

Interface agents are vulnerable to deception through fake pop-up windows, and
that vulnerability is a documented open problem. The platform's answer is
structural rather than clever: text read off a screen never reaches a decision
path. It is wrapped, classified and reported.

What this module does:

- `quarantine()` wraps observed text so that anything downstream that prints or
  stores it carries the "this is content, not an order" marker with it.
- `detect_instruction_attempt()` flags text that reads like a command. A hit is
  never a reason to obey and never a reason to improvise: it is recorded as an
  intervention and, in a strict skill, stops the run.

There is deliberately no function here that turns screen text into an action.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Two severities, because they call for two different answers.
#
# `NOTED`: the text reads like an instruction. Plenty of legitimate screens say
# "click Export", so this is recorded and reported, and the run continues.
#
# `HIGH`: the text is trying to redirect the agent itself, or fishing for
# credentials. There is no benign reading of that on a target application's
# screen, so the run stops and escalates.
_NOTED_PATTERNS = (
    r"\b(?:please\s+)?(?:click|press|open|download|install|enable|disable|delete|send|approve|confirm)\b",
    r"\b(?:por\s+favor\s+)?(?:hac[eé]|haga|presion[aá]|abr[íi]|abra|descarg[aá]|instal[aá]|"
    r"activ[aá]|desactiv[aá]|borr[aá]|envi[aá]|aprob[aá]|confirm[aá])\b",
)

_HIGH_PATTERNS = (
    r"\b(?:ignore|disregard|override|forget)\b.{0,40}\b(?:previous|prior|above|instruction|instructions)\b",
    r"\b(?:ignor[aá]|desestim[aá]|olvid[aá])\b.{0,40}\b(?:instrucci[oó]n|instrucciones|anterior|anteriores)\b",
    # A role marker at the start of a line ("System:", "Asistente:") is how an
    # injected instruction impersonates the conversation. Anchored to the line
    # start on purpose: "Aviso del sistema: ..." is an ordinary app message.
    r"^\s*(?:system|assistant|agent|ai|sistema|asistente|agente)\s*[:>]",
    r"\b(?:enter|provide|introduc[íie]|ingres[aáe])\w*\b.{0,30}\b(?:password|credential|token|contrase[nñ]a|clave)\b",
    r"\b(?:password|credential|token|contrase[nñ]a)\b.{0,20}\b(?:required|needed|requerid|necesari)",
)

_NOTED_COMPILED = tuple(re.compile(pattern, re.IGNORECASE) for pattern in _NOTED_PATTERNS)
_HIGH_COMPILED = tuple(re.compile(pattern, re.IGNORECASE) for pattern in _HIGH_PATTERNS)

SEVERITY_NONE = "none"
SEVERITY_NOTED = "noted"
SEVERITY_HIGH = "high"

QUARANTINE_OPEN = "<<screen-content>>"
QUARANTINE_CLOSE = "<</screen-content>>"


@dataclass(frozen=True)
class QuarantinedText:
    """Screen text, marked as data for everything downstream."""

    lines: tuple[str, ...]
    suspicious: bool = False
    matches: tuple[str, ...] = ()
    severity: str = SEVERITY_NONE

    @property
    def hostile(self) -> bool:
        """True when the text is trying to steer the agent, not just describe."""
        return self.severity == SEVERITY_HIGH

    def render(self) -> str:
        body = "\n".join(self.lines)
        return f"{QUARANTINE_OPEN}\n{body}\n{QUARANTINE_CLOSE}"

    def to_dict(self) -> dict:
        return {
            "lines": list(self.lines),
            "suspicious": self.suspicious,
            "severity": self.severity,
            "matches": list(self.matches),
            "handling": "data_only",
        }


@dataclass
class ScreenContentReport:
    """What the run tells the operator about what it read."""

    quarantined: QuarantinedText
    intervention_needed: bool = False
    notes: list[str] = field(default_factory=list)


def classify(lines: tuple[str, ...] | list[str]) -> tuple[str, tuple[str, ...]]:
    """Return the severity of what a screen says and the lines that caused it."""
    matches: list[str] = []
    severity = SEVERITY_NONE
    for line in lines:
        if any(pattern.search(line) for pattern in _HIGH_COMPILED):
            matches.append(line.strip()[:160])
            severity = SEVERITY_HIGH
            continue
        if any(pattern.search(line) for pattern in _NOTED_COMPILED):
            matches.append(line.strip()[:160])
            if severity == SEVERITY_NONE:
                severity = SEVERITY_NOTED
    return severity, tuple(matches)


def detect_instruction_attempt(lines: tuple[str, ...] | list[str]) -> tuple[bool, tuple[str, ...]]:
    severity, matches = classify(lines)
    return severity != SEVERITY_NONE, matches


def quarantine(lines: tuple[str, ...] | list[str]) -> QuarantinedText:
    severity, matches = classify(lines)
    return QuarantinedText(
        tuple(lines),
        suspicious=severity != SEVERITY_NONE,
        matches=matches,
        severity=severity,
    )


def inspect(observation, *, strict: bool = True) -> ScreenContentReport:
    """Classify what a screen shows.

    A screen that merely uses imperative labels is noted and the run continues;
    stopping there would make the platform unusable, since real applications say
    "Click Export" all the time. Text that tries to steer the agent stops the
    run in a strict skill, because there is no benign reading of it.
    """
    text = tuple(observation.text)
    quarantined = quarantine(text)
    report = ScreenContentReport(quarantined=quarantined)
    if quarantined.suspicious:
        report.notes.append("execution.suspicious_content")
        report.intervention_needed = strict and quarantined.hostile
    if getattr(observation, "dialog", None):
        report.notes.append("enum.InterventionReason.unexpected_dialog")
    return report
