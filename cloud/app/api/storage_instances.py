"""Customer cloud storage (bring-your-own) API — a backup destination the
customer owns (AWS S3 / Azure Blob / GCS). Org owners/admins configure these in
Protection Setup; they then appear as selectable targets in the Data Map and as
storage resources across the Overview/reporting, with per-instance health, usage
and the sources stored on them.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import customer_storage as cs_mod
from .. import security
from ..db import get_db
from ..models import Collection, CustomerStorage, SnapshotReceipt, Tenant

router = APIRouter(prefix="/storage", tags=["customer-storage"],
                   dependencies=[Depends(security.require_org_admin)])


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# Views / aggregation                                                         #
# --------------------------------------------------------------------------- #
def _usage_map(db: Session, tenant_id: str) -> dict:
    """Per-instance stored bytes / recovery points / objects, aggregated from the
    snapshot receipts written to each byos:<id> destination."""
    rows = (db.query(SnapshotReceipt.destination,
                     func.coalesce(func.sum(SnapshotReceipt.total_bytes), 0),
                     func.count(SnapshotReceipt.id),
                     func.coalesce(func.sum(SnapshotReceipt.object_count), 0))
            .filter(SnapshotReceipt.tenant_id == tenant_id,
                    SnapshotReceipt.destination.like(f"{cs_mod.DEST_PREFIX}%"))
            .group_by(SnapshotReceipt.destination).all())
    out: dict = {}
    for dest, byts, points, objs in rows:
        sid = cs_mod.storage_id_from_dest(dest)
        if sid:
            out[sid] = {"bytes": int(byts or 0), "points": int(points or 0),
                        "objects": int(objs or 0)}
    return out


def _view(cs: CustomerStorage, usage: dict | None = None) -> dict:
    spec = cs_mod.provider_spec(cs.provider) or {}
    u = usage or {}
    return {
        "id": cs.id, "name": cs.name, "provider": cs.provider,
        "provider_display": spec.get("display_name", cs.provider),
        "icon": spec.get("icon", "cloud"), "color": spec.get("color", "#4f7cff"),
        "config": {k: v for k, v in (cs.config or {}).items()},  # non-secret only
        "enabled": bool(cs.enabled),
        "status": cs.status or "unknown",
        "provision_mode": cs.provision_mode or "existing",
        "provision_state": cs.provision_state or "done",
        "provision_message": cs.provision_message,
        "has_read_credential": bool(cs.read_credentials),
        "used_bytes": int(u.get("bytes", cs.used_bytes or 0)),
        "recovery_points": int(u.get("points", 0)),
        "object_count": int(u.get("objects", 0)),
        "last_test_at": cs.last_test_at.isoformat() if cs.last_test_at else None,
        "last_test_ok": bool(cs.last_test_ok),
        "last_test_error": cs.last_test_error,
        "created_at": cs.created_at.isoformat() if cs.created_at else None,
    }


def _owned(db: Session, principal, sid: str) -> CustomerStorage:
    cs = db.get(CustomerStorage, sid)
    if not cs or cs.tenant_id != principal.tenant_id:
        raise HTTPException(404, "storage not found")
    return cs


# --------------------------------------------------------------------------- #
# Endpoints                                                                    #
# --------------------------------------------------------------------------- #
@router.get("/providers")
def list_providers(principal: security.Principal = Depends(security.get_principal)):
    """Field specs per provider for the 'existing storage' setup dialog."""
    return list(cs_mod.PROVIDERS.values())


@router.get("")
def list_storages(principal: security.Principal = Depends(security.get_principal),
                  db: Session = Depends(get_db)):
    rows = (db.query(CustomerStorage)
            .filter(CustomerStorage.tenant_id == principal.tenant_id)
            .order_by(CustomerStorage.created_at.desc()).all())
    usage = _usage_map(db, principal.tenant_id)
    return {
        "providers": list(cs_mod.PROVIDERS.values()),
        "instances": [_view(cs, usage.get(cs.id)) for cs in rows],
    }


class CreateStorage(BaseModel):
    provider: str
    name: str
    provision_mode: str = "existing"  # existing | provisioned
    config: dict = {}
    write: dict = {}   # write credential fields
    read: dict = {}    # read credential fields (optional)


@router.post("")
def create_storage(body: CreateStorage,
                   principal: security.Principal = Depends(security.get_principal),
                   db: Session = Depends(get_db)):
    spec = cs_mod.provider_spec(body.provider)
    if spec is None:
        raise HTTPException(400, "unsupported provider")
    name = (body.name or "").strip() or spec["display_name"]
    cs = CustomerStorage(
        tenant_id=principal.tenant_id, owner_user_id=principal.user_id,
        name=name, provider=body.provider.lower(),
        config={k: v for k, v in (body.config or {}).items() if v not in (None, "")},
        write_credentials=cs_mod.enc_credentials(principal.tenant_id, body.write),
        read_credentials=cs_mod.enc_credentials(principal.tenant_id, body.read),
        provision_mode=body.provision_mode or "existing",
        provision_state="done", status="unknown")
    db.add(cs)
    db.commit()
    # Validate immediately so the user sees health right away (best-effort).
    _run_test(db, cs)
    return _view(cs, _usage_map(db, principal.tenant_id).get(cs.id))


class UpdateStorage(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    config: dict | None = None
    write: dict | None = None
    read: dict | None = None


@router.put("/{sid}")
def update_storage(sid: str, body: UpdateStorage,
                   principal: security.Principal = Depends(security.get_principal),
                   db: Session = Depends(get_db)):
    cs = _owned(db, principal, sid)
    if body.name is not None:
        cs.name = body.name.strip() or cs.name
    if body.enabled is not None:
        cs.enabled = body.enabled
    if body.config is not None:
        cs.config = {**(cs.config or {}),
                     **{k: v for k, v in body.config.items() if v not in (None, "")}}
    if body.write:  # only replace when the user actually entered new secrets
        merged = {**cs_mod.dec_credentials(cs.tenant_id, cs.write_credentials), **body.write}
        cs.write_credentials = cs_mod.enc_credentials(cs.tenant_id, merged)
    if body.read:
        merged = {**cs_mod.dec_credentials(cs.tenant_id, cs.read_credentials), **body.read}
        cs.read_credentials = cs_mod.enc_credentials(cs.tenant_id, merged)
    cs.updated_at = _now()
    db.commit()
    if body.write or body.read or body.config is not None:
        _run_test(db, cs)
    return _view(cs, _usage_map(db, principal.tenant_id).get(cs.id))


@router.delete("/{sid}")
def delete_storage(sid: str,
                   principal: security.Principal = Depends(security.get_principal),
                   db: Session = Depends(get_db)):
    cs = _owned(db, principal, sid)
    # Guard: don't orphan mappings still routing here (destinations is JSON).
    dest = f"{cs_mod.DEST_PREFIX}{cs.id}"
    routed = [c for c in db.query(Collection).filter(
        Collection.tenant_id == principal.tenant_id).all()
        if dest in (c.destinations or [])]
    if routed:
        raise HTTPException(409, "this storage is still used by a data mapping — "
                                 "re-route those mappings first")
    db.delete(cs)
    db.commit()
    return {"ok": True}


@router.post("/{sid}/test")
def test_storage(sid: str,
                 principal: security.Principal = Depends(security.get_principal),
                 db: Session = Depends(get_db)):
    cs = _owned(db, principal, sid)
    _run_test(db, cs)
    return _view(cs, _usage_map(db, principal.tenant_id).get(cs.id))


def _run_test(db: Session, cs: CustomerStorage) -> None:
    ok, err = cs_mod.test_storage(db, cs)
    cs.last_test_at = _now()
    cs.last_test_ok = ok
    cs.last_test_error = err or None
    cs.status = "healthy" if ok else "error"
    cs.updated_at = _now()
    db.commit()


@router.get("/{sid}/data")
def storage_data(sid: str,
                 principal: security.Principal = Depends(security.get_principal),
                 db: Session = Depends(get_db)):
    """Per-instance drill-down: which sources are stored here + how much (mirrors
    the appliance stored-data view)."""
    cs = _owned(db, principal, sid)
    dest = f"{cs_mod.DEST_PREFIX}{cs.id}"
    receipts = (db.query(SnapshotReceipt)
                .filter(SnapshotReceipt.tenant_id == principal.tenant_id,
                        SnapshotReceipt.destination == dest).all())
    colls = {c.id: c for c in db.query(Collection).filter(
        Collection.tenant_id == principal.tenant_id).all()}
    by_coll: dict = {}
    total_bytes = 0
    for r in receipts:
        c = colls.get(r.collection_id)
        key = r.collection_id
        g = by_coll.setdefault(key, {
            "collection_id": key,
            "name": c.name if c else key,
            "source_type": c.source_type if c else "",
            "recovery_points": 0, "objects": 0, "bytes": 0,
            "recoverable": 0, "last_at": None})
        g["recovery_points"] += 1
        g["objects"] += int(r.object_count or 0)
        g["bytes"] += int(r.total_bytes or 0)
        if r.recoverable:
            g["recoverable"] += 1
        if r.created_at and (g["last_at"] is None or r.created_at > g["last_at"]):
            g["last_at"] = r.created_at
        total_bytes += int(r.total_bytes or 0)
    sources = sorted(by_coll.values(), key=lambda s: -s["bytes"])
    for s in sources:
        s["last_at"] = s["last_at"].isoformat() if s["last_at"] else None
    return {
        "storage": _view(cs, {"bytes": total_bytes,
                              "points": sum(s["recovery_points"] for s in sources),
                              "objects": sum(s["objects"] for s in sources)}),
        "sources": sources,
    }


@router.get("/arkive-cloud")
def arkive_cloud(principal: security.Principal = Depends(security.get_principal),
                 tenant: Tenant = Depends(security.get_tenant),
                 db: Session = Depends(get_db)):
    """Read-only usage view of Arkive's own hosted cloud (cv-cloud): how much the
    tenant stores with us and which sources land there. No controls — Arkive Cloud
    is managed by us, unlike a customer's bring-your-own bucket."""
    from ..api.billing import user_protection_options
    from ..models import User

    enabled = "cv-cloud" in set(user_protection_options(db.get(User, principal.user_id), tenant))
    receipts = (db.query(SnapshotReceipt)
                .filter(SnapshotReceipt.tenant_id == principal.tenant_id,
                        SnapshotReceipt.destination == "cv-cloud").all())
    colls = {c.id: c for c in db.query(Collection).filter(
        Collection.tenant_id == principal.tenant_id).all()}
    by_coll: dict = {}
    total_bytes = 0
    last_at = None
    for r in receipts:
        c = colls.get(r.collection_id)
        g = by_coll.setdefault(r.collection_id, {
            "collection_id": r.collection_id,
            "name": c.name if c else r.collection_id,
            "source_type": c.source_type if c else "",
            "recovery_points": 0, "objects": 0, "bytes": 0,
            "recoverable": 0, "last_at": None})
        g["recovery_points"] += 1
        g["objects"] += int(r.object_count or 0)
        g["bytes"] += int(r.total_bytes or 0)
        if r.recoverable:
            g["recoverable"] += 1
        if r.created_at and (g["last_at"] is None or r.created_at > g["last_at"]):
            g["last_at"] = r.created_at
        if r.created_at and (last_at is None or r.created_at > last_at):
            last_at = r.created_at
        total_bytes += int(r.total_bytes or 0)
    sources = sorted(by_coll.values(), key=lambda s: -s["bytes"])
    for s in sources:
        s["last_at"] = s["last_at"].isoformat() if s["last_at"] else None
    return {
        "enabled": enabled,
        "used_bytes": total_bytes,
        "recovery_points": sum(s["recovery_points"] for s in sources),
        "object_count": sum(s["objects"] for s in sources),
        "source_count": len(sources),
        "last_backup_at": last_at.isoformat() if last_at else None,
        "sources": sources,
    }


# ---- Scenario 2: guided auto-provisioning -----------------------------------
class ProvisionBody(BaseModel):
    provider: str
    name: str = ""
    admin: dict = {}   # org-level credential — used once to provision, never stored


@router.post("/provision")
def provision_start(body: ProvisionBody,
                    principal: security.Principal = Depends(security.get_principal),
                    db: Session = Depends(get_db)):
    """Auto-provision a dedicated bucket/container + scoped write & read
    credentials from the customer's org-level admin credential. The admin
    credential is used only for this call and never stored. Runs in the
    background; the client polls GET /storage/{id}/provision for progress."""
    spec = cs_mod.provider_spec(body.provider)
    if spec is None:
        raise HTTPException(400, "unsupported provider")
    for f in spec.get("provision", []):
        if f.get("required") and not (body.admin or {}).get(f["name"]):
            raise HTTPException(400, f"{f['label']} is required")
    cs = CustomerStorage(
        tenant_id=principal.tenant_id, owner_user_id=principal.user_id,
        name=(body.name or "").strip() or f"{spec['display_name']}",
        provider=body.provider.lower(), config={},
        provision_mode="provisioned", provision_state="provisioning",
        provision_message="Starting…", status="unknown")
    db.add(cs)
    db.commit()
    _spawn_provision(cs.id, principal.tenant_id, body.provider.lower(), dict(body.admin or {}))
    return {"id": cs.id, "provision_state": cs.provision_state}


@router.get("/{sid}/provision")
def provision_status(sid: str,
                     principal: security.Principal = Depends(security.get_principal),
                     db: Session = Depends(get_db)):
    cs = _owned(db, principal, sid)
    return {
        "id": cs.id, "provision_state": cs.provision_state or "done",
        "message": cs.provision_message,
        "done": cs.provision_state == "done",
        "error": cs.provision_state == "error",
        "status": cs.status,
    }


def _spawn_provision(cs_id: str, tenant_id: str, provider: str, admin: dict) -> None:
    import threading
    from ..db import WorkerSessionLocal
    from .. import storage_provision

    def _go():
        with WorkerSessionLocal() as wdb:
            cs = wdb.get(CustomerStorage, cs_id)
            if cs is None:
                return

            def progress(msg: str) -> None:
                cs.provision_message = msg[:300]
                cs.updated_at = _now()
                wdb.commit()

            try:
                result = storage_provision.provision(provider, admin, tenant_id, progress)
                cs_mod.apply_provision_result(wdb, cs, result)
                cs.provision_message = "Verifying access…"
                wdb.commit()
                ok, err = cs_mod.test_storage_retry(wdb, cs)
                cs.last_test_at = _now()
                cs.last_test_ok = ok
                cs.last_test_error = err or None
                cs.status = "healthy" if ok else "degraded"
                cs.provision_state = "done"
                cs.provision_message = result.get("summary") or "Storage ready."
                cs.updated_at = _now()
                wdb.commit()
            except Exception as exc:  # noqa: BLE001
                import logging
                logging.getLogger("cv.storage").exception("auto-provision failed")
                cs.provision_state = "error"
                cs.status = "error"
                cs.provision_message = str(exc)[:400]
                cs.updated_at = _now()
                wdb.commit()

    threading.Thread(target=_go, name=f"cv-storage-provision-{cs_id[:8]}", daemon=True).start()

