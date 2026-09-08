"""Preconditions, postconditions and semantic checks.

These expressions come from skill definitions that designers edit, so they are
never evaluated with `eval`. The text is parsed with `ast`, every node type is
checked against a whitelist, and the tree is walked by this module. An
expression can only call the predicates registered below and read variables the
engine put in the context.

Examples of what a designer writes:

    screen_is('erp.main')
    file_exists(output_path) and row_count() > 0
    element_exists('btnExport') and not text_contains('Error')
"""

from __future__ import annotations

import ast
import operator
from dataclasses import dataclass, field
from typing import Any, Callable

MAX_EXPRESSION_LENGTH = 500

_ALLOWED_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.Compare,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
)

_COMPARISONS: dict[type, Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}

_ARITHMETIC: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: lambda a, b: a / b if b else 0,
}


class ConditionError(ValueError):
    """The expression is not evaluable. Certification refuses such a skill."""


@dataclass
class ConditionContext:
    """Everything a condition may look at.

    `screen_text` is present so a condition can *check* for text, never so the
    agent can follow it.
    """

    screen_key: str = ""
    anchor: str = ""
    elements: dict[str, Any] = field(default_factory=dict)
    values: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    variables: dict[str, Any] = field(default_factory=dict)
    screen_text: tuple[str, ...] = ()
    rows: list[dict[str, Any]] = field(default_factory=list)

    def predicates(self) -> dict[str, Callable[..., Any]]:
        return {
            "screen_is": lambda key: self.screen_key == key,
            "anchor_is": lambda key: self.anchor == key,
            "element_exists": lambda ref: ref in self.elements,
            "element_value": lambda ref: self.elements.get(ref, {}).get("value"),
            "element_enabled": lambda ref: bool(self.elements.get(ref, {}).get("enabled")),
            "text_contains": lambda needle: any(
                needle.lower() in line.lower() for line in self.screen_text
            ),
            "file_exists": lambda path: path in self.artifacts.get("files", {}),
            "file_rows": lambda path: len(self.artifacts.get("files", {}).get(path, [])),
            "row_count": lambda: len(self.rows),
            "column_sum": lambda column: sum(
                _number(row.get(column)) for row in self.rows
            ),
            "value_of": lambda name: self.values.get(name),
            "var": lambda name: self.variables.get(name),
            "abs_diff": lambda a, b: abs(_number(a) - _number(b)),
            "is_empty": lambda value: value in (None, "", [], {}),
            "len": lambda value: len(value) if hasattr(value, "__len__") else 0,
        }

    def names(self) -> dict[str, Any]:
        merged: dict[str, Any] = {"true": True, "false": False, "none": None}
        merged.update(self.variables)
        merged.update(self.values)
        return merged


def validate(expression: str) -> None:
    """Raise `ConditionError` unless the expression can be evaluated safely.

    Certification calls this: a postcondition that is not evaluable is the same
    as no postcondition at all (§3).
    """
    if not expression or not expression.strip():
        raise ConditionError("empty expression")
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise ConditionError("expression too long")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        raise ConditionError(f"syntax error: {exc.msg}") from exc

    allowed_calls = set(ConditionContext().predicates())
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ConditionError(f"forbidden expression element: {type(node).__name__}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ConditionError("only direct predicate calls are allowed")
            if node.func.id not in allowed_calls:
                raise ConditionError(f"unknown predicate: {node.func.id}")
            if node.keywords:
                raise ConditionError("predicates take positional arguments only")


def is_evaluable(expression: str | None) -> bool:
    try:
        validate(expression or "")
    except ConditionError:
        return False
    return True


def evaluate(expression: str, context: ConditionContext) -> bool:
    """Evaluate a validated expression against a context."""
    validate(expression)
    tree = ast.parse(expression.strip(), mode="eval")
    return bool(_eval(tree.body, context))


def _eval(node: ast.AST, context: ConditionContext) -> Any:
    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        names = context.names()
        if node.id in names:
            return names[node.id]
        # An unknown name is falsy rather than an error: a condition that refers
        # to a value the run never produced has simply not been met.
        return None

    if isinstance(node, ast.BoolOp):
        results = (_eval(value, context) for value in node.values)
        if isinstance(node.op, ast.And):
            return all(results)
        return any(results)

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval(node.operand, context)

    if isinstance(node, ast.Compare):
        left = _eval(node.left, context)
        for op, comparator in zip(node.ops, node.comparators):
            right = _eval(comparator, context)
            compare = _COMPARISONS.get(type(op))
            if compare is None:
                raise ConditionError(f"forbidden comparison: {type(op).__name__}")
            if not compare(left, right):
                return False
            left = right
        return True

    if isinstance(node, ast.BinOp):
        apply = _ARITHMETIC.get(type(node.op))
        if apply is None:
            raise ConditionError(f"forbidden operator: {type(node.op).__name__}")
        return apply(_eval(node.left, context), _eval(node.right, context))

    if isinstance(node, ast.Call):
        predicate = context.predicates().get(node.func.id)  # type: ignore[union-attr]
        if predicate is None:
            raise ConditionError(f"unknown predicate: {ast.dump(node.func)}")
        args = [_eval(arg, context) for arg in node.args]
        try:
            return predicate(*args)
        except TypeError as exc:
            raise ConditionError(f"bad arguments for predicate: {exc}") from exc

    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(element, context) for element in node.elts]

    raise ConditionError(f"forbidden expression element: {type(node).__name__}")


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def context_from_observation(observation, *, anchor: str = "", artifacts: dict | None = None,
                             variables: dict | None = None, rows: list | None = None) -> ConditionContext:
    """Build a context out of a screen observation."""
    elements = {
        element.ref: {
            "role": element.role,
            "name": element.name,
            "value": element.value,
            "enabled": element.enabled,
        }
        for element in observation.elements
    }
    return ConditionContext(
        screen_key=observation.screen_key,
        anchor=anchor,
        elements=elements,
        artifacts=artifacts or {},
        variables=variables or {},
        screen_text=observation.text,
        rows=rows or [],
    )
