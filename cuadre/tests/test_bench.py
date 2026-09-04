"""The regression bench is part of the build, not an optional extra.

These tests pin the offline baseline. If a change to normalization, candidate
retrieval or the control provider moves these numbers, it shows up here before
it shows up in a client's month.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bench"))

import generate_cases  # noqa: E402
from run_bench import load_cases, run  # noqa: E402

CASES = ROOT / "bench" / "cases" / "synthetic_v1.jsonl"


def test_the_bench_has_the_two_hundred_cases_the_specification_asks_for():
    cases = load_cases(CASES)
    assert len(cases) == 200
    with_counterpart = [case for case in cases if case["expected_entry_id"]]
    # Both outcomes are represented: a bench of only matches would reward
    # guessing.
    assert 100 <= len(with_counterpart) <= 160
    assert len(cases) - len(with_counterpart) >= 40


def test_the_cases_regenerate_identically():
    """A fixed seed, so two machines measure the same thing."""
    assert generate_cases.build() == generate_cases.build()


@pytest.mark.parametrize("_", [0])
def test_the_offline_baseline_holds(settings, _):
    from cuadre.config import ModelSettings

    result = run(load_cases(CASES), ModelSettings(provider="stub"))
    # A wrong match that somebody approves costs the client. Zero, always.
    assert result["falsos_positivos"] == 0
    assert result["exactitud"] >= 0.88
    # And it declines every genuinely ambiguous case instead of guessing.
    assert result["por_familia"]["ambiguo"] == "20/20"
    assert result["por_familia"]["sin_contraparte"] == "35/35"
    assert result["por_familia"]["referencia"] == "50/50"
