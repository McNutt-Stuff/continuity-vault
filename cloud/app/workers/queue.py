"""
Activity-queue drainer.

A daemon thread retries queued deliveries whose backoff has elapsed
(:func:`app.queue_registry.run_due`). Runs on every node (each node drains its
own local queue), so an appliance or storage backend that comes back online has
its pending backups delivered automatically.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger("cv.queue_worker")

_thread: threading.Thread | None = None
_TICK_SECONDS = 60


def start_queue_worker() -> None:
    global _thread
    if _thread is not None:
        return

    def loop() -> None:
        time.sleep(30)  # let startup settle
        from ..db import WorkerSessionLocal as SessionLocal
        from ..queue_registry import run_due
        while True:
            try:
                with SessionLocal() as db:
                    run_due(db)
            except Exception:  # noqa: BLE001 — never let the worker die
                logger.exception("queue drain failed")
            time.sleep(_TICK_SECONDS)

    _thread = threading.Thread(target=loop, name="cv-queue", daemon=True)
    _thread.start()
    logger.info("activity-queue drainer started")
