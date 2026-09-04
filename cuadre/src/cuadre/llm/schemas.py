"""Strict output schema for stage 4.

The wire contract is English, like the rest of the code; the values the model
writes (the one-sentence rationale) are Spanish, because an accountant reads
them in the exception queue and in the workpaper.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class Decision(BaseModel):
    """What the model concluded about one exception."""

    exception_id: int = Field(description="Identificador de la excepción evaluada")
    outcome: Literal["match", "no_match"] = Field(
        description="'match' si encontró la contrapartida, 'no_match' si ninguna corresponde"
    )
    movement_ids: list[int] = Field(
        default_factory=list, description="Identificadores de movimientos del banco"
    )
    entry_ids: list[int] = Field(
        default_factory=list, description="Identificadores de asientos del libro"
    )
    confidence: float = Field(ge=0.0, le=1.0, description="Confianza entre 0 y 1")
    rationale: str = Field(
        max_length=240, description="Una sola frase, en español, explicando la decisión"
    )

    @field_validator("rationale")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("la justificación no puede estar vacía")
        return value.strip()

    @model_validator(mode="after")
    def _ids_consistent(self) -> Decision:
        if self.outcome == "match" and not (self.movement_ids and self.entry_ids):
            raise ValueError("un 'match' exige al menos un movimiento y un asiento")
        if self.outcome == "no_match" and (self.movement_ids or self.entry_ids):
            raise ValueError("un 'no_match' no lleva identificadores")
        return self


class Stage4Output(BaseModel):
    decisions: list[Decision] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_duplicates(self) -> Stage4Output:
        seen = [d.exception_id for d in self.decisions]
        if len(seen) != len(set(seen)):
            raise ValueError("hay más de una decisión para la misma excepción")
        return self


def stage4_json_schema() -> dict[str, Any]:
    """JSON Schema handed to the provider, tightened for strict validation."""
    schema = Stage4Output.model_json_schema()
    _harden(schema)
    return schema


def _harden(node: Any) -> None:
    """Forbid extra properties and require every declared field, recursively.

    Providers that enforce a schema need both; providers that do not still get a
    clearer contract, and our own validator rejects the difference either way.
    """
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            node["additionalProperties"] = False
            node["required"] = sorted(node["properties"].keys())
        for value in node.values():
            _harden(value)
    elif isinstance(node, list):
        for item in node:
            _harden(item)
