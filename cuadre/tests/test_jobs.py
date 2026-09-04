"""The database-backed queue that runs the night work."""

from __future__ import annotations

import datetime as dt

from cuadre.enums import JobStatus
from cuadre.jobs import queue, tasks, worker
from cuadre.models import Job


def test_a_job_is_claimed_once(session, firm):
    queue.enqueue(session, "run_reconciliation", {"reconciliation_id": 1}, firm_id=firm.id)
    session.commit()
    first = queue.claim(session, worker="a")
    assert first is not None and first.status is JobStatus.RUNNING
    assert queue.claim(session, worker="b") is None


def test_a_job_scheduled_for_later_waits(session, firm):
    queue.enqueue(session, "x", {}, firm_id=firm.id, delay_seconds=3600)
    session.commit()
    assert queue.claim(session) is None


def test_failure_retries_with_backoff_then_parks(session, firm):
    queue.enqueue(session, "x", {}, firm_id=firm.id, max_attempts=2)
    session.commit()

    job = queue.claim(session)
    queue.fail(session, job, "primer error")
    assert job.status is JobStatus.QUEUED
    assert job.run_after > dt.datetime.now(dt.UTC).replace(tzinfo=None)

    job.run_after = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    session.commit()
    job = queue.claim(session)
    queue.fail(session, job, "segundo error")
    assert job.status is JobStatus.FAILED
    assert job.last_error == "segundo error"
    assert job.finished_at is not None


def test_the_worker_runs_a_real_reconciliation(session, tenant, loaded, users):
    from cuadre.enums import ReconciliationStatus, Role

    queue.enqueue(
        session, "run_reconciliation",
        {"reconciliation_id": loaded.id, "user_id": users[Role.PREPARER].id},
        firm_id=loaded.firm_id,
    )
    session.commit()
    assert worker.run_forever(poll_seconds=0, max_jobs=1) == 1

    session.expire_all()
    job = session.query(Job).one()
    assert job.status is JobStatus.DONE
    assert loaded.status is ReconciliationStatus.IN_REVIEW
    assert loaded.balanced


def test_an_unknown_task_fails_loudly(session, firm):
    queue.enqueue(session, "tarea-que-no-existe", {}, firm_id=firm.id, max_attempts=1)
    session.commit()
    worker.run_forever(poll_seconds=0, max_jobs=1)
    session.expire_all()
    job = session.query(Job).one()
    assert job.status is JobStatus.FAILED
    assert "Tarea desconocida" in job.last_error


def test_the_registry_lists_what_the_worker_can_do():
    assert "run_reconciliation" in tasks.registered()
    assert "export_workpapers" in tasks.registered()
