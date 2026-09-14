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

        # Admin-approved content protection: collect each managed user's Exchange +
        # OneDrive with the app-only token (reusing the connector fetchers).
        if (inst.config or {}).get("collect_enabled") and client_id and client_secret:
            try:
                token = graph.app_token(client_id, client_secret, cred.microsoft_tenant_id)
            except graph.GraphError as e:
                logger.warning("m365 collect token failed (instance=%s): %s", inst.id, e)
                token = ""
            if token:
                _collect_due_sources(db, inst, token)
    return ran


def _collect_due_sources(db, inst, token: str) -> None:
    from . import collect, models as m
    sources = (db.query(m.ManagedSource)
               .filter(m.ManagedSource.integration_instance_id == inst.id,
                       m.ManagedSource.state.notin_(("decommissioned", "paused_by_admin"))).all())
    for src in sources:
        if not _source_due(src):
            continue
        try:
            collect.collect_source(db, inst, src, token)
        except Exception:  # noqa: BLE001
            logger.exception("m365 collect_source crashed (source=%s)", src.id)


def _source_due(src) -> bool:
    # Mail still backfilling collects every cycle; otherwise on the profile's
    # schedule (interval_minutes, set from the managed-protection profile) or the
    # default refresh cadence.
    if src.workload == "exchange" and (src.config or {}).get("phase", "backfill") == "backfill":
        return True
    if src.last_collected_at is None:
        return True
    iv = (src.config or {}).get("interval_minutes")
    try:
        secs = int(iv) * 60 if iv else _REDISCOVER_SECONDS
    except (TypeError, ValueError):
        secs = _REDISCOVER_SECONDS
    return (_now() - src.last_collected_at).total_seconds() >= max(300, secs)


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
