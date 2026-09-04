"""Build the regression bench.

The specification asks for 200 exceptions with the correct answer known, run
against any candidate model to choose one, to detect a provider degrading
quietly, and to check that a migration did not cost quality.

These 200 are **synthetic**, generated from the shapes that actually reach
stage 4 in Latin American bank data. They are a floor, not the real thing:
replace them file by file with real anonymised exceptions as clients come in
(the format is one JSON object per line, so a real case is just another line).

    python bench/generate_cases.py
"""

from __future__ import annotations

import datetime as dt
import json
import random
from pathlib import Path

OUT = Path(__file__).parent / "cases" / "synthetic_v1.jsonl"
SEED = 20260104  # fixed: the bench has to be the same set on every machine

PROVIDERS = [
    "DISTRIBUIDORA LA UNION", "FERRETERIA NORTE", "COMERCIAL SUR", "ALMACEN CENTRAL",
    "TRANSPORTES RIVAS", "IMPORTACIONES DEL PACIFICO", "AGROSERVICIOS EL VALLE",
    "SUMINISTROS INDUSTRIALES", "PAPELERIA LA MERCED", "SEGUROS AMERICA",
]
BANK_PREFIXES = ["TRANSFERENCIA ACH", "PAGO ACH", "TRF SINPE", "PAGO EN LINEA", "DEBITO AUTOMATICO"]
LEDGER_PREFIXES = ["Pago a", "Cancelacion factura", "Abono a cuenta", "Pago proveedor"]
CHARGES = [
    "COMISION MANEJO DE CUENTA", "IVA SOBRE COMISION", "COMISION TRANSFERENCIA",
    "CARGO POR CHEQUERA", "COMISION MANTENIMIENTO", "INTERES SOBREGIRO",
    "IMPUESTO A TRANSACCIONES", "PORTES Y TELEGRAMAS",
]


def _line(id_, side, day, cents, description, reference=None):
    return {
        "id": id_,
        "side": side,
        "date": (dt.date(2026, 1, 1) + dt.timedelta(days=day)).isoformat(),
        "amount": cents,
        "description": description,
        "reference": reference,
    }


def _case(rng: random.Random, index: int, family: str) -> dict:
    amount = -rng.randrange(50_000, 8_000_000, 25)
    provider = rng.choice(PROVIDERS)
    day = rng.randrange(1, 26)
    reference = str(rng.randrange(1000, 99999))
    target_id, right_id = 1000 + index, 2000 + index
    candidates: list[dict] = []
    expected: int | None = None

    if family == "referencia":
        # The reference is inside the bank wording and on the ledger document.
        target = _line(target_id, "BANK", day + rng.randint(6, 20), amount,
                       f"{rng.choice(BANK_PREFIXES)} {reference} {provider[:12]}")
        candidates.append(_line(right_id, "LEDGER", day, amount,
                                f"{rng.choice(LEDGER_PREFIXES)} {provider}", f"CK-{reference}"))
        expected = right_id

    elif family == "desfase":
        # Same amount, same counterparty, far apart in time.
        target = _line(target_id, "BANK", day + rng.randint(8, 25), amount,
                       f"{rng.choice(BANK_PREFIXES)} {provider}")
        candidates.append(_line(right_id, "LEDGER", day, amount,
                                f"{rng.choice(LEDGER_PREFIXES)} {provider}"))
        expected = right_id

    elif family == "senuelo":
        # The right one and a decoy of the same amount; only the reference tells
        # them apart. This is the family a careless matcher gets wrong.
        target = _line(target_id, "BANK", day + rng.randint(3, 12), amount,
                       f"{rng.choice(BANK_PREFIXES)} {reference} {provider[:12]}")
        candidates.append(_line(right_id, "LEDGER", day, amount,
                                f"{rng.choice(LEDGER_PREFIXES)} {provider}", f"CK-{reference}"))
        decoy = rng.choice([p for p in PROVIDERS if p != provider])
        candidates.append(_line(right_id + 500, "LEDGER", day + 1, amount,
                                f"{rng.choice(LEDGER_PREFIXES)} {decoy}",
                                f"CK-{rng.randrange(1000, 99999)}"))
        expected = right_id

    elif family == "sin_contraparte":
        # A bank charge the books never recorded: there is nothing to match.
        target = _line(target_id, "BANK", day, -rng.randrange(2_000, 90_000),
                       rng.choice(CHARGES))
        for offset in range(rng.randint(1, 3)):
            candidates.append(
                _line(right_id + offset, "LEDGER", day + offset,
                      int(target["amount"] * rng.uniform(0.55, 0.95)),
                      f"{rng.choice(LEDGER_PREFIXES)} {rng.choice(PROVIDERS)}")
            )
        expected = None

    elif family == "ambiguo":
        # Two identical candidates and nothing to separate them. The right
        # answer is the queue, not a coin flip.
        target = _line(target_id, "BANK", day + 2, amount, "PAGO EN LINEA")
        for offset in range(2):
            candidates.append(_line(right_id + offset, "LEDGER", day + offset, amount,
                                    f"{rng.choice(LEDGER_PREFIXES)} {rng.choice(PROVIDERS)}"))
        expected = None

    else:  # "parcial": the amounts are close but not equal, so it is not a match
        target = _line(target_id, "BANK", day, amount,
                       f"{rng.choice(BANK_PREFIXES)} {provider}")
        candidates.append(_line(right_id, "LEDGER", day, amount + rng.randrange(25, 5000),
                                f"{rng.choice(LEDGER_PREFIXES)} {provider}"))
        expected = None

    rng.shuffle(candidates)
    return {
        "id": f"{family[:3]}-{index:03d}",
        "family": family,
        "target": target,
        "candidates": candidates,
        "expected_entry_id": expected,
    }


def build() -> list[dict]:
    rng = random.Random(SEED)
    families = (
        ["referencia"] * 50 + ["desfase"] * 45 + ["senuelo"] * 35
        + ["sin_contraparte"] * 35 + ["ambiguo"] * 20 + ["parcial"] * 15
    )
    rng.shuffle(families)
    return [_case(rng, index, family) for index, family in enumerate(families, start=1)]


def main() -> int:
    cases = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False) + "\n")
    matches = sum(1 for case in cases if case["expected_entry_id"])
    print(f"{len(cases)} casos escritos en {OUT}")
    print(f"  con contrapartida: {matches}   ·   sin contrapartida: {len(cases) - matches}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
