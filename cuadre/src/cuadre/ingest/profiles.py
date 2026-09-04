"""Format profiles: which column means what.

The mapping is proposed by scoring header names and column content, shown to
the user, confirmed once, and then reused for every later upload of the same
bank and client (ESPECIFICACION 3, stage 1).

Column detection is deterministic here. A model may be asked for a second
opinion on a file this scoring cannot read, but it never decides alone and its
answer still goes to the user for confirmation.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from ..enums import UploadKind
from ..money import AmountParseError, parse_amount
from .normalize import (
    DateParseError,
    extract_reference,
    infer_date_format,
    normalize_description,
    parse_date,
)
from .readers import RawTable

# Header synonyms, accent-free and upper case, ordered strongest first.
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "date": ("FECHA", "FECHA VALOR", "FECHA OPERACION", "FEC", "DATE", "F. MOV", "FECHA MOV"),
    "posted_date": ("FECHA CONTABLE", "FECHA REGISTRO", "POSTED", "FECHA APLICACION"),
    "amount": ("MONTO", "IMPORTE", "VALOR", "AMOUNT", "MONTO MOVIMIENTO", "CARGO/ABONO"),
    "debit": ("DEBITO", "DEBE", "CARGO", "CARGOS", "RETIRO", "RETIROS", "DEBIT", "SALIDA"),
    "credit": ("CREDITO", "HABER", "ABONO", "ABONOS", "DEPOSITO", "DEPOSITOS", "CREDIT", "ENTRADA"),
    "description": (
        "DESCRIPCION", "CONCEPTO", "DETALLE", "GLOSA", "REFERENCIA DESCRIPCION",
        "DESCRIPTION", "NARRATIVA", "TRANSACCION", "MOVIMIENTO", "OBSERVACION",
    ),
    "reference": (
        "REFERENCIA", "REF", "NUMERO", "NRO", "NO DOCUMENTO", "DOCUMENTO", "COMPROBANTE",
        "CHEQUE", "NO CHEQUE", "REFERENCE", "VOUCHER", "ASIENTO",
    ),
    "balance": ("SALDO", "BALANCE", "SALDO CONTABLE", "SALDO DISPONIBLE", "SALDO FINAL"),
    "gl_account": ("CUENTA", "CUENTA CONTABLE", "CODIGO CUENTA", "ACCOUNT", "CTA"),
    "document_type": ("TIPO", "TIPO DOCUMENTO", "TIPO ASIENTO", "CLASE"),
    "third_party": ("TERCERO", "BENEFICIARIO", "PROVEEDOR", "CLIENTE", "NOMBRE", "PAYEE"),
}

_BANK_ROLES = ("date", "posted_date", "amount", "debit", "credit", "description", "reference", "balance")
_LEDGER_ROLES = _BANK_ROLES + ("gl_account", "document_type", "third_party")


@dataclass
class ProposedMapping:
    mapping: dict[str, str]
    confidence: dict[str, float]
    sign_convention: str
    date_format: str | None
    warnings: list[str] = field(default_factory=list)

    @property
    def is_usable(self) -> bool:
        has_date = "date" in self.mapping
        has_amount = "amount" in self.mapping or bool({"debit", "credit"} & set(self.mapping))
        return has_date and has_amount

    @property
    def overall_confidence(self) -> float:
        keys = [k for k in ("date", "amount", "debit", "credit") if k in self.confidence]
        if not keys:
            return 0.0
        return round(sum(self.confidence[k] for k in keys) / len(keys), 3)


@dataclass
class NormalizedLine:
    row_number: int
    value_date: dt.date
    amount_cents: int
    raw_description: str
    normalized_description: str
    reference: str | None
    balance_cents: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    raw_row: dict[str, Any] = field(default_factory=dict)


@dataclass
class Rejection:
    row_number: int
    reason: str
    raw_row: dict[str, Any]


@dataclass
class ApplyResult:
    lines: list[NormalizedLine]
    rejects: list[Rejection]
    opening_balance_cents: int | None = None
    closing_balance_cents: int | None = None

    @property
    def total_cents(self) -> int:
        return sum(line.amount_cents for line in self.lines)


def _norm_header(header: str) -> str:
    from .normalize import _strip_accents  # local: shares the accent table

    return " ".join(_strip_accents(header).upper().replace(".", " ").split())


def _header_score(header: str, role: str) -> float:
    normalized = _norm_header(header)
    if not normalized:
        return 0.0
    best = 0.0
    for rank, synonym in enumerate(_SYNONYMS[role]):
        weight = 1.0 - rank * 0.03
        if normalized == synonym:
            best = max(best, weight)
        elif normalized.startswith(synonym) or normalized.endswith(synonym):
            best = max(best, weight * 0.85)
        elif synonym in normalized:
            best = max(best, weight * 0.7)
    return round(best, 3)


def _fraction_parseable(values: list[Any], parser) -> float | None:
    """Share of non-empty cells the parser accepts, or None when the column is
    entirely empty.

    None means "no evidence", not "bad column": a statement with no credits in
    the period has an empty Créditos column, and penalising it as unparseable
    would drop half the debit/credit pair and invert every sign.
    """
    considered = [v for v in values if v is not None and str(v).strip()][:80]
    if not considered:
        return None
    ok = 0
    for value in considered:
        try:
            parser(value)
            ok += 1
        except (ValueError, TypeError):
            continue
    return round(ok / len(considered), 3)


def detect_mapping(table: RawTable, kind: UploadKind, currency: str = "USD") -> ProposedMapping:
    """Score every column against every role and keep the best assignment."""
    roles = _LEDGER_ROLES if kind in (UploadKind.LEDGER, UploadKind.SUBLEDGER) else _BANK_ROLES
    warnings: list[str] = []

    date_fraction: dict[str, float | None] = {}
    amount_fraction: dict[str, float | None] = {}
    for header in table.headers:
        values = table.column(header)
        date_fraction[header] = _fraction_parseable(values, parse_date)
        amount_fraction[header] = _fraction_parseable(values, lambda v: parse_amount(v, currency))

    scores: dict[tuple[str, str], float] = {}
    for role in roles:
        for header in table.headers:
            score = _header_score(header, role)
            dates, amounts = date_fraction[header], amount_fraction[header]
            if role in ("date", "posted_date"):
                if dates is None:
                    score *= 0.8  # empty column: judge it by its header alone
                else:
                    score = score * 0.6 + dates * 0.4
                    if dates < 0.5:
                        score *= 0.2  # what does not parse as a date is not one
            elif role in ("amount", "debit", "credit", "balance"):
                if amounts is None:
                    score *= 0.8
                else:
                    score = score * 0.65 + amounts * 0.35
                    if amounts < 0.5:
                        score *= 0.2
            elif role in ("description", "third_party"):
                textish = 1.0 - max(dates or 0.0, amounts or 0.0)
                score = score * 0.8 + textish * 0.2
            scores[(role, header)] = round(score, 4)

    mapping: dict[str, str] = {}
    confidence: dict[str, float] = {}
    taken: set[str] = set()
    # Greedy over the strongest pairs; roles are few and the ordering is stable.
    for (role, header), score in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0])):
        if score < 0.35 or role in mapping or header in taken:
            continue
        mapping[role] = header
        confidence[role] = score
        taken.add(header)

    sign_convention = "single"
    if "debit" in mapping and "credit" in mapping:
        sign_convention = "debit_credit"
        mapping.pop("amount", None)
        confidence.pop("amount", None)
    elif "amount" not in mapping and ("debit" in mapping or "credit" in mapping):
        # Only one of the two columns was recognised; a single-column export
        # with a header like 'Cargo' is far more likely than half a pair.
        only = "debit" if "debit" in mapping else "credit"
        mapping["amount"] = mapping.pop(only)
        confidence["amount"] = confidence.pop(only)
        warnings.append(
            f"Se interpretó la columna '{mapping['amount']}' como monto con signo. "
            "Verifique el signo antes de confirmar."
        )

    date_format = infer_date_format(table.column(mapping["date"])) if "date" in mapping else None

    if "date" not in mapping:
        warnings.append("No se identificó la columna de fecha.")
    if sign_convention == "single" and "amount" not in mapping:
        warnings.append("No se identificó la columna de monto.")
    if "description" not in mapping:
        warnings.append("Sin columna de descripción: el cruce difuso perderá precisión.")

    return ProposedMapping(
        mapping=mapping,
        confidence=confidence,
        sign_convention=sign_convention,
        date_format=date_format,
        warnings=warnings,
    )


def debit_is_positive(kind: UploadKind) -> bool:
    """Polarity of a debit/credit pair, by side.

    On a bank statement the columns are written from the account holder's point
    of view: a credit puts money in. On the ledger the cash account is an asset:
    a debit puts money in. Both end up in the same sign convention, which is
    what lets the matching stages compare the two sides directly.
    """
    return kind in (UploadKind.LEDGER, UploadKind.SUBLEDGER)


def apply_mapping(
    table: RawTable,
    mapping: dict[str, str],
    *,
    kind: UploadKind,
    currency: str = "USD",
    date_format: str | None = None,
    sign_convention: str = "single",
    invert_sign: bool = False,
) -> ApplyResult:
    """Turn raw rows into normalized lines. Rows that cannot be read are
    rejected with a reason and reported; nothing is silently dropped."""
    lines: list[NormalizedLine] = []
    rejects: list[Rejection] = []
    index = {header: position for position, header in enumerate(table.headers)}

    def cell(row: list[Any], role: str) -> Any:
        header = mapping.get(role)
        if header is None:
            return None
        position = index.get(header)
        if position is None or position >= len(row):
            return None
        return row[position]

    debit_positive = debit_is_positive(kind)

    for offset, row in enumerate(table.rows):
        # The physical line in the file, so a rejection points at the row the
        # user will actually find when they open it.
        row_number = (
            table.row_numbers[offset]
            if offset < len(table.row_numbers)
            else table.header_row + 2 + offset
        )
        raw_row = {h: row[i] if i < len(row) else None for h, i in index.items()}
        try:
            value_date = parse_date(cell(row, "date"), date_format)
        except DateParseError as exc:
            rejects.append(Rejection(row_number, f"Fecha: {exc}", raw_row))
            continue

        try:
            amount_cents = _row_amount(
                cell(row, "amount"),
                cell(row, "debit"),
                cell(row, "credit"),
                currency=currency,
                sign_convention=sign_convention,
                debit_positive=debit_positive,
            )
        except AmountParseError as exc:
            rejects.append(Rejection(row_number, f"Monto: {exc}", raw_row))
            continue

        if amount_cents == 0:
            rejects.append(Rejection(row_number, "Monto cero", raw_row))
            continue

        if invert_sign:
            amount_cents = -amount_cents

        raw_description = str(cell(row, "description") or "").strip()
        balance_cents = None
        balance_raw = cell(row, "balance")
        if balance_raw not in (None, ""):
            try:
                balance_cents = parse_amount(balance_raw, currency)
            except AmountParseError:
                balance_cents = None

        extra = {
            key: str(cell(row, key)).strip()
            for key in ("gl_account", "document_type", "third_party")
            if cell(row, key) not in (None, "")
        }

        lines.append(
            NormalizedLine(
                row_number=row_number,
                value_date=value_date,
                amount_cents=amount_cents,
                raw_description=raw_description,
                normalized_description=normalize_description(
                    " ".join(filter(None, [raw_description, extra.get("third_party", "")]))
                ),
                reference=extract_reference(raw_description, cell(row, "reference")),
                balance_cents=balance_cents,
                extra=extra,
                raw_row=_jsonable(raw_row),
            )
        )

    opening, closing = _derive_balances(lines)
    return ApplyResult(lines=lines, rejects=rejects, opening_balance_cents=opening,
                       closing_balance_cents=closing)


def _row_amount(
    amount: Any,
    debit: Any,
    credit: Any,
    *,
    currency: str,
    sign_convention: str,
    debit_positive: bool,
) -> int:
    if sign_convention == "debit_credit":
        debit_cents = _optional_amount(debit, currency)
        credit_cents = _optional_amount(credit, currency)
        if debit_cents == 0 and credit_cents == 0:
            raise AmountParseError("débito y crédito vacíos")
        # Either column may already carry a sign; magnitude decides, polarity
        # comes from the column the value sits in.
        signed = abs(debit_cents) - abs(credit_cents)
        return signed if debit_positive else -signed
    if amount is None or str(amount).strip() == "":
        raise AmountParseError("columna de monto vacía")
    return parse_amount(amount, currency)


def _optional_amount(value: Any, currency: str) -> int:
    if value is None or str(value).strip() == "":
        return 0
    try:
        return parse_amount(value, currency)
    except AmountParseError:
        return 0


def _derive_balances(lines: list[NormalizedLine]) -> tuple[int | None, int | None]:
    """When the export carries a running balance, opening and closing come for
    free and the movements can be checked against them."""
    with_balance = [line for line in lines if line.balance_cents is not None]
    if len(with_balance) < 2:
        return None, None
    first, last = with_balance[0], with_balance[-1]
    return first.balance_cents - first.amount_cents, last.balance_cents


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, (dt.date, dt.datetime)):
            out[key] = value.isoformat()
        elif value is None or isinstance(value, (str, int, float, bool)):
            out[key] = value
        else:
            out[key] = str(value)
    return out
