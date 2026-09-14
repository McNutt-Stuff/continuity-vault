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
    from ...config import get_settings
    from ...models import IntegrationInstance, Tenant
    from . import collect, discovery, graph, models as m, provisioning

    vals = platform_config.integration_values(INTEGRATION_TYPE)
    client_id = (vals.get("client_id") or "").strip()
    client_secret = (vals.get("client_secret") or "").strip()
    role = (get_settings().node_role or "control-plane")

    ran = 0
    insts = (db.query(IntegrationInstance)
             .filter(IntegrationInstance.integration_type == INTEGRATION_TYPE,
                     IntegrationInstance.enabled.is_(True)).all())
    for inst in insts:
        # Federation ownership: a node-hosted tenant's discovery/collection runs on
        # its ASSIGNED node (polling + storage live there). The control plane only
        # reconciles the tenants it hosts itself; the node processes its own local DB.
        tenant = db.get(Tenant, inst.tenant_id)
        if role == "control-plane" and tenant is not None and tenant.node_id:
            # Member auto-map/auto-create is a CP concern (vault keys live here), so
            # the CP reconciles bindings from the identities the node pushed back.
            cfg = inst.config or {}
            if cfg.get("auto_map") or cfg.get("auto_create"):
                try:
                    provisioning.reconcile_bindings(db, inst, can_provision=True)
                except Exception:  # noqa: BLE001
                    logger.exception("m365 CP binding reconcile failed (instance=%s)", inst.id)
            continue
        cred = (db.query(m.ManagedCredentialRef)
                .filter(m.ManagedCredentialRef.integration_instance_id == inst.id).first())
        if cred is None or cred.consent_state != "granted":
            continue
        ds = (db.query(m.IntegrationDesiredState)
              .filter(m.IntegrationDesiredState.integration_instance_id == inst.id).first())
        # A desired-state change (activation, scope edit, or a "Back up now" that
        # bumps desired) forces an immediate collection pass this cycle, not just
        # discovery — otherwise the button did nothing until the next cadence.
        force_collect = False
        if _due(inst, ds):
            if ds is not None and ds.status != "applied":
                ds.status = "applying"
                db.commit()
            res = discovery.run_discovery(db, inst, client_id=client_id,
                                          client_secret=client_secret,
                                          can_provision=(role == "control-plane"))
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
                force_collect = True
                try:
                    collect.provision_sources(db, inst)  # establish per-user sources
                except Exception:  # noqa: BLE001
                    logger.exception("m365 provision_sources failed (instance=%s)", inst.id)
            else:
                inst.last_error = str(res.get("error") or "discovery failed")[:400]
                inst.status = "error"
                if ds is not None:
                    ds.status = "failed"
            db.commit()

        # Admin-approved content protection: discover org resources (SharePoint
        # sites / Teams) into managed sources + their Collections. The ACTUAL
        # collection now runs through the SHARED scheduler (workers/scheduler.py →
        # run_backup → collect.run_managed_collection), so managed sources poll,
        # create SyncJobs and show in Activity exactly like every other source —
        # this worker only keeps discovery + provisioning current.
        if (inst.config or {}).get("collect_enabled") and client_id and client_secret:
            try:
                token = graph.app_token(client_id, client_secret, cred.microsoft_tenant_id)
            except graph.GraphError as e:
                logger.warning("m365 collect token failed (instance=%s): %s", inst.id, e)
                token = ""
            if token:
                try:
                    collect.provision_org_sources(db, inst, token)
                except Exception:  # noqa: BLE001
                    logger.exception("m365 org provisioning failed (instance=%s)", inst.id)
            # A "Back up now" (or activation) bumps desired state → force_collect:
            # reset the managed Collections' due time so the SHARED scheduler runs
            # them on its very next tick instead of waiting for the cadence.
            if force_collect:
                try:
                    _trigger_managed_now(db, inst)
                except Exception:  # noqa: BLE001
                    logger.exception("m365 trigger-now failed (instance=%s)", inst.id)
    return ran


def _trigger_managed_now(db, inst) -> None:
    """Make every managed Collection for this instance due immediately (reset the
    scheduler watermark) so the shared scheduler backs them up on its next tick."""
    from ...models import Collection
    n = 0
    for c in (db.query(Collection)
              .filter(Collection.tenant_id == inst.tenant_id).all()):
        cfg = c.config or {}
        if cfg.get("m365_instance_id") == inst.id and cfg.get("managed"):
            c.last_backup_run_at = None
            n += 1
    if n:
        db.commit()
        logger.info("m365 trigger-now: %d managed collection(s) marked due (instance=%s)",
                    n, inst.id)


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
