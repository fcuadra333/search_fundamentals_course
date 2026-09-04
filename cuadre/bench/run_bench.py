"""Run the regression bench against any provider.

    python bench/run_bench.py                       # offline stub, free
    CUADRE_LLM_PROVIDER=anthropic python bench/run_bench.py
    python bench/run_bench.py --min-accuracy 0.85   # fails the build below that

Three numbers matter, in this order:

  falsos positivos  a wrong match that somebody approves costs the client.
  revisión manual   what the customer actually feels, and the number that must
                    not move between deployments.
  aciertos          how many it got right, for choosing between models.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cuadre.config import ModelSettings, get_settings  # noqa: E402
from cuadre.engine.stage4_model import resolve  # noqa: E402
from cuadre.engine.values import Line  # noqa: E402
from cuadre.enums import Side  # noqa: E402
from cuadre.ingest.normalize import extract_reference, normalize_description  # noqa: E402
from cuadre.llm.pricing import format_micros  # noqa: E402
from cuadre.llm.registry import build_gateway  # noqa: E402

CASES = Path(__file__).parent / "cases"


def load_cases(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _line(payload: dict) -> Line:
    """Build the line exactly as stage 1 would.

    The bench has to feed the engine what production feeds it: a normalized
    description and the reference pulled out of the wording. Skipping that
    would measure a different system from the one that ships.
    """
    description = payload["description"]
    return Line(
        id=payload["id"],
        side=Side(payload["side"]),
        date=dt.date.fromisoformat(payload["date"]),
        amount_cents=payload["amount"],
        description=normalize_description(description),
        reference=extract_reference(description, payload.get("reference")),
    )


def run(cases: list[dict], settings: ModelSettings) -> dict:
    gateway = build_gateway(settings)
    outcome_by_case: dict[str, int | None] = {}

    for case in cases:
        target = _line(case["target"])
        candidates = [_line(payload) for payload in case["candidates"]]
        # One case per call: the bench measures the decision, not the batching.
        result = resolve([target], candidates, gateway=gateway, model_settings=settings)
        chosen = None
        for proposal in result.result.proposals:
            if proposal.entry_ids:
                chosen = proposal.entry_ids[0]
        outcome_by_case[case["id"]] = chosen

    correct = wrong_match = missed = to_queue = correctly_declined = 0
    by_family: Counter[str] = Counter()
    family_total: Counter[str] = Counter()

    for case in cases:
        expected = case["expected_entry_id"]
        chosen = outcome_by_case[case["id"]]
        family_total[case["family"]] += 1
        if chosen is None:
            to_queue += 1
            if expected is None:
                correct += 1
                correctly_declined += 1
                by_family[case["family"]] += 1
            else:
                missed += 1
        elif chosen == expected:
            correct += 1
            by_family[case["family"]] += 1
        else:
            wrong_match += 1

    total = len(cases)
    tokens_in, tokens_out = gateway.total_tokens
    return {
        "total": total,
        "aciertos": correct,
        "exactitud": round(correct / total, 4) if total else 0.0,
        "falsos_positivos": wrong_match,
        "tasa_falsos_positivos": round(wrong_match / total, 4) if total else 0.0,
        "no_encontrados": missed,
        "revision_manual": to_queue,
        "tasa_revision_manual": round(to_queue / total, 4) if total else 0.0,
        "declinados_correctamente": correctly_declined,
        "por_familia": {
            family: f"{by_family[family]}/{family_total[family]}" for family in sorted(family_total)
        },
        "llamadas": len(gateway.calls),
        "tokens_entrada": tokens_in,
        "tokens_salida": tokens_out,
        "costo_micros": gateway.total_cost_micros,
        "proveedor": gateway.provider.name,
        "modelo": gateway.provider.model_for(settings and __import__(
            "cuadre.enums", fromlist=["ModelTier"]).ModelTier.MID),
    }


def report(result: dict) -> None:
    print()
    print(f"Banco de regresión — {result['proveedor']} / {result['modelo']}")
    print("=" * 62)
    print(f"  Casos                       {result['total']:>8}")
    print(f"  Aciertos                    {result['aciertos']:>8}  "
          f"({result['exactitud']:.1%})")
    print(f"  Emparejamientos incorrectos {result['falsos_positivos']:>8}  "
          f"({result['tasa_falsos_positivos']:.1%})  ← el que cuesta el cliente")
    print(f"  No encontrados              {result['no_encontrados']:>8}")
    print(f"  Enviados a revisión manual  {result['revision_manual']:>8}  "
          f"({result['tasa_revision_manual']:.1%})  ← lo que percibe el cliente")
    print(f"  Declinados correctamente    {result['declinados_correctamente']:>8}")
    print("-" * 62)
    for family, score in result["por_familia"].items():
        print(f"  {family:<22} {score:>10}")
    print("-" * 62)
    print(f"  Llamadas {result['llamadas']} · tokens {result['tokens_entrada']}/"
          f"{result['tokens_salida']} · costo {format_micros(result['costo_micros'])}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Corre el banco de regresión")
    parser.add_argument("--cases", default=str(CASES / "synthetic_v1.jsonl"))
    parser.add_argument("--min-accuracy", type=float, default=0.0,
                        help="falla si la exactitud queda por debajo")
    parser.add_argument("--max-false-positive", type=float, default=1.0,
                        help="falla si la tasa de emparejamientos incorrectos la supera")
    parser.add_argument("--json", action="store_true", help="imprime el resultado en JSON")
    args = parser.parse_args()

    cases = load_cases(Path(args.cases))
    result = run(cases, get_settings().models)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        report(result)

    if result["exactitud"] < args.min_accuracy:
        print(f"FALLA: exactitud {result['exactitud']:.1%} < {args.min_accuracy:.1%}")
        return 1
    if result["tasa_falsos_positivos"] > args.max_false_positive:
        print(
            f"FALLA: emparejamientos incorrectos {result['tasa_falsos_positivos']:.1%} "
            f"> {args.max_false_positive:.1%}"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
