"""Tenant-scoped activity feed + audit log.

The admin console has a platform-wide audit view; these endpoints give a tenant
its own operational visibility: a live activity feed (backups / syncs / ingests
in flight and recently completed) and a full, filterable audit log covering
normal usage, credential access, and abnormal/security events.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import audit, security
from ..db import get_db
from ..models import (
    AuditEvent,
    Appliance,
    ApplianceStorage,
    Collection,
    ConnectorAccount,
    DesktopAgent,
    SnapshotReceipt,
    SyncJob,
    Tenant,
)

router = APIRouter(tags=["activity"])


def _dest_labeler(db: Session, tenant_id: str):
    """Return a fn mapping a destination id to a friendly label (resolving
    store:<id> to "<appliance> · <storage>")."""
    appliances = {a.id: a for a in db.query(Appliance)
                  .filter(Appliance.tenant_id == tenant_id).all()}
    stores = {f"store:{s.id}": s for s in db.query(ApplianceStorage)
              .filter(ApplianceStorage.tenant_id == tenant_id).all()}

    def label(dest: str) -> str:
        if dest == "cv-cloud":
            return "Arkive Cloud"
        if dest == "customer-s3":
            return "Customer S3"
        if dest in stores:
            s = stores[dest]
            a = appliances.get(s.appliance_id)
            return f"{a.name} · {s.name}" if a else s.name
        if dest.startswith("appliance"):
            return "Appliance"
        return dest
    return label


@router.get("/activity")
def activity(limit: int = 40,
             principal: security.Principal = Depends(security.get_principal),
             tenant: Tenant = Depends(security.get_tenant),
             db: Session = Depends(get_db)):
    """Recent + in-flight backup / sync / ingest activity for this tenant."""
    colls = {c.id: c for c in db.query(Collection)
             .filter(Collection.tenant_id == tenant.id).all()}

    def _source_label(collection_id: str) -> str:
        c = colls.get(collection_id)
        if not c:
            return "unknown source"
        if c.connector_account_id:
            acc = db.get(ConnectorAccount, c.connector_account_id)
            if acc:
                return acc.account_label
        return c.name

    def _source_type(collection_id: str) -> str:
        c = colls.get(collection_id)
        return c.source_type if c else ""

    # Recently completed snapshot receipts (the concrete "data landed" events).
    receipts = (db.query(SnapshotReceipt)
                .filter(SnapshotReceipt.tenant_id == tenant.id)
                .order_by(SnapshotReceipt.created_at.desc())
                .limit(limit).all())
    dest_label = _dest_labeler(db, tenant.id)
    events = [{
        "kind": "backup",
        "collection_id": rc.collection_id,
        "source": _source_label(rc.collection_id),
        "source_type": _source_type(rc.collection_id),
        "destination": rc.destination,
        "destination_label": dest_label(rc.destination),
        "object_count": rc.object_count,
        "total_bytes": rc.total_bytes,
        "status": "recoverable" if rc.recoverable else "pending",
        "snapshot_id": rc.snapshot_id,
        "at": rc.created_at.isoformat(),
    } for rc in receipts]

    # In-flight desktop-agent collections (queued but not yet delivered).
    in_flight = []
    for a in (db.query(DesktopAgent)
              .filter(DesktopAgent.tenant_id == tenant.id).all()):
        pending = ([a.pending_command] if a.pending_command else []) + list(a.pending_commands or [])
        for cmd in pending:
            params = (cmd or {}).get("params") or {}
            in_flight.append({
                "kind": "agent-collect",
                "source": a.hostname or a.name,
                "source_type": params.get("source_type") or "onepassword",
                "status": "queued",
                "command": (cmd or {}).get("type"),
            })

    # Tracked connector backup/sync jobs (running + recently finished).
    jobs = (db.query(SyncJob)
            .filter(SyncJob.tenant_id == tenant.id,
                    SyncJob.status.in_(["queued", "running"]))
            .order_by(SyncJob.created_at.desc()).all())
    job_items = [{
        "id": j.id,
        "collection_id": j.collection_id,
        "source": _source_label(j.collection_id),
        "source_type": _source_type(j.collection_id),
        "kind": j.kind,
        "status": j.status,
        "processed": j.processed or 0,
        "total": j.total or 0,
        "message": j.message or "",
        "at": (j.started_at or j.created_at).isoformat(),
    } for j in jobs]

    pending = sum(1 for e in events if e["status"] == "pending")

    # Sources currently in an error / needs-reauth state (from the last sync).
    source_errors = [{
        "kind": "source-error",
        "account_id": a.id,
        "source": a.account_label,
        "source_type": a.connector_type,
        "needs_reauth": a.auth_status == "needs-reauth",
        "error": a.last_error,
        "at": a.last_error_at.isoformat() if a.last_error_at else None,
    } for a in (db.query(ConnectorAccount)
                .filter(ConnectorAccount.tenant_id == tenant.id,
                        ConnectorAccount.last_error.isnot(None))
                .order_by(ConnectorAccount.last_error_at.desc()).all())]

    return {
        "in_flight": in_flight,
        "events": events,
        "jobs": job_items,
        "source_errors": source_errors,
        "summary": {
            "recent": len(events),
            "pending": pending,
            "queued_agents": len(in_flight),
            "active_jobs": len(job_items),
            "source_errors": len(source_errors),
        },
    }


# Severities that constitute an operator-facing alert.
_ALERT_SEVERITIES = ("warning", "error", "critical")


@router.get("/alerts")
def alerts(limit: int = 50,
           principal: security.Principal = Depends(security.get_principal),
           tenant: Tenant = Depends(security.get_tenant),
           db: Session = Depends(get_db)):
    """Abnormal / security-relevant events surfaced as operator alerts.

    Derived from the audit ledger (warning + critical severities): failed auth,
    attestation failures, quarantines, backup failures, and re-auth needs.
    """
    rows = (db.query(AuditEvent)
            .filter(AuditEvent.tenant_id == tenant.id,
                    AuditEvent.severity.in_(_ALERT_SEVERITIES))
            .order_by(AuditEvent.created_at.desc())
            .limit(limit).all())
    items = [{
        "id": (e.entry_hash or "")[:16],
        "actor": e.actor, "action": e.action, "resource": e.resource,
        "category": e.category, "severity": e.severity,
        "detail": e.detail, "created_at": e.created_at.isoformat(),
    } for e in rows]
    return {
        "count": len(items),
        "critical": sum(1 for i in items if i["severity"] == "critical"),
        "alerts": items,
    }


@router.get("/audit")
def audit_log(limit: int = 200, category: str | None = None,
              severity: str | None = None, actor: str | None = None,
              action: str | None = None,
              principal: security.Principal = Depends(security.get_principal),
              tenant: Tenant = Depends(security.get_tenant),
              db: Session = Depends(get_db)):
    """Full, filterable tenant audit log (activity, security, credential access)."""
    q = db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant.id)
    if category:
        q = q.filter(AuditEvent.category == category)
    if severity:
        q = q.filter(AuditEvent.severity == severity)
    if actor:
        q = q.filter(AuditEvent.actor.ilike(f"%{actor}%"))
    if action:
        q = q.filter(AuditEvent.action.ilike(f"%{action}%"))
    rows = q.order_by(AuditEvent.created_at.desc()).limit(limit).all()

    # Category/severity tallies across the tenant's whole ledger (for the header).
    tallies: dict[str, int] = {}
    for e in (db.query(AuditEvent)
              .filter(AuditEvent.tenant_id == tenant.id).all()):
        tallies[e.category or "activity"] = tallies.get(e.category or "activity", 0) + 1

    return {
        "chain_valid": audit.verify_chain(db),
        "tallies": tallies,
        "events": [{
            "actor": e.actor, "action": e.action, "resource": e.resource,
            "category": e.category, "severity": e.severity,
            "detail": e.detail, "entry_hash": (e.entry_hash or "")[:16],
            "created_at": e.created_at.isoformat(),
        } for e in rows],
    }
