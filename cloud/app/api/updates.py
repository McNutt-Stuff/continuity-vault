"""
Software release publishing + cloud-triggered updates (spec 11).

Releases carry a hybrid-signed update manifest. Update jobs can target the cloud
server itself or appliances; appliance jobs are delivered as signed
STAGE_UPDATE / APPLY_UPDATE commands via the management plane, preserving offline
isolation (updates cannot reset retention or export keys).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from cv_crypto.provider import hexdigest
from cv_crypto.signing import HybridSigner

from .. import audit, fleet, security
from ..config import get_settings
from ..db import get_db
from ..models import Appliance, SoftwareRelease, UpdateJob

settings = get_settings()
router = APIRouter(prefix="/updates", tags=["updates"],
                   dependencies=[Depends(security.require_platform_admin)])


class PublishReleaseRequest(BaseModel):
    component: str  # cloud | appliance
    version: str
    package_url: str
    package_hash: str
    security_floor: str = "1.0.0"
    channel: str = "stable"


@router.post("/releases")
def publish_release(body: PublishReleaseRequest,
                    principal: security.Principal = Depends(security.require_platform_admin),
                    db: Session = Depends(get_db)):
    manifest_payload = {
        "component": body.component,
        "version": body.version,
        "channel": body.channel,
        "packageUrl": body.package_url,
        "packageHash": body.package_hash,
        "securityFloor": body.security_floor,
        "rollbackPolicy": "retain-previous",
    }
    # Hybrid-signed update manifest (classical + ML-DSA) — spec 11.
    manifest = {"payload": manifest_payload,
                "signature": fleet.fleet_signer().sign(manifest_payload)}
    release = SoftwareRelease(
        component=body.component,
        version=body.version,
        channel=body.channel,
        package_url=body.package_url,
        package_hash=body.package_hash,
        security_floor=body.security_floor,
        manifest=manifest,
    )
    db.add(release)
    db.commit()
    db.refresh(release)
    audit.record(db, actor=principal.user_id, action="release.published",
                 detail={"component": body.component, "version": body.version})
    return {"id": release.id, "version": release.version, "manifest": manifest}


@router.get("/releases")
def list_releases(component: str | None = None, db: Session = Depends(get_db)):
    q = db.query(SoftwareRelease)
    if component:
        q = q.filter(SoftwareRelease.component == component)
    rows = q.order_by(SoftwareRelease.created_at.desc()).all()
    return [{"id": r.id, "component": r.component, "version": r.version,
             "channel": r.channel, "package_url": r.package_url,
             "package_hash": r.package_hash, "security_floor": r.security_floor,
             "created_at": r.created_at.isoformat()} for r in rows]


class TriggerUpdateRequest(BaseModel):
    release_id: str
    target_type: str  # cloud | appliance
    target_id: str | None = None  # appliance id
    approval_mode: str = "maintenance-window"


@router.post("/trigger")
def trigger_update(body: TriggerUpdateRequest,
                   principal: security.Principal = Depends(security.require_platform_admin),
                   db: Session = Depends(get_db)):
    release = db.get(SoftwareRelease, body.release_id)
    if not release:
        raise HTTPException(404, "release not found")

    job = UpdateJob(
        target_type=body.target_type,
        target_id=body.target_id,
        release_id=release.id,
        approval_mode=body.approval_mode,
        status="scheduled",
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    if body.target_type == "appliance" and body.target_id:
        appliance = db.get(Appliance, body.target_id)
        if not appliance:
            raise HTTPException(404, "appliance not found")
        # Staged install via signed command; appliance verifies + rollback-guards.
        fleet.issue_command(db, appliance, "STAGE_UPDATE", principal.user_id,
                            {"version": release.version, "packageUrl": release.package_url,
                             "packageHash": release.package_hash,
                             "manifest": release.manifest,
                             "approvalMode": body.approval_mode})
    audit.record(db, actor=principal.user_id, action="update.triggered",
                 detail={"target": body.target_type, "version": release.version})
    return {"job_id": job.id, "status": job.status,
            "note": "Cloud updates run via the updater script polling this job; "
                    "appliance updates delivered as signed STAGE_UPDATE command."}


@router.get("/jobs")
def list_jobs(db: Session = Depends(get_db)):
    rows = db.query(UpdateJob).order_by(UpdateJob.created_at.desc()).all()
    return [{"id": j.id, "target_type": j.target_type, "target_id": j.target_id,
             "release_id": j.release_id, "status": j.status,
             "approval_mode": j.approval_mode,
             "created_at": j.created_at.isoformat()} for j in rows]


# Unauthenticated endpoint the cloud updater script polls for its own pending
# job. In production this is authenticated with the server's machine identity.
public_router = APIRouter(prefix="/self-update", tags=["updates"])


@public_router.get("/pending")
def pending_cloud_update(db: Session = Depends(get_db)):
    job = (db.query(UpdateJob)
           .filter(UpdateJob.target_type == "cloud", UpdateJob.status == "scheduled")
           .order_by(UpdateJob.created_at.asc()).first())
    if not job:
        return {"pending": False}
    release = db.get(SoftwareRelease, job.release_id)
    return {"pending": True, "job_id": job.id, "version": release.version,
            "package_url": release.package_url, "package_hash": release.package_hash,
            "manifest": release.manifest}
