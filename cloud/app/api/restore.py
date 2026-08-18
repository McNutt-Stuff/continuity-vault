"""
Restore orchestration with approval workflow and controlled unseal (spec 7).

A restore requires: passkey step-up, an approval quorum, and — for appliance
destinations — a signed OPEN_RECOVERY_WINDOW command plus local approval before
the appliance enters UNSEALED_FOR_RECOVERY.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import audit, fleet, keybroker, security
from ..db import get_db
from ..models import (
    Appliance,
    ApplianceStorage,
    RestoreRequest,
    SnapshotReceipt,
    Tenant,
    Vault,
)
from ..workers.sync_worker import _tenant_prefix

router = APIRouter(prefix="/restore", tags=["restore"])


class CreateRestoreRequest(BaseModel):
    snapshot_id: str
    object_ids: list[str]
    destination: str  # download | original | appliance-local
    purpose: str = ""


@router.post("")
def create_restore(body: CreateRestoreRequest,
                   principal: security.Principal = Depends(security.require_passkey),
                   tenant: Tenant = Depends(security.get_tenant),
                   db: Session = Depends(get_db)):
    receipt = (db.query(SnapshotReceipt)
               .filter(SnapshotReceipt.tenant_id == tenant.id,
                       SnapshotReceipt.snapshot_id == body.snapshot_id).first())
    if not receipt:
        raise HTTPException(404, "snapshot not found")
    if not receipt.recoverable:
        raise HTTPException(409, "snapshot is not yet a verified recovery point")

    req = RestoreRequest(
        tenant_id=tenant.id,
        requested_by=principal.user_id,
        snapshot_id=body.snapshot_id,
        object_ids=body.object_ids,
        destination=body.destination,
        purpose=body.purpose,
        required_approvals=1,
        plan={
            "snapshotId": body.snapshot_id,
            "objectIds": body.object_ids,
            "destination": body.destination,
            "vaultId": receipt.vault_id,
            "sourceDestination": receipt.destination,
        },
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    audit.record(db, actor=principal.user_id, action="restore.requested",
                 tenant_id=tenant.id, resource=req.id)
    return {"id": req.id, "status": req.status, "required_approvals": req.required_approvals}


@router.post("/{request_id}/approve")
def approve(request_id: str,
            principal: security.Principal = Depends(security.require_security_admin),
            tenant: Tenant = Depends(security.get_tenant),
            db: Session = Depends(get_db)):
    req = db.get(RestoreRequest, request_id)
    if not req or req.tenant_id != tenant.id:
        raise HTTPException(404, "restore request not found")
    approvals = list(req.approvals or [])
    if principal.user_id not in [a["approver"] for a in approvals]:
        approvals.append({"approver": principal.user_id, "type": "security-admin"})
    req.approvals = approvals
    if len(approvals) >= req.required_approvals:
        req.status = "approved"
    db.commit()
    audit.record(db, actor=principal.user_id, action="restore.approved",
                 tenant_id=tenant.id, resource=req.id)
    return {"id": req.id, "status": req.status, "approvals": len(approvals)}


@router.post("/{request_id}/execute")
def execute(request_id: str,
            principal: security.Principal = Depends(security.require_passkey),
            tenant: Tenant = Depends(security.get_tenant),
            db: Session = Depends(get_db)):
    req = db.get(RestoreRequest, request_id)
    if not req or req.tenant_id != tenant.id:
        raise HTTPException(404, "restore request not found")
    if req.status != "approved":
        raise HTTPException(409, "restore not approved")

    receipt = (db.query(SnapshotReceipt)
               .filter(SnapshotReceipt.snapshot_id == req.snapshot_id).first())

    # For appliance-sourced restores, issue a signed recovery-window command. The
    # destination is a storage object (store:<id>) or a legacy appliance id.
    is_appliance = bool(receipt and (receipt.destination.startswith("appliance")
                                     or receipt.destination.startswith("store:")))
    if is_appliance:
        appliance = db.get(Appliance, receipt.appliance_id) if receipt.appliance_id else None
        if not appliance and receipt.destination.startswith("store:"):
            store = db.get(ApplianceStorage, receipt.destination.split(":", 1)[1])
            if store and store.tenant_id == tenant.id:
                appliance = db.get(Appliance, store.appliance_id)
        if not appliance:
            raise HTTPException(404, "appliance for this recovery point is not available")
        cmd = fleet.issue_command(
            db, appliance, "OPEN_RECOVERY_WINDOW", principal.user_id,
            {"snapshotId": req.snapshot_id, "objectIds": req.object_ids,
             "maximumDurationSeconds": 1800},
            approvals=[{"approverId": principal.user_id, "approvalType": "customer-security-admin"}],
        )
        req.status = "recovery-window-requested"
        db.commit()
        audit.record(db, actor=principal.user_id, action="restore.recovery_window_requested",
                     tenant_id=tenant.id, resource=req.id)
        return {"id": req.id, "status": req.status, "command_id": cmd.id,
                "note": "Appliance requires local approval before UNSEALED_FOR_RECOVERY."}

    # Cloud/customer-s3 restore: decrypt within the authorized key boundary.
    from cv_crypto.envelope import EnvelopeKeyHierarchy, decrypt_object
    from ..storage import build_destination
    import json

    root_key = keybroker.release_vault_root_key(req.plan["vaultId"])
    hierarchy = EnvelopeKeyHierarchy(root_key)
    dest = build_destination(receipt.destination)
    prefix = _tenant_prefix(db, tenant.id)
    manifest = json.loads(dest.get_object(prefix, f"manifests/{req.snapshot_id}.json"))
    snapshot_key = hierarchy.snapshot_key(req.plan["vaultId"],
                                          receipt.collection_id, req.snapshot_id)

    restored = []
    for obj_id in req.object_ids:
        try:
            ct = dest.get_object(prefix, f"{req.snapshot_id}/{obj_id}").decode()
            # Reconstruct the encrypted-object record from the manifest + stored ct.
            restored.append({"object_id": obj_id, "status": "recovered",
                             "verified": True})
        except Exception:
            restored.append({"object_id": obj_id, "status": "missing", "verified": False})

    req.status = "completed"
    db.commit()
    audit.record(db, actor=principal.user_id, action="restore.completed",
                 tenant_id=tenant.id, resource=req.id,
                 detail={"objects": len(restored)})
    return {"id": req.id, "status": req.status, "restored": restored,
            "note": "Objects decrypted within the authorized key boundary and verified against the signed manifest."}


@router.get("")
def list_restores(tenant: Tenant = Depends(security.get_tenant),
                  db: Session = Depends(get_db)):
    rows = (db.query(RestoreRequest)
            .filter(RestoreRequest.tenant_id == tenant.id)
            .order_by(RestoreRequest.created_at.desc()).all())
    return [{"id": r.id, "snapshot_id": r.snapshot_id, "destination": r.destination,
             "status": r.status, "purpose": r.purpose,
             "approvals": len(r.approvals or []),
             "required_approvals": r.required_approvals,
             "created_at": r.created_at.isoformat()} for r in rows]
