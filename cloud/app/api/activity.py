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
    Collection,
    ConnectorAccount,
    DesktopAgent,
    SnapshotReceipt,
    Tenant,
)

router = APIRouter(tags=["activity"])


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
    events = [{
        "kind": "backup",
        "source": _source_label(rc.collection_id),
        "source_type": _source_type(rc.collection_id),
        "destination": rc.destination,
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
        if a.pending_command:
            in_flight.append({
                "kind": "agent-collect",
                "source": a.hostname or a.name,
                "source_type": "onepassword",
                "status": "queued",
                "command": (a.pending_command or {}).get("type"),
            })

    pending = sum(1 for e in events if e["status"] == "pending")
    return {
        "in_flight": in_flight,
        "events": events,
        "summary": {
            "recent": len(events),
            "pending": pending,
            "queued_agents": len(in_flight),
        },
    }


# Severities that constitute an operator-facing alert.
_ALERT_SEVERITIES = ("warning", "critical")


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
