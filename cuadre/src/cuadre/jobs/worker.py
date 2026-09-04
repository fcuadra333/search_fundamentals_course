"""The worker loop.

Run alongside the web process:

    python -m cuadre.jobs.worker

One process is enough for a firm of eighty clients; several may run against the
same database because claiming a job is atomic.
"""

from __future__ import annotations

import logging
import signal
import time

from ..db import get_session_factory
from . import queue, tasks

logger = logging.getLogger("cuadre.worker")
_stop = False


def _handle_signal(signum, _frame) -> None:  # pragma: no cover - signal path
    global _stop
    _stop = True
    logger.info("Señal %s recibida: terminando al finalizar el trabajo en curso.", signum)


def run_forever(poll_seconds: float = 2.0, max_jobs: int | None = None) -> int:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    factory = get_session_factory()
    processed = 0
    while not _stop and (max_jobs is None or processed < max_jobs):
        session = factory()
        try:
            job = queue.claim(session)
            if job is None:
                session.close()
                if max_jobs is not None:
                    break
                time.sleep(poll_seconds)
                continue
            logger.info("Trabajo %s (%s) tomado", job.id, job.kind)
            try:
                result = tasks.get_handler(job.kind)(session, job.payload or {})
                queue.finish(session, job)
                logger.info("Trabajo %s terminado: %s", job.id, result)
            except Exception as exc:
                session.rollback()
                queue.fail(session, job, f"{type(exc).__name__}: {exc}")
                logger.exception("Trabajo %s falló", job.id)
            processed += 1
        finally:
            session.close()
    return processed


def main() -> None:  # pragma: no cover - entry point
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    logger.info("Worker de Cuadre iniciado. Tareas: %s", ", ".join(tasks.registered()))
    run_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
