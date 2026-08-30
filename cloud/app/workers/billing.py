"""
Recurring-billing sweep worker.

A daemon thread on the control plane wakes hourly and charges every billing
profile whose monthly anniversary has arrived (``billing_engine.run_due_charges``).
Billing data is control-plane-authoritative, so this runs only there.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger("cv.billing_worker")

_thread: threading.Thread | None = None
_TICK_SECONDS = 3600  # hourly is fine — charges are due-date driven, not tick driven


def start_billing_worker() -> None:
    global _thread
    if _thread is not None:
        return

    def loop() -> None:
        time.sleep(45)  # let startup + migrations settle
        from ..billing_engine import run_due_charges
        from ..db import WorkerSessionLocal as SessionLocal
        while True:
            try:
                with SessionLocal() as db:
                    run_due_charges(db)
            except Exception:  # noqa: BLE001 — never let the worker die
                logger.exception("billing sweep failed")
            time.sleep(_TICK_SECONDS)

    _thread = threading.Thread(target=loop, name="cv-billing", daemon=True)
    _thread.start()
    logger.info("recurring-billing worker started")
