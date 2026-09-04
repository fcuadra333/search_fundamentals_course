"""Upload workflow: file in, normalized lines out.

    stage_upload   store the bytes, detect a re-upload, look for a confirmed
                   profile for this bank and client
    propose        read the file and score its columns for the mapping screen
    import_upload  apply a mapping, write the lines, refresh the balances

A closed period rejects all of it (invariant 2).
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..enums import (
    LineState,
    ReconciliationStatus,
    UploadKind,
    UploadStatus,
)
from ..models import (
    Account,
    BankMovement,
    Entity,
    FormatProfile,
    LedgerEntry,
    Reconciliation,
    Upload,
)
from ..tenancy import Tenant, audit, require_writable
from .profiles import ApplyResult, ProposedMapping, apply_mapping, detect_mapping
from .readers import RawTable, read_table
from .storage import BlobStore, content_hash, default_store


class DuplicateUploadError(ValueError):
    """The same bytes were already imported into this reconciliation."""


def source_key(account: Account, kind: UploadKind, client_id: int) -> str:
    """Identity of a format: one bank export layout, for one client."""
    origin = (account.bank_name or account.gl_code or account.name or "").strip().upper()
    return f"{kind.value}|{origin}|{client_id}"


def find_profile(
    session: Session, tenant: Tenant, key: str, client_id: int
) -> FormatProfile | None:
    """The client's own confirmed profile wins; a firm-wide one for the same
    bank is the fallback, which is what makes the second client of the same
    bank a one-click upload."""
    stmt = (
        select(FormatProfile)
        .where(
            FormatProfile.firm_id == tenant.firm_id,
            FormatProfile.source_key == key,
            FormatProfile.confirmed_at.is_not(None),
        )
        .order_by(FormatProfile.client_id.is_(None), FormatProfile.version.desc())
    )
    for profile in session.execute(stmt).scalars():
        if profile.client_id in (None, client_id):
            return profile
    return None


def stage_upload(
    session: Session,
    tenant: Tenant,
    reconciliation: Reconciliation,
    *,
    kind: UploadKind,
    filename: str,
    data: bytes,
    store: BlobStore | None = None,
) -> tuple[Upload, FormatProfile | None]:
    require_writable(session, reconciliation)
    store = store or default_store()
    digest = content_hash(data)

    existing = session.execute(
        select(Upload).where(
            Upload.firm_id == tenant.firm_id,
            Upload.reconciliation_id == reconciliation.id,
            Upload.content_hash == digest,
            Upload.status == UploadStatus.IMPORTED,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise DuplicateUploadError(
            f"Este archivo ya fue importado el {existing.created_at:%d/%m/%Y} "
            f"como «{existing.filename}»."
        )

    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    _, blob_path = store.put(data, suffix=suffix)

    upload = Upload(
        firm_id=tenant.firm_id,
        reconciliation_id=reconciliation.id,
        kind=kind,
        filename=filename,
        content_hash=digest,
        blob_path=blob_path,
        byte_size=len(data),
        status=UploadStatus.AWAITING_PROFILE,
        uploaded_by_id=tenant.user_id,
    )
    session.add(upload)
    session.flush()

    account = session.get(Account, reconciliation.bank_account_id)
    entity = session.get(Entity, account.entity_id) if account else None
    profile = None
    if account is not None and entity is not None:
        key = source_key(account, kind, entity.client_id)
        profile = find_profile(session, tenant, key, entity.client_id)

    audit(
        session,
        tenant,
        "upload.staged",
        object_type="Upload",
        object_id=upload.id,
        detail={"archivo": filename, "hash": digest, "perfil": profile.id if profile else None},
    )
    return upload, profile


def read_upload(session: Session, upload: Upload, *, store: BlobStore | None = None,
                header_row: int | None = None) -> RawTable:
    store = store or default_store()
    return read_table(store.get(upload.blob_path), upload.filename, header_row=header_row)


def propose(
    session: Session, upload: Upload, *, currency: str = "USD", store: BlobStore | None = None
) -> tuple[RawTable, ProposedMapping]:
    table = read_upload(session, upload, store=store)
    return table, detect_mapping(table, UploadKind(upload.kind), currency=currency)


def save_profile(
    session: Session,
    tenant: Tenant,
    upload: Upload,
    *,
    name: str,
    mapping: dict[str, str],
    header_row: int,
    date_format: str | None,
    sign_convention: str,
    invert_sign: bool = False,
    client_id: int | None = None,
    account: Account | None = None,
) -> FormatProfile:
    """Confirm a mapping once; every later upload of this bank skips the screen."""
    key = source_key(account, UploadKind(upload.kind), client_id or 0) if account else upload.filename
    profile = FormatProfile(
        firm_id=tenant.firm_id,
        client_id=client_id,
        name=name,
        source_key=key,
        kind=UploadKind(upload.kind),
        mapping=mapping,
        header_row=header_row,
        date_format=date_format,
        sign_convention=sign_convention,
        invert_sign=invert_sign,
        confirmed_by_id=tenant.user_id,
        confirmed_at=dt.datetime.now(dt.UTC).replace(tzinfo=None),
    )
    session.add(profile)
    session.flush()
    if account is not None and UploadKind(upload.kind) is UploadKind.BANK_STATEMENT:
        account.format_profile_id = profile.id
    audit(session, tenant, "profile.confirmed", object_type="FormatProfile",
          object_id=profile.id, detail={"mapeo": mapping, "origen": key})
    return profile


def import_upload(
    session: Session,
    tenant: Tenant,
    upload: Upload,
    *,
    profile: FormatProfile | None = None,
    mapping: dict[str, str] | None = None,
    header_row: int | None = None,
    date_format: str | None = None,
    sign_convention: str = "single",
    invert_sign: bool = False,
    store: BlobStore | None = None,
) -> ApplyResult:
    """Write the normalized lines. Re-importing the same upload replaces its
    own lines and nothing else, so a bad mapping is a fix, not a reset."""
    reconciliation = session.get(Reconciliation, upload.reconciliation_id)
    require_writable(session, reconciliation)

    if profile is not None:
        mapping = dict(profile.mapping)
        header_row = profile.header_row
        date_format = profile.date_format
        sign_convention = profile.sign_convention
        invert_sign = profile.invert_sign
    if not mapping:
        raise ValueError("No hay mapeo de columnas para aplicar.")

    kind = UploadKind(upload.kind)
    currency = reconciliation.currency
    table = read_upload(session, upload, store=store, header_row=header_row)
    result = apply_mapping(
        table,
        mapping,
        kind=kind,
        currency=currency,
        date_format=date_format,
        sign_convention=sign_convention,
        invert_sign=invert_sign,
    )

    _clear_lines_of(session, upload)
    is_bank = kind is UploadKind.BANK_STATEMENT
    model = BankMovement if is_bank else LedgerEntry
    for line in result.lines:
        row = model(
            firm_id=tenant.firm_id,
            reconciliation_id=reconciliation.id,
            upload_id=upload.id,
            row_number=line.row_number,
            value_date=line.value_date,
            amount_cents=line.amount_cents,
            currency=currency,
            raw_description=line.raw_description,
            normalized_description=line.normalized_description,
            reference=line.reference,
            state=LineState.OPEN,
            raw_row=line.raw_row,
        )
        if not is_bank:
            row.gl_account = line.extra.get("gl_account")
            row.document_type = line.extra.get("document_type")
            row.third_party = line.extra.get("third_party")
        session.add(row)

    upload.status = UploadStatus.IMPORTED
    upload.rows_read = len(table.rows)
    upload.rows_imported = len(result.lines)
    upload.rows_rejected = len(result.rejects)
    upload.rejects = [
        {"fila": r.row_number, "motivo": r.reason, "datos": r.raw_row} for r in result.rejects[:200]
    ]
    if profile is not None:
        upload.format_profile_id = profile.id
        profile.times_used += 1

    _apply_balances(reconciliation, kind, result)
    _refresh_status(session, reconciliation)
    session.flush()

    audit(
        session,
        tenant,
        "upload.imported",
        object_type="Upload",
        object_id=upload.id,
        detail={
            "importadas": upload.rows_imported,
            "rechazadas": upload.rows_rejected,
            "total_movimiento": result.total_cents,
        },
    )
    return result


def _clear_lines_of(session: Session, upload: Upload) -> None:
    session.execute(delete(BankMovement).where(BankMovement.upload_id == upload.id))
    session.execute(delete(LedgerEntry).where(LedgerEntry.upload_id == upload.id))


def _apply_balances(reconciliation: Reconciliation, kind: UploadKind, result: ApplyResult) -> None:
    """A running-balance column gives the opening and closing figures without
    asking. Without one, the user types them on the reconciliation screen."""
    if result.opening_balance_cents is None or result.closing_balance_cents is None:
        return
    if kind is UploadKind.BANK_STATEMENT:
        reconciliation.opening_bank_cents = result.opening_balance_cents
        reconciliation.closing_bank_cents = result.closing_balance_cents
    else:
        reconciliation.opening_ledger_cents = result.opening_balance_cents
        reconciliation.closing_ledger_cents = result.closing_balance_cents


def _refresh_status(session: Session, reconciliation: Reconciliation) -> None:
    has_bank = session.execute(
        select(BankMovement.id).where(BankMovement.reconciliation_id == reconciliation.id).limit(1)
    ).first()
    has_ledger = session.execute(
        select(LedgerEntry.id).where(LedgerEntry.reconciliation_id == reconciliation.id).limit(1)
    ).first()
    if has_bank and has_ledger:
        if reconciliation.status in (ReconciliationStatus.PENDING_DATA, ReconciliationStatus.FAILED):
            reconciliation.status = ReconciliationStatus.READY
    else:
        reconciliation.status = ReconciliationStatus.PENDING_DATA
