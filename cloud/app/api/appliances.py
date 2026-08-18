"""
Appliance endpoints: tenant-facing fleet management + agent-facing management
plane (spec 5).

The agent-facing endpoints implement outbound-only management: the appliance
polls ``/appliance/heartbeat`` and receives signed, sequenced, expiring commands.
The control plane never mounts appliance storage; it only exchanges telemetry,
attestation, signed commands, and signed receipts (spec 2.1, build-instr 3).
"""

from __future__ import annotations

import hashlib
import io
import secrets
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from cv_crypto.signing import SigPolicy

from .. import audit, fleet, security
from ..config import get_settings
from ..db import get_db
from ..models import (
    Appliance,
    ApplianceCommand,
    ApplianceStorage,
    LinkingCode,
    SnapshotReceipt,
    Tenant,
)

settings = get_settings()
router = APIRouter(tags=["appliances"])

# In-memory fast path; the durable source of truth is the sha256 hash persisted
# on the Appliance row, so tokens survive cloud restarts.
_agent_tokens: dict[str, str] = {}  # token -> appliance_id


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _now():
    # Naive UTC to match SQLAlchemy DateTime columns returned tz-naive by Postgres.
    return datetime.now(timezone.utc).replace(tzinfo=None)


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


@fleet_router.post("/installer")
def appliance_installer(body: CreateLinkingCodeRequest,
                        principal: security.Principal = Depends(security.require_security_admin),
                        tenant: Tenant = Depends(security.get_tenant),
                        db: Session = Depends(get_db)):
    """Generate a linking code and return a single-line install command that a
    clean Ubuntu host can run to download, install, and register from the cloud."""
    code = f"CV-{secrets.token_hex(3).upper()}-{secrets.token_hex(3).upper()}"
    lc = LinkingCode(
        tenant_id=tenant.id, code=code, model=body.model, name=body.name,
        expires_at=_now() + timedelta(seconds=settings.linking_code_ttl_seconds),
    )
    db.add(lc)
    db.commit()
    audit.record(db, actor=principal.user_id, action="appliance.installer_created",
                 tenant_id=tenant.id, detail={"model": body.model})
    api = settings.api_base_url.rstrip("/")
    command = (
        f'curl -fsSL "{api}/appliance/bootstrap" -o /tmp/arkive-appliance.sh && '
        f'sudo CV_CLOUD_URL="{api}" CV_LINKING_CODE="{code}" bash /tmp/arkive-appliance.sh'
    )
    return {"code": code, "expires_at": lc.expires_at.isoformat(), "command": command}


@fleet_router.get("")
def list_appliances(tenant: Tenant = Depends(security.get_tenant),
                    db: Session = Depends(get_db)):
    rows = db.query(Appliance).filter(Appliance.tenant_id == tenant.id).all()
    return [_appliance_view(a) for a in rows]


@fleet_router.delete("/{appliance_id}")
def delete_appliance(appliance_id: str,
                     principal: security.Principal = Depends(security.require_security_admin),
                     tenant: Tenant = Depends(security.get_tenant),
                     db: Session = Depends(get_db)):
    """Remove an appliance from the fleet (e.g. a decommissioned or stale test
    unit). Dependent command rows are deleted and snapshot receipts are detached
    so recovery-point history is preserved."""
    a = db.get(Appliance, appliance_id)
    if not a or a.tenant_id != tenant.id:
        raise HTTPException(404, "appliance not found")
    db.query(ApplianceCommand).filter(ApplianceCommand.appliance_id == appliance_id).delete()
    db.query(ApplianceStorage).filter(ApplianceStorage.appliance_id == appliance_id).delete()
    (db.query(SnapshotReceipt)
     .filter(SnapshotReceipt.appliance_id == appliance_id)
     .update({SnapshotReceipt.appliance_id: None}))
    (db.query(LinkingCode)
     .filter(LinkingCode.appliance_id == appliance_id)
     .update({LinkingCode.appliance_id: None}))
    db.delete(a)
    db.commit()
    audit.record(db, actor=principal.user_id, action="appliance.removed",
                 tenant_id=tenant.id, resource=appliance_id,
                 detail={"serial": a.serial, "name": a.name})
    return {"ok": True}


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
    view["stored_data"] = _stored_data(db, a)
    return view


def _stored_data(db: Session, a: Appliance) -> dict:
    """Recovery points physically stored on this appliance, grouped by source."""
    from ..models import Collection, ConnectorAccount, ApplianceStorage as _AS  # noqa
    receipts = (db.query(SnapshotReceipt)
                .filter(SnapshotReceipt.appliance_id == a.id)
                .order_by(SnapshotReceipt.created_at.desc()).all())
    colls = {c.id: c for c in db.query(Collection)
             .filter(Collection.tenant_id == a.tenant_id).all()}
    stores = {f"store:{s.id}": s.name for s in db.query(ApplianceStorage)
              .filter(ApplianceStorage.appliance_id == a.id).all()}

    def _src(cid: str) -> str:
        c = colls.get(cid)
        if not c:
            return "unknown source"
        if c.connector_account_id:
            acc = db.get(ConnectorAccount, c.connector_account_id)
            if acc:
                return acc.account_label
        return c.name

    # Each snapshot is committed to <data_path>/protected/<snapshot_id> on disk.
    base = (a.telemetry or {}).get("data_path") or ""
    protected = f"{base.rstrip('/')}/protected" if base else "protected"
    items = [{
        "snapshot_id": r.snapshot_id,
        "source": _src(r.collection_id),
        "storage": stores.get(r.destination, "Built-In Storage"),
        "path": f"{protected}/{r.snapshot_id}",
        "object_count": r.object_count,
        "total_bytes": r.total_bytes,
        "recoverable": bool(r.recoverable),
        "at": r.created_at.isoformat(),
    } for r in receipts[:50]]
    return {
        "recovery_points": len(receipts),
        "objects": sum(r.object_count for r in receipts),
        "bytes": sum(r.total_bytes for r in receipts),
        "items": items,
    }


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
    from ..db import SessionLocal  # local import to avoid a cycle at module load
    stores = []
    db = SessionLocal()
    try:
        for s in (db.query(ApplianceStorage)
                  .filter(ApplianceStorage.appliance_id == a.id)
                  .order_by(ApplianceStorage.kind.desc(), ApplianceStorage.created_at.asc()).all()):
            cap = s.capacity_bytes or 0
            used = s.used_bytes or 0
            health = s.health or {}
            # Fallback: if the storage record hasn't been synced from a heartbeat
            # yet, show the appliance's reported capacity for the built-in volume.
            if s.kind == "builtin" and not cap:
                cap = int(tel.get("capacity_total_bytes") or 0)
                used = int(tel.get("capacity_used_bytes") or 0)
                if not health:
                    health = {"drive_health": tel.get("drive_health", "healthy"),
                              "temperature_c": tel.get("temperature_c")}
            stores.append({"id": s.id, "name": s.name, "kind": s.kind,
                           "capacity_bytes": cap, "used_bytes": used,
                           "free_bytes": max(cap - used, 0),
                           "path": tel.get("data_path") if s.kind == "builtin" else None,
                           "mount": tel.get("data_mount") if s.kind == "builtin" else None,
                           "health": health})
    finally:
        db.close()
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
        "stores": stores,
    }


def _ensure_builtin_store(db: Session, a: Appliance) -> ApplianceStorage:
    store = (db.query(ApplianceStorage)
             .filter(ApplianceStorage.appliance_id == a.id,
                     ApplianceStorage.kind == "builtin").first())
    if not store:
        store = ApplianceStorage(tenant_id=a.tenant_id, appliance_id=a.id,
                                 name="Built-In Storage", kind="builtin")
        db.add(store)
        db.commit()
        db.refresh(store)
    return store


def _sync_storage_telemetry(db: Session, a: Appliance, telemetry: dict) -> None:
    """Reflect the appliance's reported per-volume capacity + health onto its
    ApplianceStorage rows so each storage element shows its own usage/health."""
    reported = telemetry.get("storages")
    builtin = _ensure_builtin_store(db, a)
    if not reported:
        # Older agent: map the appliance-wide capacity onto the built-in store.
        builtin.capacity_bytes = int(telemetry.get("capacity_total_bytes") or 0)
        builtin.used_bytes = int(telemetry.get("capacity_used_bytes") or 0)
        builtin.health = {"drive_health": telemetry.get("drive_health", "healthy"),
                          "temperature_c": telemetry.get("temperature_c")}
        db.commit()
        return
    existing = {s.name: s for s in (db.query(ApplianceStorage)
                                    .filter(ApplianceStorage.appliance_id == a.id).all())}
    for rep in reported:
        name = rep.get("name") or "Storage"
        s = existing.get(name)
        if not s:
            # Prefer updating the built-in row for the primary volume.
            s = builtin if rep.get("kind") == "builtin" else None
        if not s:
            s = ApplianceStorage(tenant_id=a.tenant_id, appliance_id=a.id,
                                 name=name, kind=rep.get("kind", "external"))
            db.add(s)
        s.capacity_bytes = int(rep.get("capacity_bytes") or 0)
        s.used_bytes = int(rep.get("used_bytes") or 0)
        s.health = rep.get("health") or {}
    db.commit()


class RenameApplianceRequest(BaseModel):
    name: str | None = None
    location_label: str | None = None


@fleet_router.put("/{appliance_id}")
def rename_appliance(appliance_id: str, body: RenameApplianceRequest,
                     principal: security.Principal = Depends(security.require_security_admin),
                     tenant: Tenant = Depends(security.get_tenant),
                     db: Session = Depends(get_db)):
    a = db.get(Appliance, appliance_id)
    if not a or a.tenant_id != tenant.id:
        raise HTTPException(404, "appliance not found")
    if body.name is not None:
        a.name = body.name.strip() or a.name
    if body.location_label is not None:
        a.location_label = body.location_label
    db.commit()
    audit.record(db, actor=principal.user_id, action="appliance.renamed",
                 tenant_id=tenant.id, resource=a.id, detail={"name": a.name})
    return _appliance_view(a)


class StorageRequest(BaseModel):
    name: str
    kind: str = "external"  # external | builtin


@fleet_router.post("/{appliance_id}/storage")
def add_storage(appliance_id: str, body: StorageRequest,
                principal: security.Principal = Depends(security.require_security_admin),
                tenant: Tenant = Depends(security.get_tenant),
                db: Session = Depends(get_db)):
    a = db.get(Appliance, appliance_id)
    if not a or a.tenant_id != tenant.id:
        raise HTTPException(404, "appliance not found")
    s = ApplianceStorage(tenant_id=tenant.id, appliance_id=a.id,
                         name=body.name.strip() or "Storage",
                         kind="builtin" if body.kind == "builtin" else "external")
    db.add(s)
    db.commit()
    db.refresh(s)
    audit.record(db, actor=principal.user_id, action="appliance.storage_added",
                 tenant_id=tenant.id, resource=a.id, detail={"storage": s.name})
    return {"id": s.id, "name": s.name, "kind": s.kind}


@fleet_router.put("/{appliance_id}/storage/{storage_id}")
def rename_storage(appliance_id: str, storage_id: str, body: StorageRequest,
                   principal: security.Principal = Depends(security.require_security_admin),
                   tenant: Tenant = Depends(security.get_tenant),
                   db: Session = Depends(get_db)):
    s = db.get(ApplianceStorage, storage_id)
    if not s or s.tenant_id != tenant.id or s.appliance_id != appliance_id:
        raise HTTPException(404, "storage not found")
    s.name = body.name.strip() or s.name
    db.commit()
    audit.record(db, actor=principal.user_id, action="appliance.storage_renamed",
                 tenant_id=tenant.id, resource=appliance_id, detail={"storage": s.name})
    return {"id": s.id, "name": s.name, "kind": s.kind}


@fleet_router.delete("/{appliance_id}/storage/{storage_id}")
def delete_storage(appliance_id: str, storage_id: str,
                   principal: security.Principal = Depends(security.require_security_admin),
                   tenant: Tenant = Depends(security.get_tenant),
                   db: Session = Depends(get_db)):
    s = db.get(ApplianceStorage, storage_id)
    if not s or s.tenant_id != tenant.id or s.appliance_id != appliance_id:
        raise HTTPException(404, "storage not found")
    if s.kind == "builtin":
        raise HTTPException(400, "the built-in storage cannot be removed")
    db.delete(s)
    db.commit()
    audit.record(db, actor=principal.user_id, action="appliance.storage_removed",
                 tenant_id=tenant.id, resource=appliance_id, detail={"storage": s.name})
    return {"ok": True}


# --------------------------------------------------------------------------
# Agent-facing management plane (outbound-only from the appliance)
# --------------------------------------------------------------------------

agent_router = APIRouter(prefix="/appliance", tags=["appliance-agent"])


_BUNDLE_DIRS = ("appliance", "shared")
_BUNDLE_EXCLUDE = (".venv", "__pycache__", "node_modules", ".git", ".pyc", "web/dist")
# Only the specific installer/infra/updater files an appliance needs — never the
# cloud installer, Caddy config, cloud services, or the desktop-agent installers.
_BUNDLE_FILES = (
    "installers/lib.sh",
    "installers/appliance-install.sh",
    "installers/appliance-bootstrap.sh",
    "installers/appliance-update.sh",
    "infra/systemd/cv-appliance-agent.service",
    "infra/systemd/cv-appliance-selfupdate.service",
    "infra/systemd/cv-appliance-selfupdate.timer",
    "updater/appliance-update.sh",
)

_appliance_version_cache: str | None = None


def _appliance_bundle_version() -> str:
    """Stable content hash of the appliance bundle; changes only when code changes,
    so the headless self-update timer only re-installs on real updates."""
    global _appliance_version_cache
    if _appliance_version_cache:
        return _appliance_version_cache
    root = _repo_root()
    h = hashlib.sha256()
    for d in ("appliance", "shared"):
        base = root / d
        if not base.exists():
            continue
        for f in sorted(base.rglob("*")):
            if f.is_file() and not any(x in str(f) for x in _BUNDLE_EXCLUDE) \
                    and f.name != "VERSION":
                h.update(str(f.relative_to(root)).encode())
                try:
                    h.update(f.read_bytes())
                except Exception:
                    pass
    _appliance_version_cache = h.hexdigest()[:12]
    return _appliance_version_cache


@agent_router.get("/bootstrap")
def appliance_bootstrap():
    """Serve the appliance cloud bootstrap installer (no auth — open code)."""
    path = _repo_root() / "installers" / "appliance-bootstrap.sh"
    try:
        return PlainTextResponse(path.read_text())
    except Exception:
        raise HTTPException(404, "bootstrap unavailable")


@agent_router.get("/bundle")
def appliance_bundle():
    """Serve the appliance install bundle: only appliance + shared code plus the
    specific appliance installer/service files (no cloud code)."""
    root = _repo_root()
    buf = io.BytesIO()

    def _filter(ti: tarfile.TarInfo):
        return None if any(x in ti.name for x in _BUNDLE_EXCLUDE) else ti

    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for d in _BUNDLE_DIRS:
            p = root / d
            if p.exists():
                tar.add(str(p), arcname=d, filter=_filter)
        for f in _BUNDLE_FILES:
            p = root / f
            if p.exists():
                tar.add(str(p), arcname=f)
        # Stamp a build version so the appliance can detect when to self-update.
        version = _appliance_bundle_version().encode()
        vi = tarfile.TarInfo("appliance/VERSION")
        vi.size = len(version)
        tar.addfile(vi, io.BytesIO(version))
    return Response(content=buf.getvalue(), media_type="application/gzip",
                    headers={"Content-Disposition": "attachment; filename=arkive-appliance.tar.gz"})


def _agent_appliance(authorization: str = Header(default=""),
                     db: Session = Depends(get_db)) -> Appliance:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing appliance token")
    token = authorization.split(" ", 1)[1]
    a = None
    appliance_id = _agent_tokens.get(token)
    if appliance_id:
        a = db.get(Appliance, appliance_id)
    if a is None:
        # Fall back to the durable hash so tokens survive cloud restarts.
        a = db.query(Appliance).filter(
            Appliance.agent_token_hash == _hash_token(token)).first()
        if a:
            _agent_tokens[token] = a.id
    if not a:
        raise HTTPException(401, "invalid appliance token")
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

    # Re-activating the same physical unit (same serial) updates the existing
    # record instead of creating a duplicate, so re-installs don't leave stale
    # appliances lingering in the fleet.
    appliance = (db.query(Appliance)
                 .filter(Appliance.tenant_id == lc.tenant_id,
                         Appliance.serial == body.serial).first())
    if appliance:
        appliance.model = body.model or lc.model
        appliance.name = lc.name
        appliance.state = "ONLINE_STAGING"
        appliance.isolation_state = "sealed"
        appliance.identity_bundle = body.identity_bundle
        appliance.attestation_ok = True
        appliance.last_attestation_at = _now()
        appliance.software_version = "1.0.0"
    else:
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
    db.flush()
    lc.appliance_id = appliance.id
    db.commit()
    db.refresh(appliance)

    # Every appliance has at least a built-in storage object that mappings target.
    _ensure_builtin_store(db, appliance)

    token = secrets.token_urlsafe(32)
    appliance.agent_token_hash = _hash_token(token)
    db.commit()
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
    # Attestation: hardware appliances require secure-boot; VM models attest on
    # tamper-state only (no firmware secure-boot measurement to verify).
    model_kind = (body.telemetry or {}).get("model_kind", "hardware")
    secure = bool(body.attestation.get("secure_boot"))
    appliance.attestation_ok = body.tamper_state == "normal" and \
        (secure or model_kind == "vm")
    if not appliance.attestation_ok and appliance.state != "QUARANTINED":
        appliance.state = "QUARANTINED"
    db.commit()

    _sync_storage_telemetry(db, appliance, body.telemetry or {})

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
            "latest_version": _appliance_bundle_version(),
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
    storage_id: str | None = None  # the appliance storage the objects landed in


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
    # The sync worker already recorded a not-yet-recoverable receipt for this
    # appliance+snapshot (destination store:<id>). Mark that one recoverable
    # rather than inserting a second, differently-labelled record.
    existing = (db.query(SnapshotReceipt)
                .filter(SnapshotReceipt.snapshot_id == body.snapshot_id,
                        SnapshotReceipt.appliance_id == appliance.id)
                .order_by(SnapshotReceipt.created_at.desc()).first())
    if existing:
        existing.recoverable = ok
        existing.manifest_hash = body.manifest_hash
        existing.receipt = body.receipt
        existing.object_count = body.object_count or existing.object_count
        existing.total_bytes = body.total_bytes or existing.total_bytes
    else:
        destination = f"store:{body.storage_id}" if body.storage_id else "appliance"
        db.add(SnapshotReceipt(
            tenant_id=appliance.tenant_id,
            appliance_id=appliance.id,
            vault_id=body.vault_id,
            collection_id=body.collection_id,
            snapshot_id=body.snapshot_id,
            destination=destination,
            object_count=body.object_count,
            total_bytes=body.total_bytes,
            manifest_hash=body.manifest_hash,
            recoverable=ok,
            receipt=body.receipt,
        ))
    db.commit()
    audit.record(db, actor=f"appliance:{appliance.serial}",
                 action="appliance.snapshot_sealed", tenant_id=appliance.tenant_id,
                 resource=body.snapshot_id, detail={"verified": ok})
    return {"recoverable": ok, "snapshot_id": body.snapshot_id}
