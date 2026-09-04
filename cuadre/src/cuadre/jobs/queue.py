"""Database-backed job queue.

Reconciliation is night work, so a table is enough and it keeps the deployment
to one artifact: no broker to install on a client's on-premise box. Claiming is
a conditional UPDATE, which is atomic on both PostgreSQL and SQLite, with
`SKIP LOCKED` used where the dialect offers it.
"""

from __future__ import annotations

import datetime as dt
import os
import socket
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..enums import JobStatus
from ..models import Job

BACKOFF_SECONDS = (30, 120, 600)


def worker_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def enqueue(
    session: Session,
    kind: str,
    payload: dict[str, Any],
    *,
    firm_id: int | None = None,
    delay_seconds: int = 0,
    max_attempts: int = 3,
) -> Job:
    job = Job(
        firm_id=firm_id,
        kind=kind,
        payload=payload,
        status=JobStatus.QUEUED,
        max_attempts=max_attempts,
        run_after=_now() + dt.timedelta(seconds=delay_seconds),
    )
    session.add(job)
    session.flush()
    return job


def claim(session: Session, *, worker: str | None = None) -> Job | None:
    """Take the oldest due job, or return None. Safe with several workers."""
    worker = worker or worker_name()
    now = _now()
    statement = (
        select(Job.id)
        .where(Job.status == JobStatus.QUEUED, Job.run_after <= now)
        .order_by(Job.run_after, Job.id)
        .limit(1)
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update(skip_locked=True)

    job_id = session.execute(statement).scalar_one_or_none()
    if job_id is None:
        return None

    # The status check inside the UPDATE is what makes the claim exclusive.
    result = session.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == JobStatus.QUEUED)
        .values(status=JobStatus.RUNNING, locked_at=now, locked_by=worker,
                attempts=Job.attempts + 1)
    )
    session.commit()
    if result.rowcount == 0:
        return None
    return session.get(Job, job_id)


def finish(session: Session, job: Job) -> None:
    job.status = JobStatus.DONE
    job.finished_at = _now()
    job.last_error = None
    session.commit()


def fail(session: Session, job: Job, error: str) -> None:
    """Retry with backoff until the attempts run out, then park it as FAILED so
    somebody can see it rather than have it disappear."""
    job.last_error = error[:4000]
    if job.attempts >= job.max_attempts:
        job.status = JobStatus.FAILED
        job.finished_at = _now()
    else:
        delay = BACKOFF_SECONDS[min(job.attempts - 1, len(BACKOFF_SECONDS) - 1)]
        job.status = JobStatus.QUEUED
        job.run_after = _now() + dt.timedelta(seconds=delay)
        job.locked_at = None
        job.locked_by = None
    session.commit()


def pending_count(session: Session, firm_id: int | None = None) -> int:
    statement = select(Job.id).where(Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
    if firm_id is not None:
        statement = statement.where(Job.firm_id == firm_id)
    return len(list(session.execute(statement).scalars()))
