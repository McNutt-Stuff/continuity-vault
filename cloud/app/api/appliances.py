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
import time
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response, FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from cv_crypto.signing import SigPolicy

from .. import audit, fleet, security
from ..config import get_settings
from ..db import get_db
from ..models import (
    Appliance,
    ApplianceAssignment,
    ApplianceCommand,
    ApplianceStorage,
    AuditEvent,
    LinkingCode,
    PendingAppliance,
    SnapshotReceipt,
    Tenant,
)

settings = get_settings()
router = APIRouter(tags=["appliances"])
logger = logging.getLogger("cv.appliances")

_TB = 1024 ** 4

# OEM hardware signatures the Arkive appliance ships on (lowercased). We present
# these under the Arkive brand in the UI rather than the ODM's DMI strings.
_ARKIVE_OEM_VENDORS = {"azw", "arkive", "beelink"}


def _brand_telemetry(tel: dict, model: str) -> dict:
    """Rebrand raw DMI hardware strings (e.g. vendor 'AZW', product 'ME Pro') to
    the Arkive product line for display — vendor 'Arkive', product the CV Edge
    model — keeping the originals in *_raw. Only real hardware is rebranded."""
    if tel.get("model_kind") == "hardware":
        tel["hardware_vendor_raw"] = tel.get("hardware_vendor") or ""
        tel["hardware_product_raw"] = tel.get("hardware_product") or ""
        tel["hardware_vendor"] = "Arkive"
        tel["hardware_product"] = model or tel.get("hardware_product") or "CV Edge"
    return tel


def _model_for_capacity(db: Session, total_bytes: int, fallback: str) -> str:
    """Map the dedicated-storage capacity to the nearest CV Edge model from the
    platform pricing tiers (e.g. ~1-2 TB → CV Edge 1, ~4-5 TB → CV Edge 5)."""
    if not total_bytes:
        return fallback
    from .billing import get_pricing
    tiers = get_pricing(db).appliance_tiers or []
    avail_tb = total_bytes / _TB
    best = None  # (distance, capacity_tb, model)
    for t in tiers:
        model = t.get("model")
        if not model:
            continue
        cap = float(t.get("capacity_tb") or 0)
        d = abs(cap - avail_tb)
        if best is None or d < best[0] or (d == best[0] and cap > best[1]):
            best = (d, cap, model)
    return best[2] if best else fallback

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
                        principal: security.Principal = Depends(security.get_principal),
                        tenant: Tenant = Depends(security.get_tenant),
                        db: Session = Depends(get_db)):
    _require_appliance_manager(principal, tenant)
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
                        principal: security.Principal = Depends(security.get_principal),
                        tenant: Tenant = Depends(security.get_tenant),
                        db: Session = Depends(get_db)):
    """Generate a linking code and return a single-line install command that a
    clean Ubuntu host can run to download, install, and register from the cloud."""
    _require_appliance_manager(principal, tenant)
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


class PairRequest(BaseModel):
    code: str
    name: str | None = None


def _require_appliance_manager(principal: security.Principal, tenant: Tenant) -> None:
    """Org admins manage the whole fleet; a personal (shared-tenant) account
    manages its own appliances. Everyone else is read-only."""
    if security.is_org_admin(principal.role) or principal.is_platform_admin:
        return
    if (tenant.tenant_type or "dedicated") == "shared":
        return
    raise HTTPException(403, "not allowed to add appliances")


def _require_can_manage_appliance(principal: security.Principal, tenant: Tenant,
                                  appliance_id: str, db: Session) -> None:
    """Org admins manage the whole fleet; a personal (shared-tenant) account and
    any member the appliance was assigned to with can_manage may rename, command,
    and adjust storage on that appliance. Everyone else is read-only."""
    if security.is_org_admin(principal.role) or principal.is_platform_admin:
        return
    if (tenant.tenant_type or "dedicated") == "shared":
        return
    asn = (db.query(ApplianceAssignment)
           .filter(ApplianceAssignment.tenant_id == tenant.id,
                   ApplianceAssignment.appliance_id == appliance_id,
                   ApplianceAssignment.user_id == principal.user_id).first())
    if asn and asn.can_manage:
        return
    raise HTTPException(403, "you don't have permission to manage this appliance")


@fleet_router.post("/pair")
def pair_appliance(body: PairRequest,
                   principal: security.Principal = Depends(security.get_principal),
                   tenant: Tenant = Depends(security.get_tenant),
                   db: Session = Depends(get_db)):
    """Claim a zero-touch appliance to this account using the pairing code shown on
    the appliance's local web UI. Creates the real Appliance; the agent adopts it
    on its next register-heartbeat."""
    _require_appliance_manager(principal, tenant)
    code = (body.code or "").strip().upper()
    if not code:
        raise HTTPException(400, "enter the pairing code from the appliance screen")
    pa = db.query(PendingAppliance).filter(PendingAppliance.pairing_code == code).first()
    if not pa:
        raise HTTPException(404, "no appliance found for that pairing code")
    if pa.paired_appliance_id:
        raise HTTPException(400, "that appliance has already been paired")
    a = (db.query(Appliance)
         .filter(Appliance.tenant_id == tenant.id, Appliance.serial == pa.serial).first())
    if a is None:
        a = Appliance(tenant_id=tenant.id, serial=pa.serial, model=pa.model,
                      name=(body.name or "Home Appliance"), state="ONLINE_STAGING",
                      isolation_state="sealed", identity_bundle=pa.identity_bundle,
                      attestation_ok=True, last_attestation_at=_now(),
                      software_version="1.0.0", telemetry=pa.telemetry or {})
        db.add(a)
    db.flush()
    pa.paired_appliance_id = a.id
    pa.paired_tenant_id = tenant.id
    # A non-admin (personal account / member) owns what they pair, so they can see
    # and manage it — org admins already see the whole fleet.
    if not (security.is_org_admin(principal.role) or principal.is_platform_admin):
        exists = (db.query(ApplianceAssignment)
                  .filter(ApplianceAssignment.appliance_id == a.id,
                          ApplianceAssignment.user_id == principal.user_id).first())
        if not exists:
            db.add(ApplianceAssignment(tenant_id=tenant.id, appliance_id=a.id,
                                       user_id=principal.user_id, can_manage=True))
    db.commit()
    db.refresh(a)
    _ensure_builtin_store(db, a)
    audit.record(db, actor=principal.user_id, action="appliance.paired",
                 tenant_id=tenant.id, resource=a.id, detail={"serial": pa.serial})
    return _appliance_view(a)


@fleet_router.get("")
def list_appliances(principal: security.Principal = Depends(security.get_principal),
                    tenant: Tenant = Depends(security.get_tenant),
                    db: Session = Depends(get_db)):
    admin = security.is_org_admin(principal.role) or principal.is_platform_admin
    rows = db.query(Appliance).filter(Appliance.tenant_id == tenant.id).all()
    # A standard member sees only appliances assigned to them (view-only); an org
    # admin sees the whole fleet and can make changes.
    manage = {}
    if not admin:
        for asn in (db.query(ApplianceAssignment)
                    .filter(ApplianceAssignment.tenant_id == tenant.id,
                            ApplianceAssignment.user_id == principal.user_id).all()):
            manage[asn.appliance_id] = bool(asn.can_manage)
        rows = [a for a in rows if a.id in manage]
    out = []
    for a in rows:
        v = _appliance_view(a)
        v["can_manage"] = admin or manage.get(a.id, False)
        v["view_only"] = not v["can_manage"]
        out.append(v)
    return out


@fleet_router.get("/versions")
def appliance_versions(tenant: Tenant = Depends(security.get_tenant)):
    """The production appliance software version the control plane currently serves."""
    from .. import versions
    return {"production_version": versions.appliance_production_version()}


@fleet_router.delete("/{appliance_id}")
def delete_appliance(appliance_id: str,
                     principal: security.Principal = Depends(security.get_principal),
                     tenant: Tenant = Depends(security.get_tenant),
                     db: Session = Depends(get_db)):
    """Remove an appliance from the fleet (e.g. a decommissioned or stale test
    unit). Dependent command rows are deleted and snapshot receipts are detached
    so recovery-point history is preserved."""
    a = db.get(Appliance, appliance_id)
    if not a or a.tenant_id != tenant.id:
        raise HTTPException(404, "appliance not found")
    _require_can_manage_appliance(principal, tenant, appliance_id, db)
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
                  principal: security.Principal = Depends(security.get_principal),
                  tenant: Tenant = Depends(security.get_tenant),
                  db: Session = Depends(get_db)):
    a = db.get(Appliance, appliance_id)
    if not a or a.tenant_id != tenant.id:
        raise HTTPException(404, "appliance not found")
    admin = security.is_org_admin(principal.role) or principal.is_platform_admin
    assignment = None
    if not admin:
        assignment = (db.query(ApplianceAssignment)
                      .filter(ApplianceAssignment.appliance_id == a.id,
                              ApplianceAssignment.user_id == principal.user_id).first())
        if not assignment:
            raise HTTPException(404, "appliance not found")
    commands = (db.query(ApplianceCommand.command_type, ApplianceCommand.status,
                         ApplianceCommand.sequence, ApplianceCommand.created_at)
                .filter(ApplianceCommand.appliance_id == a.id)
                .order_by(ApplianceCommand.created_at.desc()).limit(20).all())
    view = _appliance_view(a)
    view["can_manage"] = admin or bool(assignment and assignment.can_manage)
    view["view_only"] = not view["can_manage"]
    view["recent_commands"] = [{"type": c.command_type, "status": c.status,
                                "sequence": c.sequence,
                                "created_at": c.created_at.isoformat()} for c in commands]
    # A member only sees data belonging to their own vaults on a shared appliance.
    allowed = None if admin else security.content_vault_ids(db, principal)
    view["stored_data"] = _stored_data(db, a, allowed_vault_ids=allowed)
    # Integrations running on this appliance (status surfaced in the portal).
    from ..models import IntegrationInstance
    integs = (db.query(IntegrationInstance)
              .filter(IntegrationInstance.appliance_id == a.id)
              .order_by(IntegrationInstance.created_at.desc()).all())
    if not admin:
        integs = [i for i in integs if i.owner_user_id == principal.user_id]
    view["integrations"] = [{
        "id": i.id, "integration_type": i.integration_type,
        "label": i.label or i.integration_type, "enabled": i.enabled,
        "status": i.status, "poll_interval_minutes": i.poll_interval_minutes,
        "last_run_at": i.last_run_at.isoformat() if i.last_run_at else None,
        "last_error": i.last_error, "last_stats": i.last_stats or {},
    } for i in integs]
    return view


def _stored_data(db: Session, a: Appliance, allowed_vault_ids: list[str] | None = None) -> dict:
    """Recovery points physically stored on this appliance, grouped by source.
    When ``allowed_vault_ids`` is given (a standard member), only that member's
    own vaults are shown — never another member's sources on a shared appliance."""
    from ..models import Collection, ConnectorAccount, Vault, ApplianceStorage as _AS  # noqa
    if allowed_vault_ids is not None and not allowed_vault_ids:
        return {"recovery_points": 0, "objects": 0, "bytes": 0, "sources": [], "items": []}
    rq = (db.query(SnapshotReceipt)
          .filter(SnapshotReceipt.appliance_id == a.id))
    if allowed_vault_ids is not None:
        rq = rq.filter(SnapshotReceipt.vault_id.in_(allowed_vault_ids))
    receipts = rq.order_by(SnapshotReceipt.created_at.desc()).all()
    colls = {c.id: c for c in db.query(Collection)
             .filter(Collection.tenant_id == a.tenant_id).all()}
    vaults = {v.id: v.name for v in db.query(Vault)
              .filter(Vault.tenant_id == a.tenant_id).all()}
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

    def _srcuser(cid: str) -> str | None:
        c = colls.get(cid)
        if c and c.connector_account_id:
            acc = db.get(ConnectorAccount, c.connector_account_id)
            if acc:
                return acc.account_username
        return None

    # Each snapshot is committed to <data_path>/protected/<snapshot_id> on disk.
    base = (a.telemetry or {}).get("data_path") or ""
    protected = f"{base.rstrip('/')}/protected" if base else "protected"
    items = [{
        "snapshot_id": r.snapshot_id,
        "source": _src(r.collection_id),
        "source_username": _srcuser(r.collection_id),
        "storage": stores.get(r.destination, "Built-In Storage"),
        "path": f"{protected}/{r.snapshot_id}",
        "object_count": r.object_count,
        "total_bytes": r.total_bytes,
        "recoverable": bool(r.recoverable),
        "at": r.created_at.isoformat(),
    } for r in receipts[:50]]

    # Summarise by source→vault so the UI shows one polished row per source
    # (total objects, recovery points, bytes, storage) instead of one per snapshot.
    summary: dict[str, dict] = {}
    for r in receipts:
        c = colls.get(r.collection_id)
        source = _src(r.collection_id)
        vault = vaults.get(c.vault_id, "—") if c else "—"
        source_type = c.source_type if c else "custom"
        key = f"{source}\u241f{vault}"
        s = summary.get(key)
        if s is None:
            s = {"source": source, "source_username": _srcuser(r.collection_id),
                 "vault": vault, "source_type": source_type,
                 "recovery_points": 0, "objects": 0, "bytes": 0,
                 "recoverable": 0, "storage": stores.get(r.destination, "Built-In Storage"),
                 "last_at": r.created_at.isoformat()}
            summary[key] = s
        s["recovery_points"] += 1
        s["objects"] += r.object_count or 0
        s["bytes"] += r.total_bytes or 0
        if r.recoverable:
            s["recoverable"] += 1
        if r.created_at.isoformat() > s["last_at"]:
            s["last_at"] = r.created_at.isoformat()

    return {
        "recovery_points": len(receipts),
        "objects": sum(r.object_count for r in receipts),
        "bytes": sum(r.total_bytes for r in receipts),
        "sources": sorted(summary.values(), key=lambda x: x["bytes"], reverse=True),
        "items": items,
    }


class CommandRequest(BaseModel):
    command_type: str
    parameters: dict = {}


@fleet_router.post("/{appliance_id}/command")
def send_command(appliance_id: str, body: CommandRequest,
                 principal: security.Principal = Depends(security.get_principal),
                 tenant: Tenant = Depends(security.get_tenant),
                 db: Session = Depends(get_db)):
    a = db.get(Appliance, appliance_id)
    if not a or a.tenant_id != tenant.id:
        raise HTTPException(404, "appliance not found")
    _require_can_manage_appliance(principal, tenant, appliance_id, db)
    # Prohibited unilateral operations are simply not exposed here (spec 5.1).
    cmd = fleet.issue_command(db, a, body.command_type, principal.user_id, body.parameters)
    audit.record(db, actor=principal.user_id, action="appliance.command_issued",
                 tenant_id=tenant.id, resource=a.id,
                 detail={"type": body.command_type, "sequence": cmd.sequence})
    return {"command_id": cmd.id, "sequence": cmd.sequence, "status": cmd.status}


def _appliance_view(a: Appliance) -> dict:
    tel = a.telemetry or {}
    # The vault's actual volume kind (dedicated /arkive RAID vs. built-in system
    # disk on VMs). Used to keep the appliance-wide capacity fallback attached to
    # the right row, and to hide the built-in disk on hardware.
    is_hardware = (tel.get("model_kind") == "hardware")
    reported_kind = tel.get("storage_kind")
    from ..db import SessionLocal  # local import to avoid a cycle at module load
    stores = []
    db = SessionLocal()
    try:
        rows = (db.query(ApplianceStorage)
                .filter(ApplianceStorage.appliance_id == a.id)
                .order_by(ApplianceStorage.kind.desc(), ApplianceStorage.created_at.asc()).all())
        # Built-in (system disk) is only a real backup volume on VMs with no
        # dedicated storage. Hide it whenever this appliance has dedicated storage
        # (a dedicated store row, or the agent reports its vault on one) or is
        # hardware — dedicated is the only customer-usable volume there. The system
        # disk's own stats are retained separately via telemetry.os_storage.
        has_dedicated = any(s.kind == "dedicated" for s in rows)
        hide_builtin = is_hardware or has_dedicated or (reported_kind == "dedicated")
        for s in rows:
            if s.kind == "builtin" and hide_builtin:
                continue
            cap = s.capacity_bytes or 0
            used = s.used_bytes or 0
            health = s.health or {}
            primary_row = s.kind in ("builtin", "dedicated")
            # Only THE vault's volume (matched by the reported storage_kind) borrows
            # the appliance-wide capacity as a fallback, so a stale/duplicate primary
            # row can't inherit the dedicated RAID's (all-drives) total. Older agents
            # don't report storage_kind — fall back to the legacy primary-row match.
            is_vault_volume = primary_row and (not reported_kind or s.kind == reported_kind)
            if is_vault_volume and not cap:
                cap = int(tel.get("capacity_total_bytes") or 0)
                used = int(tel.get("capacity_used_bytes") or 0)
                if not health:
                    health = {"drive_health": tel.get("drive_health", "healthy"),
                              "temperature_c": tel.get("temperature_c")}
            stores.append({"id": s.id, "name": s.name, "kind": s.kind,
                           "state": s.state or "ready",
                           "mirror_of_id": s.mirror_of_id,
                           "connected": (s.state or "ready") not in ("disconnected", "provisioning"),
                           "device_serial": s.device_serial or "",
                           "capacity_bytes": cap, "used_bytes": used,
                           "free_bytes": max(cap - used, 0),
                           "path": tel.get("data_path") if is_vault_volume else None,
                           "mount": tel.get("data_mount") if is_vault_volume else None,
                           "health": health})
    finally:
        db.close()
    # Removable drives the appliance detected that aren't yet set up as Arkive
    # storage — surfaced so the user can start the (destructive) setup flow. Drives
    # already adopted (is_arkive with a known store) are filtered out.
    known_serials = {s.get("device_serial") for s in stores if s.get("device_serial")}
    detected = []
    for dev in (tel.get("external_devices") or []):
        if dev.get("serial") and dev.get("serial") in known_serials:
            continue
        detected.append({
            "device": dev.get("device"), "serial": dev.get("serial") or "",
            "model": dev.get("model") or "", "vendor": dev.get("vendor") or "",
            "size_bytes": int(dev.get("size_bytes") or 0),
            "transport": dev.get("transport") or "",
            "has_filesystem": bool(dev.get("has_filesystem")),
            "fstype": dev.get("fstype") or "", "label": dev.get("label") or "",
            "is_arkive": bool(dev.get("is_arkive")),
        })
    return {
        "id": a.id,
        "serial": a.serial,
        "model": a.model,
        "name": a.name,
        "location_label": a.location_label,
        "state": a.state,
        "isolation_state": a.isolation_state,
        "software_version": a.software_version,
        "production_version": _appliance_bundle_version(),
        "update_available": bool(a.software_version and a.software_version != "0.0.0"
                                 and a.software_version != _appliance_bundle_version()),
        "version_updated_at": a.version_updated_at.isoformat() if a.version_updated_at else None,
        "attestation_ok": a.attestation_ok,
        "tamper_state": a.tamper_state,
        "last_heartbeat_at": a.last_heartbeat_at.isoformat() if a.last_heartbeat_at else None,
        "last_attestation_at": a.last_attestation_at.isoformat() if a.last_attestation_at else None,
        "telemetry": tel,
        "stores": stores,
        "detected_storage": detected,
    }


def _ensure_builtin_store(db: Session, a: Appliance) -> ApplianceStorage:
    # The appliance's primary volume — a dedicated Arkive RAID disk when present,
    # else the built-in system-disk store (VMs). Only one primary row exists.
    store = (db.query(ApplianceStorage)
             .filter(ApplianceStorage.appliance_id == a.id,
                     ApplianceStorage.kind.in_(["builtin", "dedicated"]))
             .order_by(ApplianceStorage.kind.desc()).first())
    if not store:
        store = ApplianceStorage(tenant_id=a.tenant_id, appliance_id=a.id,
                                 name="Built-In Storage", kind="builtin")
        db.add(store)
        db.commit()
        db.refresh(store)
    return store


def _sync_storage_telemetry(db: Session, a: Appliance, telemetry: dict) -> None:
    """Reflect the appliance's reported per-volume capacity + health onto its
    ApplianceStorage rows so each storage element shows its own usage/health.

    Primary-volume rows (built-in/dedicated) carry no store_id; external and mirror
    rows are keyed by their stable id (== the ApplianceStorage id written to the
    drive's marker at setup). Arkive-managed drives not present this heartbeat are
    flagged disconnected so the UI shows their status."""
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
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    seen_external: set[str] = set()
    existing = {s.name: s for s in (db.query(ApplianceStorage)
                                    .filter(ApplianceStorage.appliance_id == a.id).all())}
    for rep in reported:
        sid = rep.get("store_id")
        kind = rep.get("kind", "external")
        if sid:
            # External / mirror store keyed by its stable id (created at setup).
            s = db.get(ApplianceStorage, sid)
            if not s:
                # Adopt a store the appliance set up locally (e.g. via cvtool) that
                # the cloud hasn't seen yet — key the row by the reported store id.
                if kind not in ("external", "mirror"):
                    continue
                s = ApplianceStorage(id=sid, tenant_id=a.tenant_id, appliance_id=a.id,
                                     name=rep.get("name") or "External Storage", kind=kind)
                db.add(s)
            elif s.appliance_id != a.id:
                continue
            seen_external.add(sid)
            connected = bool(rep.get("connected", True))
            s.capacity_bytes = int(rep.get("capacity_bytes") or 0)
            s.used_bytes = int(rep.get("used_bytes") or 0)
            s.health = rep.get("health") or {}
            if rep.get("device_serial"):
                s.device_serial = rep.get("device_serial")
            if rep.get("mirror_of_id") is not None:
                s.mirror_of_id = rep.get("mirror_of_id")
            # Leave a provisioning/errored store alone until the drive reports ready.
            if connected:
                s.state = "ready"
                s.last_seen_at = now
            elif s.state not in ("provisioning", "error"):
                s.state = "disconnected"
            continue
        # Primary volume (no store_id): repurpose the single built-in/dedicated row
        # so existing store:<id> mappings keep resolving to the same object.
        name = rep.get("name") or "Storage"
        s = existing.get(name)
        if not s and kind in ("builtin", "dedicated"):
            s = builtin
            s.name = name
            s.kind = kind
        if not s:
            s = ApplianceStorage(tenant_id=a.tenant_id, appliance_id=a.id,
                                 name=name, kind=kind)
            db.add(s)
        s.capacity_bytes = int(rep.get("capacity_bytes") or 0)
        s.used_bytes = int(rep.get("used_bytes") or 0)
        s.health = rep.get("health") or {}
        s.state = "ready"
    # Arkive-managed external/mirror stores not reported this heartbeat are offline.
    for s in (db.query(ApplianceStorage)
              .filter(ApplianceStorage.appliance_id == a.id,
                      ApplianceStorage.kind.in_(["external", "mirror"])).all()):
        if s.id not in seen_external and s.state not in ("provisioning", "error"):
            s.state = "disconnected"
            h = dict(s.health or {})
            h["drive_health"] = "disconnected"
            s.health = h
    db.commit()


def _notify_detected_storage(db: Session, a: Appliance, telemetry: dict) -> None:
    """Raise a one-time operator alert when a brand-new removable drive appears on
    an appliance (surfaced on the Overview alerts feed + the Appliances page). Own
    (already-adopted) drives and drives we've already flagged are skipped."""
    devices = telemetry.get("external_devices") or []
    if not devices:
        return
    own_serials = {ser for (ser,) in
                   db.query(ApplianceStorage.device_serial)
                   .filter(ApplianceStorage.appliance_id == a.id).all() if ser}
    # Serials we already alerted on (dedupe across heartbeats).
    recent = (db.query(AuditEvent)
              .filter(AuditEvent.tenant_id == a.tenant_id,
                      AuditEvent.action == "appliance.storage_detected",
                      AuditEvent.resource == a.id)
              .order_by(AuditEvent.created_at.desc()).limit(50).all())
    alerted = {(e.detail or {}).get("serial") for e in recent}
    for dev in devices:
        serial = dev.get("serial")
        if not serial or serial in own_serials or serial in alerted:
            continue
        label = (dev.get("model") or dev.get("vendor") or "External drive").strip()
        audit.record(db, actor="system", action="appliance.storage_detected",
                     tenant_id=a.tenant_id, resource=a.id, category="storage",
                     severity="warning",
                     detail={"serial": serial, "device": dev.get("device"),
                             "model": label, "size_bytes": dev.get("size_bytes"),
                             "appliance_name": a.name, "appliance_id": a.id,
                             "is_arkive": bool(dev.get("is_arkive"))})


class RenameApplianceRequest(BaseModel):
    name: str | None = None
    location_label: str | None = None


@fleet_router.put("/{appliance_id}")
def rename_appliance(appliance_id: str, body: RenameApplianceRequest,
                     principal: security.Principal = Depends(security.get_principal),
                     tenant: Tenant = Depends(security.get_tenant),
                     db: Session = Depends(get_db)):
    a = db.get(Appliance, appliance_id)
    if not a or a.tenant_id != tenant.id:
        raise HTTPException(404, "appliance not found")
    _require_can_manage_appliance(principal, tenant, appliance_id, db)
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
                principal: security.Principal = Depends(security.get_principal),
                tenant: Tenant = Depends(security.get_tenant),
                db: Session = Depends(get_db)):
    a = db.get(Appliance, appliance_id)
    if not a or a.tenant_id != tenant.id:
        raise HTTPException(404, "appliance not found")
    _require_can_manage_appliance(principal, tenant, appliance_id, db)
    s = ApplianceStorage(tenant_id=tenant.id, appliance_id=a.id,
                         name=body.name.strip() or "Storage",
                         kind="builtin" if body.kind == "builtin" else "external")
    db.add(s)
    db.commit()
    db.refresh(s)
    audit.record(db, actor=principal.user_id, action="appliance.storage_added",
                 tenant_id=tenant.id, resource=a.id, detail={"storage": s.name})
    return {"id": s.id, "name": s.name, "kind": s.kind}


class StorageSetupRequest(BaseModel):
    serial: str
    device: str = ""            # advisory; the agent re-resolves by serial
    name: str
    mirror_of_id: str | None = None   # when set, this drive mirrors that store (1:1)
    confirm: bool = False       # must be true — formatting is destructive


@fleet_router.post("/{appliance_id}/storage/setup")
def setup_storage(appliance_id: str, body: StorageSetupRequest,
                  principal: security.Principal = Depends(security.get_principal),
                  tenant: Tenant = Depends(security.get_tenant),
                  db: Session = Depends(get_db)):
    """Adopt a detected removable drive as Arkive storage. Creates the store row and
    dispatches a signed SETUP_STORAGE command that (destructively) partitions,
    formats and mounts the drive on the appliance. Guarded: the drive must be a
    currently-detected removable device and the caller must confirm."""
    a = db.get(Appliance, appliance_id)
    if not a or a.tenant_id != tenant.id:
        raise HTTPException(404, "appliance not found")
    _require_can_manage_appliance(principal, tenant, appliance_id, db)
    logger.info("storage setup requested: appliance=%s serial=%r name=%r mirror_of=%s by=%s",
                appliance_id, body.serial, body.name, body.mirror_of_id, principal.user_id)
    if not body.confirm:
        raise HTTPException(400, "formatting must be explicitly confirmed")
    if not body.serial:
        raise HTTPException(400, "a drive serial is required")
    # Safety: only adopt a drive the appliance actually reported as a removable
    # candidate this session (never the system or dedicated disk).
    candidates = {d.get("serial"): d for d in (a.telemetry or {}).get("external_devices", [])
                  if d.get("serial")}
    dev = candidates.get(body.serial)
    if not dev:
        logger.warning("storage setup rejected: serial %r not in the appliance's currently-"
                       "detected removable drives %s (telemetry age may be stale, or the drive "
                       "was unplugged/already claimed). appliance=%s",
                       body.serial, sorted(candidates), appliance_id)
        raise HTTPException(409, "That drive is no longer detected on the appliance. "
                                 "Reconnect it and refresh the detected-drives list, then retry.")
    # A mirror shadows exactly one existing store (its source must belong here).
    kind = "external"
    if body.mirror_of_id:
        src = db.get(ApplianceStorage, body.mirror_of_id)
        if not src or src.appliance_id != a.id:
            raise HTTPException(404, "mirror source store not found")
        if src.kind == "mirror":
            raise HTTPException(400, "a mirror cannot mirror another mirror")
        kind = "mirror"
    s = ApplianceStorage(tenant_id=tenant.id, appliance_id=a.id,
                         name=body.name.strip() or "External Storage",
                         kind=kind, state="provisioning",
                         device_serial=body.serial, mirror_of_id=body.mirror_of_id)
    db.add(s)
    db.commit()
    db.refresh(s)
    # Dispatch the signed SETUP_STORAGE command. If it can't be signed/queued, the
    # setup can't proceed — mark the store errored (so it's visible + removable)
    # and surface a clear reason instead of a bare 500 with no trace.
    try:
        cmd = fleet.issue_command(db, a, "SETUP_STORAGE", principal.user_id, {
            "storeId": s.id, "serial": body.serial, "device": dev.get("device") or body.device,
            "name": s.name, "kind": kind, "mirrorOfId": body.mirror_of_id})
    except Exception as exc:  # noqa: BLE001
        logger.exception("storage setup: could not issue SETUP_STORAGE for appliance=%s store=%s",
                         appliance_id, s.id)
        s.state = "error"
        db.commit()
        raise HTTPException(502, f"Could not dispatch the setup command to the appliance: {exc}")
    online = bool(a.last_heartbeat_at and (_now() - a.last_heartbeat_at).total_seconds() < 90)
    logger.info("storage setup dispatched: appliance=%s store=%s command=%s online=%s — the "
                "appliance will format+mount on its next heartbeat", appliance_id, s.id, cmd.id, online)
    if not online:
        logger.warning("storage setup: appliance %s is OFFLINE (last heartbeat %s) — the command "
                       "is queued and runs when it reconnects", appliance_id, a.last_heartbeat_at)
    audit.record(db, actor=principal.user_id, action="appliance.storage_setup",
                 tenant_id=tenant.id, resource=a.id, severity="notice",
                 detail={"storage": s.name, "serial": body.serial, "mirror": bool(body.mirror_of_id)})
    return {"id": s.id, "name": s.name, "kind": s.kind, "state": s.state,
            "appliance_online": online}


class MirrorRequest(BaseModel):
    mirror_of_id: str | None = None   # null clears the mirror role


@fleet_router.put("/{appliance_id}/storage/{storage_id}/mirror")
def configure_mirror(appliance_id: str, storage_id: str, body: MirrorRequest,
                     principal: security.Principal = Depends(security.get_principal),
                     tenant: Tenant = Depends(security.get_tenant),
                     db: Session = Depends(get_db)):
    """Turn an external store into a 1:1 mirror of another store (or clear it).
    Non-destructive: only rewrites the drive's marker + the appliance's mirror
    routing via a signed RECONFIGURE_STORAGE command."""
    a = db.get(Appliance, appliance_id)
    if not a or a.tenant_id != tenant.id:
        raise HTTPException(404, "appliance not found")
    s = db.get(ApplianceStorage, storage_id)
    if not s or s.tenant_id != tenant.id or s.appliance_id != appliance_id:
        raise HTTPException(404, "storage not found")
    _require_can_manage_appliance(principal, tenant, appliance_id, db)
    if s.kind in ("builtin", "dedicated"):
        raise HTTPException(400, "the primary volume cannot be a mirror")
    if body.mirror_of_id:
        src = db.get(ApplianceStorage, body.mirror_of_id)
        if not src or src.appliance_id != a.id:
            raise HTTPException(404, "mirror source store not found")
        if src.id == s.id or src.kind == "mirror":
            raise HTTPException(400, "invalid mirror source")
        s.mirror_of_id = src.id
        s.kind = "mirror"
    else:
        s.mirror_of_id = None
        s.kind = "external"
    db.commit()
    fleet.issue_command(db, a, "RECONFIGURE_STORAGE", principal.user_id, {
        "storeId": s.id, "kind": s.kind, "mirrorOfId": s.mirror_of_id})
    audit.record(db, actor=principal.user_id, action="appliance.storage_mirror",
                 tenant_id=tenant.id, resource=a.id,
                 detail={"storage": s.name, "mirror_of": s.mirror_of_id})
    return {"id": s.id, "name": s.name, "kind": s.kind, "mirror_of_id": s.mirror_of_id}


@fleet_router.put("/{appliance_id}/storage/{storage_id}")
def rename_storage(appliance_id: str, storage_id: str, body: StorageRequest,
                   principal: security.Principal = Depends(security.get_principal),
                   tenant: Tenant = Depends(security.get_tenant),
                   db: Session = Depends(get_db)):
    s = db.get(ApplianceStorage, storage_id)
    if not s or s.tenant_id != tenant.id or s.appliance_id != appliance_id:
        raise HTTPException(404, "storage not found")
    _require_can_manage_appliance(principal, tenant, appliance_id, db)
    s.name = body.name.strip() or s.name
    db.commit()
    audit.record(db, actor=principal.user_id, action="appliance.storage_renamed",
                 tenant_id=tenant.id, resource=appliance_id, detail={"storage": s.name})
    return {"id": s.id, "name": s.name, "kind": s.kind}


@fleet_router.delete("/{appliance_id}/storage/{storage_id}")
def delete_storage(appliance_id: str, storage_id: str,
                   principal: security.Principal = Depends(security.get_principal),
                   tenant: Tenant = Depends(security.get_tenant),
                   db: Session = Depends(get_db)):
    s = db.get(ApplianceStorage, storage_id)
    if not s or s.tenant_id != tenant.id or s.appliance_id != appliance_id:
        raise HTTPException(404, "storage not found")
    _require_can_manage_appliance(principal, tenant, appliance_id, db)
    if s.kind in ("builtin", "dedicated"):
        raise HTTPException(400, "the primary volume cannot be removed")
    # Capture what we need BEFORE the row is deleted/expired (reading s.name after
    # db.delete()+commit() raises a detached-instance error).
    store_name, store_kind = s.name, s.kind
    logger.info("removing appliance storage %s (%s, kind=%s, state=%s) from appliance %s",
                storage_id, store_name, store_kind, s.state, appliance_id)
    # Any mirrors shadowing this store lose their source — revert them to plain
    # external stores so they stop being treated as mirrors.
    for m in (db.query(ApplianceStorage)
              .filter(ApplianceStorage.mirror_of_id == s.id).all()):
        m.mirror_of_id = None
        m.kind = "external"
    a = db.get(Appliance, appliance_id)
    # Best-effort: tell the appliance to unmount + deregister the drive (data is
    # left intact). A failed/offline appliance or an unsignable command must NEVER
    # block removing the record — the store row is the source of truth in the UI.
    forget_issued = False
    if a and store_kind in ("external", "mirror"):
        try:
            fleet.issue_command(db, a, "FORGET_STORAGE", principal.user_id, {"storeId": storage_id})
            forget_issued = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not issue FORGET_STORAGE for %s (removing record anyway): %s",
                           storage_id, exc)
            db.rollback()
            # Re-fetch after rollback so the delete below is on a live instance.
            s = db.get(ApplianceStorage, storage_id)
            if s is None:
                return {"ok": True, "forget_issued": False}
            for m in (db.query(ApplianceStorage)
                      .filter(ApplianceStorage.mirror_of_id == storage_id).all()):
                m.mirror_of_id = None
                m.kind = "external"
    db.delete(s)
    db.commit()
    audit.record(db, actor=principal.user_id, action="appliance.storage_removed",
                 tenant_id=tenant.id, resource=appliance_id,
                 detail={"storage": store_name, "forget_issued": forget_issued})
    logger.info("appliance storage %s (%s) removed; forget_command=%s",
                storage_id, store_name, forget_issued)
    return {"ok": True, "forget_issued": forget_issued}


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
    "installers/cvtool",
    "infra/systemd/cv-appliance-agent.service",
    "infra/systemd/cv-appliance-selfupdate.service",
    "infra/systemd/cv-appliance-selfupdate.timer",
    "infra/systemd/cv-appliance-storage.service",
    "infra/systemd/cv-appliance-storage.path",
    "updater/appliance-update.sh",
)

_appliance_version_cache: str | None = None


def _appliance_bundle_version() -> str:
    """Stable content hash of everything shipped in the appliance bundle — code
    plus the installer/updater/unit files — so the headless self-update timer
    re-installs on any real change (including update-script fixes)."""
    global _appliance_version_cache
    if _appliance_version_cache:
        return _appliance_version_cache
    root = _repo_root()
    paths: list = []
    for d in _BUNDLE_DIRS:
        base = root / d
        if base.exists():
            paths.extend(base.rglob("*"))
    for f in _BUNDLE_FILES:
        paths.append(root / f)
    h = hashlib.sha256()
    for f in sorted(set(paths)):
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


@agent_router.get("/bundle/version")
def appliance_bundle_version():
    """Advertise the appliance bundle version the control plane serves so the
    self-update timer can cheaply check before downloading the whole bundle."""
    return {"version": _appliance_bundle_version()}


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
        vi.mtime = int(time.time())  # fresh mtime so rsync never skips the stamp
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
        "config": _activation_config(),
    }


def _activation_config() -> dict:
    return {
        "heartbeat_interval_seconds": settings.heartbeat_interval_seconds,
        "command_ttl_seconds": settings.command_ttl_seconds,
        "domain": settings.domain,
        "retention_floor_days": 365,
        "immutability": True,
    }


def _gen_pairing_code() -> str:
    """A short, human-friendly pairing code (no ambiguous characters)."""
    alpha = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "ARK-" + "-".join(
        "".join(secrets.choice(alpha) for _ in range(4)) for _ in range(2))


def _mint_appliance_token(db: Session, a: Appliance) -> str:
    token = secrets.token_urlsafe(32)
    a.agent_token_hash = _hash_token(token)
    db.commit()
    _agent_tokens[token] = a.id
    return token


class RegisterRequest(BaseModel):
    serial: str
    model: str | None = None
    identity_bundle: dict
    attestation: dict = {}
    telemetry: dict = {}


@agent_router.post("/register")
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    """Zero-touch registration: an appliance installed WITHOUT a linking code
    registers here and receives a pairing code to display on its local web UI. No
    auth — identity is bound to the presented hardware serial + signing bundle."""
    pa = db.query(PendingAppliance).filter(PendingAppliance.serial == body.serial).first()
    token = secrets.token_urlsafe(32)
    if pa is None:
        pa = PendingAppliance(serial=body.serial, model=body.model or "CV Edge 8",
                              pairing_code=_gen_pairing_code())
        db.add(pa)
    pa.model = body.model or pa.model
    pa.identity_bundle = body.identity_bundle
    pa.attestation = body.attestation or {}
    pa.telemetry = body.telemetry or {}
    pa.token_hash = _hash_token(token)
    pa.last_seen_at = _now()
    if not pa.pairing_code:
        pa.pairing_code = _gen_pairing_code()
    db.commit()
    db.refresh(pa)
    return {
        "registration_id": pa.id,
        "agent_token": token,
        "pairing_code": pa.pairing_code,
        "paired": bool(pa.paired_appliance_id),
        "cloud_public_bundle": fleet.cloud_public_bundle(),
        "heartbeat_interval_seconds": settings.heartbeat_interval_seconds,
    }


def _pending_by_token(authorization: str, db: Session) -> PendingAppliance:
    token = (authorization.split(" ", 1)[1] if authorization.lower().startswith("bearer ")
             else "")
    pa = (db.query(PendingAppliance).filter(
        PendingAppliance.token_hash == _hash_token(token)).first() if token else None)
    if not pa:
        raise HTTPException(401, "invalid registration token")
    return pa


class RegisterHeartbeatRequest(BaseModel):
    telemetry: dict = {}


@agent_router.post("/register-heartbeat")
def register_heartbeat(body: RegisterHeartbeatRequest,
                       authorization: str = Header(default=""),
                       db: Session = Depends(get_db)):
    """An unpaired appliance polls this until a customer claims its pairing code.
    Once claimed, it returns the full activation payload so the agent adopts the
    real appliance identity and switches to the normal management plane."""
    pa = _pending_by_token(authorization, db)
    pa.telemetry = body.telemetry or pa.telemetry
    pa.last_seen_at = _now()
    db.commit()
    if pa.paired_appliance_id:
        a = db.get(Appliance, pa.paired_appliance_id)
        if a is not None:
            token = _mint_appliance_token(db, a)
            # This poll IS the agent checking in live, so count it as a heartbeat:
            # the appliance reflects "online" the moment it adopts, instead of
            # sitting Offline in ONLINE_STAGING until its first management heartbeat.
            a.last_heartbeat_at = _now()
            if body.telemetry:
                a.telemetry = body.telemetry
            db.commit()
            from .. import services
            return {"paired": True, "activation": {
                "appliance_id": a.id, "agent_token": token, "tenant_id": a.tenant_id,
                "cloud_public_bundle": fleet.cloud_public_bundle(),
                "node_url": services.tenant_node_url(db, a.tenant_id),
                "config": _activation_config()}}
    return {"paired": False, "pairing_code": pa.pairing_code,
            "heartbeat_interval_seconds": settings.heartbeat_interval_seconds}


class HeartbeatRequest(BaseModel):
    state: str
    isolation_state: str
    software_version: str
    attestation: dict
    telemetry: dict
    tamper_state: str = "normal"


@agent_router.post("/heartbeat")
def heartbeat(body: HeartbeatRequest,
              request: Request,
              appliance: Appliance = Depends(_agent_appliance),
              db: Session = Depends(get_db)):
    # Capture the appliance's public IP as seen by the control plane (behind
    # Caddy the real client is in X-Forwarded-For) and fold it into telemetry.
    fwd = request.headers.get("x-forwarded-for", "")
    public_ip = (fwd.split(",")[0].strip() if fwd
                 else (request.client.host if request.client else None))
    tel = dict(body.telemetry or {})
    if public_ip:
        tel["public_ip"] = public_ip
    appliance.state = body.state
    appliance.isolation_state = body.isolation_state
    if body.software_version and body.software_version != appliance.software_version:
        appliance.software_version = body.software_version
        appliance.version_updated_at = _now()
    # Auto-map a dedicated-storage appliance's capacity to its CV Edge model, then
    # rebrand the OEM DMI strings to the Arkive product line for the UI. A VM is
    # always the CV Edge VM model.
    if tel.get("model_kind") == "vm":
        if appliance.model != "CV Edge VM":
            appliance.model = "CV Edge VM"
    elif tel.get("storage_kind") == "dedicated":
        cap_total = int(tel.get("capacity_total_bytes") or 0)
        if cap_total:
            mapped = _model_for_capacity(db, cap_total, appliance.model)
            if mapped and mapped != appliance.model:
                appliance.model = mapped
    _brand_telemetry(tel, appliance.model)
    appliance.telemetry = tel
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

    # Storage telemetry is best-effort — never let it reject the heartbeat.
    try:
        _sync_storage_telemetry(db, appliance, body.telemetry or {})
        _notify_detected_storage(db, appliance, body.telemetry or {})
    except Exception:
        db.rollback()
        logger.exception("storage telemetry sync failed for appliance %s", appliance.id)

    # Fold the appliance's recent log lines into the unified platform log store so
    # they're viewable from the control plane. Best-effort — never fail heartbeat.
    try:
        from .. import logsink
        logsink.ingest_device_logs(
            db, source="appliance", lines=(body.telemetry or {}).get("recent_logs") or [],
            tenant_id=appliance.tenant_id, appliance_id=appliance.id,
            device_name=appliance.name or appliance.serial or "")
    except Exception:  # noqa: BLE001
        db.rollback()

    # Deliver pending signed commands (management plane only). Delivery is
    # at-least-once: a command that was handed out but never acked (appliance
    # missed it, crashed mid-handling, or its result POST failed) is redelivered
    # on a later heartbeat until it is acked or its signed TTL expires. This stops
    # recovery/ingest from stalling forever on a single dropped delivery.
    ttl = settings.command_ttl_seconds or 900
    redeliver_after = max(60, settings.heartbeat_interval_seconds * 2)
    cmds = (db.query(ApplianceCommand)
            .filter(ApplianceCommand.appliance_id == appliance.id,
                    ApplianceCommand.status.in_(["pending", "delivered"]))
            .order_by(ApplianceCommand.sequence.asc()).all())
    delivered = []
    delivered_types = []
    for c in cmds:
        age = (_now() - (c.created_at.replace(tzinfo=None) if c.created_at
                         and c.created_at.tzinfo else c.created_at)).total_seconds() \
            if c.created_at else 0.0
        if age > ttl:
            c.status = "expired"  # signed command no longer valid; appliance would reject it
            c.envelope = {}       # free the (possibly huge, inline-ciphertext) payload
            continue
        if c.status == "pending" or age > redeliver_after:
            c.status = "delivered"
            delivered.append(c.envelope)
            delivered_types.append(c.command_type)
    db.commit()
    if delivered:
        logger.info("delivered %d command(s) to appliance %s: %s",
                    len(delivered), appliance.id, delivered_types)
    from .. import services
    return {"commands": delivered,
            "config": _appliance_runtime_config(db, appliance),
            "latest_version": _appliance_bundle_version(),
            "control_plane_key_id": fleet.cloud_public_bundle().get("keyId"),
            "node_url": services.tenant_node_url(db, appliance.tenant_id),
            "next_heartbeat_seconds": settings.heartbeat_interval_seconds}


def _appliance_runtime_config(db: Session, appliance: Appliance) -> dict:
    """Runtime settings delivered to the appliance from its assigned configuration
    profile (e.g. log level), so admins can tune it from the config-profile page."""
    from ..models import ConfigProfile
    data = {}
    pid = getattr(appliance, "config_profile_id", None)
    if pid:
        prof = db.get(ConfigProfile, pid)
        if prof and prof.enabled:
            data = prof.data or {}
    return {"log_level": (data.get("CV_LOG_LEVEL") or "info")}


class CommandResultRequest(BaseModel):
    command_id: str
    accepted: bool
    result: dict = {}
    receipt: dict | None = None  # signed seal/attestation receipt


@agent_router.get("/control-plane-bundle")
def control_plane_bundle(appliance: Appliance = Depends(_agent_appliance),
                         db: Session = Depends(get_db)):
    """Return the cloud's current command-signing public bundle so an already-
    linked appliance can re-pin it after a legitimate control-plane key rotation.

    This is the only distribution path for a rotated signing key: commands are
    signed with the new key and would otherwise fail the appliance's pinned-key
    check forever. The request is authenticated by the appliance's own bearer
    token over TLS (same trust channel as activation), and the re-pin is audited."""
    bundle = fleet.cloud_public_bundle()
    audit.record(db, actor=f"appliance:{appliance.serial}",
                 action="appliance.control_plane_retrust", tenant_id=appliance.tenant_id,
                 resource=appliance.id, detail={"key_id": bundle.get("keyId")})
    return {"bundle": bundle}


@agent_router.get("/index-replica")
def index_replica_pull(scope: str, scopeId: str, storeId: str,
                       appliance: Appliance = Depends(_agent_appliance),
                       db: Session = Depends(get_db)):
    """Stream the staged encrypted search-index blob for a scope so the appliance
    can PULL its DR copy (the STAGE_INDEX command carries only a pointer, never the
    blob). Authenticated by the appliance bearer token; the store must belong to
    this appliance and this appliance's tenant."""
    store = db.get(ApplianceStorage, storeId)
    if not store or store.appliance_id != appliance.id:
        raise HTTPException(404, "store not found for this appliance")
    from ..workers import index_replication
    path = index_replication.staged_index_path(scope, scopeId, storeId)
    if not path.exists():
        logger.warning("index-replica pull: no staged blob for scope=%s:%s store=%s "
                       "(appliance=%s)", scope, scopeId, storeId, appliance.id)
        raise HTTPException(404, "no staged index for this scope")
    logger.info("index-replica pull: serving %d bytes to appliance=%s scope=%s:%s",
                path.stat().st_size, appliance.id, scope, scopeId)
    return FileResponse(str(path), media_type="application/octet-stream",
                        filename=path.name)


@agent_router.post("/command-result")
def command_result(body: CommandResultRequest,
                   appliance: Appliance = Depends(_agent_appliance),
                   db: Session = Depends(get_db)):
    cmd = db.get(ApplianceCommand, body.command_id)
    if cmd is None or cmd.appliance_id != appliance.id:
        # Legacy rows created before the row id was keyed to the signed
        # commandId: resolve by matching the envelope's commandId instead.
        candidates = (db.query(ApplianceCommand)
                      .filter(ApplianceCommand.appliance_id == appliance.id,
                              ApplianceCommand.status.in_(["pending", "delivered"]))
                      .all())
        cmd = next((c for c in candidates
                    if (c.envelope or {}).get("payload", {}).get("commandId") == body.command_id),
                   None)
    if not cmd or cmd.appliance_id != appliance.id:
        raise HTTPException(404, "command not found")
    cmd.status = "acked" if body.accepted else "rejected"
    result = dict(body.result or {})
    logger.info("command-result %s type=%s accepted=%s result_keys=%s",
                cmd.id, cmd.command_type, body.accepted, list(result.keys()))
    # Recovery windows: the appliance returns the encrypted object units it read
    # out of sealed storage. Decrypt them here (inside the key boundary) and stage
    # them into the same time-limited recovery window used by cloud retrieval, so
    # the requesting operator can view/download the item.
    if body.accepted and cmd.command_type == "OPEN_RECOVERY_WINDOW":
        try:
            result["recovered"] = _stage_recovered_from_appliance(db, appliance, cmd, result)
        except Exception as exc:  # noqa: BLE001 - never fail the ack on staging error
            logger.warning("recovery staging failed for %s: %s", cmd.id, exc)
            result.setdefault("error", f"staging failed: {exc}")
    # An ingest the appliance ACCEPTED but failed to write (e.g. a disk/mount error)
    # must not be silently lost: enqueue a durable retry so the backup re-attempts
    # (with backoff) and resumes once the underlying issue is resolved.
    if cmd.command_type == "OPEN_INGEST_WINDOW" and (not body.accepted or result.get("error")):
        _enqueue_ingest_retry(db, appliance, cmd,
                              result.get("error") or "appliance rejected ingest command")
    # Reflect a storage setup outcome on the store so the UI doesn't spin on
    # "Setting up" forever — surface the failure reason (retryable from the UI).
    if cmd.command_type == "SETUP_STORAGE":
        _apply_setup_result(db, appliance, cmd, body.accepted, result)
    cmd.result = result
    # The command is terminal now — drop the inline-ciphertext envelope so completed
    # commands don't bloat appliance_commands (its payload was already delivered).
    cmd.envelope = {}
    db.commit()
    audit.record(db, actor=f"appliance:{appliance.serial}",
                 action="appliance.command_result", tenant_id=appliance.tenant_id,
                 resource=cmd.id, detail={"accepted": body.accepted})
    return {"ok": True}


def _apply_setup_result(db: Session, appliance: Appliance, cmd: ApplianceCommand,
                        accepted: bool, result: dict) -> None:
    """Reflect a SETUP_STORAGE outcome onto the ApplianceStorage row: a failure
    marks the store 'error' with the reason (UI stops spinning, user can retry);
    success leaves the store to flip 'ready' on the next storage telemetry sync."""
    store_id = (cmd.envelope or {}).get("payload", {}).get("parameters", {}).get("storeId")
    if not store_id:
        return
    store = db.get(ApplianceStorage, store_id)
    if not store or store.appliance_id != appliance.id:
        return
    err = result.get("error") if (not accepted or result.get("error")) else None
    if err:
        store.state = "error"
        h = dict(store.health or {})
        h["setup_error"] = str(err)[:300]
        store.health = h
        logger.warning("appliance %s storage setup failed for store %s: %s",
                       appliance.id, store_id, err)
    else:
        store.state = "ready"
        h = dict(store.health or {})
        h.pop("setup_error", None)
        store.health = h


def _enqueue_ingest_retry(db: Session, appliance: Appliance,
                          cmd: ApplianceCommand, error: str) -> None:
    """Register a failed appliance ingest for durable, backed-off retry so a write
    the appliance accepted-then-failed is re-attempted (never silently lost).
    Reads the command params BEFORE the envelope is cleared."""
    from .. import queue_registry as _q
    from ..models import Collection, Tenant
    params = (cmd.envelope or {}).get("payload", {}).get("parameters", {})
    store_id = params.get("storageId")
    collection_id = params.get("collectionId")
    snapshot_id = params.get("snapshotId")
    target = f"store:{store_id}" if store_id else "appliance"
    label = ""
    coll = db.get(Collection, collection_id) if collection_id else None
    if coll:
        label = f"{coll.name or coll.source_type} → {_q.target_label(target)}"
    tenant = db.get(Tenant, appliance.tenant_id)
    try:
        _q.enqueue(db, tenant_id=appliance.tenant_id, target=target, error=error,
                   collection_id=collection_id, snapshot_id=snapshot_id,
                   node_id=(tenant.node_id if tenant else None),
                   label=label)
        db.commit()
        logger.warning("appliance %s ingest failed (%s) → queued retry for %s",
                       appliance.id, error, target)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not queue ingest retry for appliance %s: %s", appliance.id, exc)
        db.rollback()


def _stage_recovered_from_appliance(db: Session, appliance: Appliance,
                                    cmd: ApplianceCommand, result: dict) -> list[dict]:
    """Decrypt the envelope units an appliance returned and stage recovery-window
    items. Returns a compact list describing each staged item for status polling."""
    from ..models import SearchDocument
    from . import recovery
    from .search import decrypt_recovered_units

    units = result.get("units") or {}
    params = (cmd.envelope or {}).get("payload", {}).get("parameters", {})
    snapshot_id = params.get("snapshotId")
    object_ids = params.get("objectIds", [])
    if not units or not snapshot_id:
        logger.warning("recovery staging: nothing to stage (units=%d snapshot=%s) for %s",
                       len(units), snapshot_id, cmd.id)
        return []
    receipt = (db.query(SnapshotReceipt)
               .filter(SnapshotReceipt.tenant_id == appliance.tenant_id,
                       SnapshotReceipt.snapshot_id == snapshot_id).first())
    if receipt is None:
        logger.warning("recovery staging: no receipt for snapshot %s (tenant %s)",
                       snapshot_id, appliance.tenant_id)
        return []
    staged: list[dict] = []
    for oid in object_ids:
        plaintext, client_encrypted = decrypt_recovered_units(db, receipt, oid, units)
        if plaintext is None or client_encrypted:
            logger.warning("recovery staging: %s not staged (plaintext=%s client_encrypted=%s)",
                           oid, plaintext is not None, client_encrypted)
            continue
        doc = (db.query(SearchDocument)
               .filter(SearchDocument.tenant_id == appliance.tenant_id,
                       SearchDocument.object_id == oid).first())
        title = (doc.title if doc else oid) or oid
        item = recovery.create_recovered(
            db, appliance.tenant_id, cmd.requested_by, object_id=oid,
            snapshot_id=snapshot_id, title=title,
            doc_type=doc.doc_type if doc else "",
            source_type=doc.source_type if doc else "",
            location=appliance.name, content=plaintext)
        staged.append({
            "object_id": oid, "recovered_id": item.id, "title": item.title,
            "mime": item.mime, "size_bytes": item.size_bytes,
            "doc_type": item.doc_type, "source_type": item.source_type,
        })
    logger.info("recovery staging: staged %d/%d item(s) from appliance %s",
                len(staged), len(object_ids), appliance.id)
    return staged


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
