"""
Appliance endpoints: tenant-facing fleet management + agent-facing management
plane (spec 5).

The agent-facing endpoints implement outbound-only management: the appliance
polls ``/appliance/heartbeat`` and receives signed, sequenced, expiring commands.
The control plane never mounts appliance storage; it only exchanges telemetry,
attestation, signed commands, and signed receipts (spec 2.1, build-instr 3).
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from cv_crypto.signing import SigPolicy

from .. import audit, fleet, security
from ..config import get_settings
from ..db import get_db
from ..models import (
    Appliance,
    ApplianceCommand,
    LinkingCode,
    SnapshotReceipt,
    Tenant,
)

settings = get_settings()
router = APIRouter(tags=["appliances"])

# Prototype appliance-agent bearer tokens issued at activation.
_agent_tokens: dict[str, str] = {}  # token -> appliance_id


def _now():
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Tenant-facing fleet management
# --------------------------------------------------------------------------

fleet_router = APIRouter(prefix="/appliances", tags=["appliances"])


class CreateLinkingCodeRequest(BaseModel):
    model: str = "CV Edge 8"
    name: str = "Home Appliance"


@fleet_router.post("/linking-code")
def create_linking_code(body: CreateLinkingCodeRequest,
                        principal: security.Principal = Depends(security.require_security_admin),
                        tenant: Tenant = Depends(security.get_tenant),
                        db: Session = Depends(get_db)):
    code = f"CV-{secrets.token_hex(3).upper()}-{secrets.token_hex(3).upper()}"
    lc = LinkingCode(
        tenant_id=tenant.id,
        code=code,
        model=body.model,
        name=body.name,
        expires_at=_now() + timedelta(seconds=settings.linking_code_ttl_seconds),
    )
    db.add(lc)
    db.commit()
    audit.record(db, actor=principal.user_id, action="appliance.linking_code_created",
                 tenant_id=tenant.id, detail={"model": body.model})
    return {"code": code, "expires_at": lc.expires_at.isoformat(),
            "model": body.model, "name": body.name}


@fleet_router.get("")
def list_appliances(tenant: Tenant = Depends(security.get_tenant),
                    db: Session = Depends(get_db)):
    rows = db.query(Appliance).filter(Appliance.tenant_id == tenant.id).all()
    return [_appliance_view(a) for a in rows]


@fleet_router.get("/{appliance_id}")
def get_appliance(appliance_id: str,
                  tenant: Tenant = Depends(security.get_tenant),
                  db: Session = Depends(get_db)):
    a = db.get(Appliance, appliance_id)
    if not a or a.tenant_id != tenant.id:
        raise HTTPException(404, "appliance not found")
    commands = (db.query(ApplianceCommand)
                .filter(ApplianceCommand.appliance_id == a.id)
                .order_by(ApplianceCommand.created_at.desc()).limit(20).all())
    view = _appliance_view(a)
    view["recent_commands"] = [{"type": c.command_type, "status": c.status,
                                "sequence": c.sequence,
                                "created_at": c.created_at.isoformat()} for c in commands]
    return view


class CommandRequest(BaseModel):
    command_type: str
    parameters: dict = {}


@fleet_router.post("/{appliance_id}/command")
def send_command(appliance_id: str, body: CommandRequest,
                 principal: security.Principal = Depends(security.require_security_admin),
                 tenant: Tenant = Depends(security.get_tenant),
                 db: Session = Depends(get_db)):
    a = db.get(Appliance, appliance_id)
    if not a or a.tenant_id != tenant.id:
        raise HTTPException(404, "appliance not found")
    # Prohibited unilateral operations are simply not exposed here (spec 5.1).
    cmd = fleet.issue_command(db, a, body.command_type, principal.user_id, body.parameters)
    audit.record(db, actor=principal.user_id, action="appliance.command_issued",
                 tenant_id=tenant.id, resource=a.id,
                 detail={"type": body.command_type, "sequence": cmd.sequence})
    return {"command_id": cmd.id, "sequence": cmd.sequence, "status": cmd.status}


def _appliance_view(a: Appliance) -> dict:
    tel = a.telemetry or {}
    return {
        "id": a.id,
        "serial": a.serial,
        "model": a.model,
        "name": a.name,
        "location_label": a.location_label,
        "state": a.state,
        "isolation_state": a.isolation_state,
        "software_version": a.software_version,
        "attestation_ok": a.attestation_ok,
        "tamper_state": a.tamper_state,
        "last_heartbeat_at": a.last_heartbeat_at.isoformat() if a.last_heartbeat_at else None,
        "last_attestation_at": a.last_attestation_at.isoformat() if a.last_attestation_at else None,
        "telemetry": tel,
    }


# --------------------------------------------------------------------------
# Agent-facing management plane (outbound-only from the appliance)
# --------------------------------------------------------------------------

agent_router = APIRouter(prefix="/appliance", tags=["appliance-agent"])


def _agent_appliance(authorization: str = Header(default=""),
                     db: Session = Depends(get_db)) -> Appliance:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing appliance token")
    token = authorization.split(" ", 1)[1]
    appliance_id = _agent_tokens.get(token)
    if not appliance_id:
        raise HTTPException(401, "invalid appliance token")
    a = db.get(Appliance, appliance_id)
    if not a:
        raise HTTPException(404, "appliance not found")
    return a


class ActivateRequest(BaseModel):
    linking_code: str
    serial: str
    model: str | None = None
    identity_bundle: dict  # appliance hybrid signing public bundle
    attestation: dict  # secure-boot / firmware / os measurements


@agent_router.post("/activate")
def activate(body: ActivateRequest, db: Session = Depends(get_db)):
    """Turnkey activation ceremony (spec 13.1). The appliance presents a linking
    code and its hardware-bound identity; the cloud returns configuration and the
    control-plane public bundle used to verify all future commands."""
    lc = db.query(LinkingCode).filter(LinkingCode.code == body.linking_code).first()
    if not lc or lc.consumed or lc.expires_at < _now():
        raise HTTPException(400, "invalid or expired linking code")

    appliance = Appliance(
        tenant_id=lc.tenant_id,
        serial=body.serial,
        model=body.model or lc.model,
        name=lc.name,
        state="ONLINE_STAGING",
        isolation_state="sealed",
        identity_bundle=body.identity_bundle,
        attestation_ok=True,
        last_attestation_at=_now(),
        software_version="1.0.0",
    )
    db.add(appliance)
    lc.consumed = True
    lc.appliance_id = appliance.id
    db.commit()
    db.refresh(appliance)

    token = secrets.token_urlsafe(32)
    _agent_tokens[token] = appliance.id
    audit.record(db, actor=f"appliance:{body.serial}", action="appliance.activated",
                 tenant_id=lc.tenant_id, resource=appliance.id)

    return {
        "appliance_id": appliance.id,
        "agent_token": token,
        "tenant_id": lc.tenant_id,
        "cloud_public_bundle": fleet.cloud_public_bundle(),
        "config": {
            "heartbeat_interval_seconds": settings.heartbeat_interval_seconds,
            "command_ttl_seconds": settings.command_ttl_seconds,
            "domain": settings.domain,
            "retention_floor_days": 365,
            "immutability": True,
        },
    }


class HeartbeatRequest(BaseModel):
    state: str
    isolation_state: str
    software_version: str
    attestation: dict
    telemetry: dict
    tamper_state: str = "normal"


@agent_router.post("/heartbeat")
def heartbeat(body: HeartbeatRequest,
              appliance: Appliance = Depends(_agent_appliance),
              db: Session = Depends(get_db)):
    appliance.state = body.state
    appliance.isolation_state = body.isolation_state
    appliance.software_version = body.software_version
    appliance.telemetry = body.telemetry
    appliance.tamper_state = body.tamper_state
    appliance.last_heartbeat_at = _now()
    appliance.last_attestation_at = _now()
    # Failed attestation -> restricted/quarantined (spec 5.3).
    appliance.attestation_ok = bool(body.attestation.get("secure_boot")) and \
        body.tamper_state == "normal"
    if not appliance.attestation_ok and appliance.state != "QUARANTINED":
        appliance.state = "QUARANTINED"
    db.commit()

    # Deliver pending signed commands (management plane only).
    pending = (db.query(ApplianceCommand)
               .filter(ApplianceCommand.appliance_id == appliance.id,
                       ApplianceCommand.status == "pending")
               .order_by(ApplianceCommand.sequence.asc()).all())
    delivered = []
    for c in pending:
        c.status = "delivered"
        delivered.append(c.envelope)
    db.commit()
    return {"commands": delivered,
            "next_heartbeat_seconds": settings.heartbeat_interval_seconds}


class CommandResultRequest(BaseModel):
    command_id: str
    accepted: bool
    result: dict = {}
    receipt: dict | None = None  # signed seal/attestation receipt


@agent_router.post("/command-result")
def command_result(body: CommandResultRequest,
                   appliance: Appliance = Depends(_agent_appliance),
                   db: Session = Depends(get_db)):
    cmd = db.get(ApplianceCommand, body.command_id)
    if not cmd or cmd.appliance_id != appliance.id:
        raise HTTPException(404, "command not found")
    cmd.status = "acked" if body.accepted else "rejected"
    cmd.result = body.result
    db.commit()
    audit.record(db, actor=f"appliance:{appliance.serial}",
                 action="appliance.command_result", tenant_id=appliance.tenant_id,
                 resource=cmd.id, detail={"accepted": body.accepted})
    return {"ok": True}


class SealReceiptRequest(BaseModel):
    vault_id: str
    collection_id: str
    snapshot_id: str
    object_count: int
    total_bytes: int
    manifest_hash: str
    receipt: dict  # signed seal receipt {payload, signature}


@agent_router.post("/seal-receipt")
def seal_receipt(body: SealReceiptRequest,
                 appliance: Appliance = Depends(_agent_appliance),
                 db: Session = Depends(get_db)):
    # Verify the appliance's hybrid signature before marking recoverable
    # (spec build-instr 18: only mark recoverable once destination signs commit).
    ok = fleet.verify_appliance_receipt(
        appliance, body.receipt["payload"], body.receipt["signature"],
        SigPolicy.REQUIRE_BOTH,
    )
    receipt = SnapshotReceipt(
        tenant_id=appliance.tenant_id,
        appliance_id=appliance.id,
        vault_id=body.vault_id,
        collection_id=body.collection_id,
        snapshot_id=body.snapshot_id,
        destination="appliance",
        object_count=body.object_count,
        total_bytes=body.total_bytes,
        manifest_hash=body.manifest_hash,
        recoverable=ok,
        receipt=body.receipt,
    )
    db.add(receipt)
    db.commit()
    audit.record(db, actor=f"appliance:{appliance.serial}",
                 action="appliance.snapshot_sealed", tenant_id=appliance.tenant_id,
                 resource=body.snapshot_id, detail={"verified": ok})
    return {"recoverable": ok, "snapshot_id": body.snapshot_id}
