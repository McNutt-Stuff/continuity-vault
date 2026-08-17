"""
Backend admin console API (platform-admin only).

Provides cross-tenant fleet visibility, tenant administration, crypto-profile
and quantum-transition inventory, audit-ledger verification, and software-release
publishing / update dispatch. Deliberately excludes any operation that would let
an operator read customer plaintext (spec 3.1: no standing plaintext access).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from cv_crypto.profiles import PROFILE_REGISTRY
from cv_crypto.provider import get_provider

from .. import audit, security
from ..db import get_db
from ..models import (
    Appliance,
    AuditEvent,
    ConnectorAccount,
    SnapshotReceipt,
    SoftwareRelease,
    Tenant,
    UpdateJob,
    User,
)

router = APIRouter(prefix="/admin", tags=["admin"],
                   dependencies=[Depends(security.require_platform_admin)])


@router.get("/overview")
def overview(db: Session = Depends(get_db)):
    provider = get_provider()
    return {
        "tenants": db.query(func.count(Tenant.id)).scalar(),
        "users": db.query(func.count(User.id)).scalar(),
        "appliances": db.query(func.count(Appliance.id)).scalar(),
        "connectors": db.query(func.count(ConnectorAccount.id)).scalar(),
        "snapshots": db.query(func.count(SnapshotReceipt.id)).scalar(),
        "recoverable_snapshots": db.query(func.count(SnapshotReceipt.id))
            .filter(SnapshotReceipt.recoverable.is_(True)).scalar(),
        "pq_available": provider.pq_available,
        "audit_chain_valid": audit.verify_chain(db),
    }


@router.get("/tenants")
def list_tenants(db: Session = Depends(get_db)):
    out = []
    for t in db.query(Tenant).all():
        out.append({
            "id": t.id,
            "name": t.name,
            "plan": t.plan,
            "key_ownership_model": t.key_ownership_model,
            "status": t.status,
            "users": db.query(func.count(User.id)).filter(User.tenant_id == t.id).scalar(),
            "appliances": db.query(func.count(Appliance.id))
                .filter(Appliance.tenant_id == t.id).scalar(),
        })
    return out


@router.get("/fleet")
def fleet_view(db: Session = Depends(get_db)):
    rows = db.query(Appliance).all()
    return [{
        "id": a.id, "serial": a.serial, "model": a.model, "name": a.name,
        "tenant_id": a.tenant_id, "state": a.state,
        "isolation_state": a.isolation_state, "attestation_ok": a.attestation_ok,
        "tamper_state": a.tamper_state, "software_version": a.software_version,
        "last_heartbeat_at": a.last_heartbeat_at.isoformat() if a.last_heartbeat_at else None,
    } for a in rows]


@router.get("/crypto-profiles")
def crypto_profiles():
    """Cryptographic-profile registry + quantum-transition inventory (spec 9.7)."""
    return {"profiles": [p.as_dict() for p in PROFILE_REGISTRY.values()],
            "pq_available": get_provider().pq_available}


@router.get("/audit")
def audit_log(limit: int = 100, db: Session = Depends(get_db)):
    rows = (db.query(AuditEvent).order_by(AuditEvent.created_at.desc())
            .limit(limit).all())
    return {
        "chain_valid": audit.verify_chain(db),
        "events": [{
            "actor": e.actor, "action": e.action, "resource": e.resource,
            "tenant_id": e.tenant_id, "detail": e.detail,
            "entry_hash": e.entry_hash[:16],
            "created_at": e.created_at.isoformat(),
        } for e in rows],
    }
