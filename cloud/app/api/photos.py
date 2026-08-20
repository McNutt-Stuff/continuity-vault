"""
Google Photos Picker import + customer pending-actions.

Google no longer allows unattended full-library reads, so photos are captured
through the Picker API: the user creates a session, picks photos/albums in
Google's UI, and Arkive imports only what's new (deduped against prior backups).
A scheduler reminder surfaces a "pick new photos" action on a cadence.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import audit, security
from ..connectors import live
from ..db import get_db
from ..models import Collection, ConnectorAccount, PendingAction, Tenant, Vault
from ..workers.jobs import start_picker_import_job
from ..workers.sync_worker import access_token_for_account

router = APIRouter(prefix="/photos", tags=["photos"])
actions_router = APIRouter(prefix="/actions", tags=["actions"])


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _account(db: Session, tenant: Tenant, account_id: str) -> ConnectorAccount:
    a = db.get(ConnectorAccount, account_id)
    if not a or a.tenant_id != tenant.id or a.connector_type != "google_photos":
        raise HTTPException(404, "google photos account not found")
    return a


class PickerSessionRequest(BaseModel):
    account_id: str


@router.post("/picker/session")
def create_session(body: PickerSessionRequest,
                   principal: security.Principal = Depends(security.get_principal),
                   tenant: Tenant = Depends(security.get_tenant),
                   db: Session = Depends(get_db)):
    account = _account(db, tenant, body.account_id)
    token = access_token_for_account(db, account)
    if not token:
        raise HTTPException(400, "account needs to be reconnected")
    try:
        sess = live.create_picker_session(token)
    except Exception as exc:
        raise HTTPException(502, f"could not start Google Photos picker: {exc}")
    return {"session_id": sess.get("id"), "picker_uri": sess.get("pickerUri"),
            "poll_interval_ms": 3000, "expire_time": sess.get("expireTime")}


@router.get("/picker/session/{session_id}")
def poll_session(session_id: str, account_id: str,
                 principal: security.Principal = Depends(security.get_principal),
                 tenant: Tenant = Depends(security.get_tenant),
                 db: Session = Depends(get_db)):
    account = _account(db, tenant, account_id)
    token = access_token_for_account(db, account)
    if not token:
        raise HTTPException(400, "account needs to be reconnected")
    try:
        sess = live.get_picker_session(token, session_id)
    except Exception as exc:
        raise HTTPException(502, f"picker session error: {exc}")
    return {"media_items_set": bool(sess.get("mediaItemsSet")),
            "expire_time": sess.get("expireTime")}


def _ensure_photos_collection(db: Session, tenant: Tenant, account: ConnectorAccount,
                              vault_id: str | None) -> Collection:
    coll = (db.query(Collection)
            .filter(Collection.tenant_id == tenant.id,
                    Collection.source_type == "google_photos",
                    Collection.connector_account_id == account.id).first())
    if coll:
        return coll
    vault = (db.get(Vault, vault_id) if vault_id else None) or \
        db.query(Vault).filter(Vault.tenant_id == tenant.id).first()
    if not vault:
        raise HTTPException(400, "no vault available to store photos")
    coll = Collection(tenant_id=tenant.id, vault_id=vault.id,
                      name=account.account_label or "Google Photos",
                      source_type="google_photos", connector_account_id=account.id,
                      sensitivity="standard", destinations=["cv-cloud"])
    db.add(coll)
    db.commit()
    db.refresh(coll)
    return coll


class PickerImportRequest(BaseModel):
    account_id: str
    session_id: str
    vault_id: str | None = None


@router.post("/picker/import")
def import_selection(body: PickerImportRequest,
                     principal: security.Principal = Depends(security.get_principal),
                     tenant: Tenant = Depends(security.get_tenant),
                     db: Session = Depends(get_db)):
    account = _account(db, tenant, body.account_id)
    coll = _ensure_photos_collection(db, tenant, account, body.vault_id)
    job = start_picker_import_job(db, tenant.id, coll.id, body.session_id)
    # Clear any open reminder for this source — the user just acted on it.
    for a in (db.query(PendingAction)
              .filter(PendingAction.collection_id == coll.id,
                      PendingAction.status == "open").all()):
        a.status = "done"
    db.commit()
    audit.record(db, actor=principal.user_id, action="photos.import_started",
                 tenant_id=tenant.id, resource=coll.id)
    return {"job_id": job.id, "collection_id": coll.id}


# --- Pending actions --------------------------------------------------------

@actions_router.get("")
def list_actions(tenant: Tenant = Depends(security.get_tenant),
                 db: Session = Depends(get_db)):
    rows = (db.query(PendingAction)
            .filter(PendingAction.tenant_id == tenant.id,
                    PendingAction.status == "open")
            .order_by(PendingAction.created_at.desc()).all())
    out = []
    for a in rows:
        coll = db.get(Collection, a.collection_id) if a.collection_id else None
        account_id = coll.connector_account_id if coll else None
        out.append({
            "id": a.id, "kind": a.kind, "title": a.title, "message": a.message,
            "source_type": a.source_type, "collection_id": a.collection_id,
            "account_id": account_id,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        })
    return out


@actions_router.post("/{action_id}/dismiss")
def dismiss_action(action_id: str,
                   tenant: Tenant = Depends(security.get_tenant),
                   db: Session = Depends(get_db)):
    a = db.get(PendingAction, action_id)
    if not a or a.tenant_id != tenant.id:
        raise HTTPException(404, "action not found")
    a.status = "dismissed"
    db.commit()
    return {"ok": True}
