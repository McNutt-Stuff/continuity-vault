"""Microsoft 365 — node/CP reconcile worker (phase 2: discovery).

Runs on every box that owns M365 integration instances (the control plane for
CP-hosted tenants, each customer node for its federated tenants). Each cycle it
reconciles the signed desired state and refreshes Entra identity discovery for
connected organizations. Discovered identities live in the local DB and (on a
node) replicate up to the control plane for the portal.

Content collection (Exchange/OneDrive/…) is a later phase; this worker only does
identity discovery today.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger("cv.integrations.m365.worker")

INTEGRATION_TYPE = "microsoft365"
_REDISCOVER_SECONDS = 6 * 3600  # periodic identity refresh cadence
_thread = None


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _due(inst, ds) -> bool:
    """Discovery is due when the desired state hasn't been applied yet (a fresh
    activate/scope change), when we've never discovered, or on the refresh cadence."""
    if ds is not None and int(ds.applied_version or 0) < int(ds.version or 0):
        return True
    if inst.last_run_at is None:
        return True
    return (_now() - inst.last_run_at).total_seconds() >= _REDISCOVER_SECONDS


def run_due(db) -> int:
    """Reconcile + discover for every connected M365 instance in the local DB."""
    from ... import platform_config
    from ...models import IntegrationInstance
    from . import discovery, models as m

    vals = platform_config.integration_values(INTEGRATION_TYPE)
    client_id = (vals.get("client_id") or "").strip()
    client_secret = (vals.get("client_secret") or "").strip()

    ran = 0
    insts = (db.query(IntegrationInstance)
             .filter(IntegrationInstance.integration_type == INTEGRATION_TYPE,
                     IntegrationInstance.enabled.is_(True)).all())
    for inst in insts:
        cred = (db.query(m.ManagedCredentialRef)
                .filter(m.ManagedCredentialRef.integration_instance_id == inst.id).first())
        if cred is None or cred.consent_state != "granted":
            continue
        ds = (db.query(m.IntegrationDesiredState)
              .filter(m.IntegrationDesiredState.integration_instance_id == inst.id).first())
        if not _due(inst, ds):
            continue
        if ds is not None and ds.status != "applied":
            ds.status = "applying"
            db.commit()
        res = discovery.run_discovery(db, inst, client_id=client_id,
                                      client_secret=client_secret)
        ran += 1
        if res.get("ok"):
            if ds is not None:
                ds.applied_version = ds.version
                ds.applied_at = _now()
                ds.status = "applied"
            inst.provision_state = "done"
            inst.provision_message = (f"Discovered {res.get('discovered', 0)} Entra "
                                      f"identities ({res.get('in_scope', 0)} in scope)")
            inst.status = "active"
        else:
            inst.last_error = str(res.get("error") or "discovery failed")[:400]
            inst.status = "error"
            if ds is not None:
                ds.status = "failed"
        db.commit()
    return ran


def start_m365_worker() -> None:
    """Start the background reconcile loop (idempotent)."""
    global _thread
    if _thread is not None:
        return

    def _loop() -> None:
        time.sleep(30)  # let startup settle
        while True:
            try:
                from ...db import WorkerSessionLocal
                with WorkerSessionLocal() as db:
                    n = run_due(db)
                    if n:
                        logger.info("m365 reconcile: discovered %d instance(s)", n)
            except Exception:  # noqa: BLE001
                logger.exception("m365 reconcile cycle failed")
            time.sleep(300)

    _thread = threading.Thread(target=_loop, name="cv-m365-worker", daemon=True)
    _thread.start()
    logger.info("microsoft 365 discovery worker started")
