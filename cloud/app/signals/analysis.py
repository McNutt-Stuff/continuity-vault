"""Arkive Signal Platform — analysis pass.

Runs every provider for a tenant (reusing already-collected data), then derives
cross-provider outcomes: protection-gap findings, asset reconciliation, and
freshness aging. Idempotent — findings dedup + auto-resolve when their condition
clears. Runs on the box that owns the tenant (customer node in federated mode,
else the control plane).
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from . import engine
from .provider_base import all_providers

logger = logging.getLogger("cv.signals.analysis")


def run_tenant(db: Session, tenant) -> dict:
    tid = tenant.id
    results: dict = {}
    for p in all_providers():
        try:
            results[p.provider] = p.collect(db, tenant)
        except Exception as exc:  # noqa: BLE001 — one provider must not break others
            db.rollback()
            logger.exception("signal provider %s failed for tenant %s", p.provider, tid)
            try:
                engine.record_provider_health(db, tid, p.provider,
                                              connection_status="degraded", success=False,
                                              errors=1, last_error=str(exc)[:500])
            except Exception:  # noqa: BLE001
                db.rollback()
    db.flush()
    try:
        _protection_gap_findings(db, tenant)
        _asset_reconciliation(db, tenant)
        engine.refresh_freshness(db, tid)
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("signal analysis derivation failed for tenant %s", tid)
    return results


def _protection_gap_findings(db: Session, tenant) -> None:
    from ..models import Signal
    tid = tenant.id
    active_fps: set[str] = set()
    rows = (db.query(Signal)
            .filter(Signal.tenant_id == tid, Signal.status == "active",
                    Signal.signal_type.in_([
                        "arkive.source.protection_status", "arkive.source.sync_health",
                        "arkive.backup.current", "appliance.online", "node.online"]))
            .all())
    for s in rows:
        v = s.value or {}
        ft = None
        title = desc = ""
        sev = s.severity
        remediation: dict = {}
        subj = (s.subject_type, s.subject_id)
        if s.signal_type == "arkive.source.protection_status" and s.normalized_value == "degraded":
            ft = "protection_gap"
            title = f"Source needs attention: {v.get('name') or s.subject_id}"
            desc = f"{v.get('source_type', 'source')} is degraded ({v.get('auth_status') or v.get('last_error') or 'unhealthy'})."
            remediation = {"label": "Fix source", "route": "/connectors"}
        elif s.signal_type == "arkive.backup.current" and s.normalized_value == "false":
            ft = "protection_gap"
            title = f"Backup overdue: {v.get('name') or s.subject_id}"
            desc = "This source has not completed a recent backup within its schedule."
            remediation = {"label": "Review source", "route": "/connectors"}
        elif s.signal_type == "arkive.source.sync_health" and s.normalized_value == "failing":
            ft = "protection_gap"
            title = f"Source failing repeatedly: {s.subject_id}"
            desc = f"Consecutive sync failures: {v.get('fail_count')}."
            remediation = {"label": "Fix source", "route": "/connectors"}
        elif s.signal_type == "appliance.online" and s.normalized_value == "false":
            ft = "device_offline"
            title = f"Appliance offline: {v.get('name') or s.subject_id}"
            desc = "The appliance has not reported recently."
            remediation = {"label": "View appliances", "route": "/appliances"}
        elif s.signal_type == "node.online" and s.normalized_value == "false":
            ft = "device_offline"
            title = f"Node offline: {v.get('name') or s.subject_id}"
            desc = "A customer node has stopped heartbeating."
        if ft:
            f = engine.upsert_finding(
                db, tid, ft, title=title, description=desc, severity=sev,
                category=s.category, subject_type=subj[0], subject_id=subj[1],
                signal_ids=[s.id], remediation=remediation, owner_user_id=(s.actor_id or ""))
            active_fps.add(f.fingerprint)
    engine.resolve_findings_not_in(db, tid, "protection_gap", active_fps)
    engine.resolve_findings_not_in(db, tid, "device_offline",
                                   {fp for fp in active_fps})


def _asset_reconciliation(db: Session, tenant) -> None:
    """Correlate network clients with managed endpoints; flag corporate-looking
    network assets that have no corresponding Arkive-managed endpoint."""
    from ..models import DesktopAgent, NetworkClient
    tid = tenant.id
    agents = db.query(DesktopAgent).filter(DesktopAgent.tenant_id == tid).all()
    known = set()
    for a in agents:
        for key in (a.hostname, a.name):
            if key:
                known.add(str(key).strip().lower())
    active_fps: set[str] = set()
    for c in db.query(NetworkClient).filter(
            NetworkClient.tenant_id == tid,
            NetworkClient.monitor_state != "ignored").all():
        # Only "computer" clients that aren't guests are candidate managed endpoints.
        if c.is_guest or (c.device_type or "") not in ("computer", "server", "laptop", "desktop"):
            continue
        label = (c.hostname or c.name or c.nickname or "").strip().lower()
        if label and label in known:
            continue  # correlated to a managed endpoint
        f = engine.upsert_finding(
            db, tid, "unmanaged_asset",
            title=f"Unmanaged network device: {c.nickname or c.name or c.hostname or c.ip}",
            description="A corporate-looking device is on the network with no matching "
                        "Arkive-managed endpoint.",
            severity="medium", category="ENDPOINT",
            subject_type="network_client", subject_id=(c.client_key or c.id),
            remediation={"label": "Review devices", "route": "/integrations"},
            meta={"ip": c.ip, "mac": c.mac, "device_type": c.device_type},
            owner_user_id=(c.owner_user_id or ""))
        active_fps.add(f.fingerprint)
    engine.resolve_findings_not_in(db, tid, "unmanaged_asset", active_fps)
