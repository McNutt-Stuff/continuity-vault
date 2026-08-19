"""Recovery window: view decrypted items that were brought out of storage.

When an item is recovered it's decrypted into a temporary store and registered as
a ``RecoveredItem`` with an expiry. It can be viewed/downloaded until it expires
(or is destroyed), then the plaintext is purged — a time-limited recovery window,
not a lasting copy.
"""

from __future__ import annotations

import logging
import mimetypes
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .. import audit, security
from ..config import get_settings
from ..db import get_db
from ..models import ObjectVersion, RecoveredItem, Tenant

router = APIRouter(prefix="/recovered", tags=["recovery"])
logger = logging.getLogger("cv.recovery")

_STORE = Path(os.environ.get("CV_RECOVERED_STORE", "/var/lib/continuity-vault/recovered"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _path(item_id: str) -> Path:
    _STORE.mkdir(parents=True, exist_ok=True)
    return _STORE / item_id


def guess_mime(title: str, doc_type: str) -> str:
    if doc_type == "email":
        return "message/rfc822"
    if doc_type in ("secret", "login", "note", "password", "identity"):
        return "application/json"
    mt, _ = mimetypes.guess_type(title or "")
    return mt or "application/octet-stream"


def create_recovered(db: Session, tenant_id: str, actor: str, *, object_id: str,
                     snapshot_id: str, title: str, doc_type: str, source_type: str,
                     location: str, content: bytes) -> RecoveredItem:
    """Stage decrypted content in the temporary store and register the window."""
    ttl = get_settings().recovered_ttl_seconds
    # Record which stored version this recovery came from (and when it was
    # captured) so the recovered view can show the point-in-time it represents.
    ov = (db.query(ObjectVersion)
          .filter(ObjectVersion.tenant_id == tenant_id,
                  ObjectVersion.object_id == object_id,
                  ObjectVersion.snapshot_id == snapshot_id).first())
    item = RecoveredItem(
        tenant_id=tenant_id, object_id=object_id, snapshot_id=snapshot_id,
        title=title or object_id, doc_type=doc_type, source_type=source_type,
        mime=guess_mime(title, doc_type), size_bytes=len(content),
        location=location, requested_by=actor,
        version=ov.version if ov else None,
        version_created_at=ov.created_at if ov else None,
        expires_at=_now().replace(tzinfo=None) + timedelta(seconds=ttl),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    _path(item.id).write_bytes(content)
    return item


def _destroy(item: RecoveredItem) -> None:
    try:
        p = _path(item.id)
        if p.exists():
            p.unlink()
    except Exception:
        pass
    item.destroyed = True


def purge_expired(db: Session) -> int:
    """Destroy any recovery windows that have passed their expiry."""
    now = _now().replace(tzinfo=None)
    rows = (db.query(RecoveredItem)
            .filter(RecoveredItem.destroyed.is_(False),
                    RecoveredItem.expires_at < now).all())
    for r in rows:
        _destroy(r)
    if rows:
        db.commit()
    return len(rows)


def _view(item: RecoveredItem) -> dict:
    remaining = int((item.expires_at - _now().replace(tzinfo=None)).total_seconds())
    return {
        "id": item.id, "object_id": item.object_id, "title": item.title,
        "doc_type": item.doc_type, "source_type": item.source_type,
        "mime": item.mime, "size_bytes": item.size_bytes, "location": item.location,
        "version": item.version,
        "version_created_at": item.version_created_at.isoformat() if item.version_created_at else None,
        "created_at": item.created_at.isoformat(),
        "expires_at": item.expires_at.isoformat(),
        "expires_in_seconds": max(0, remaining),
        "viewed": item.viewed_at is not None,
    }


@router.get("")
def list_recovered(principal: security.Principal = Depends(security.get_principal),
                   tenant: Tenant = Depends(security.get_tenant),
                   db: Session = Depends(get_db)):
    purge_expired(db)
    rows = (db.query(RecoveredItem)
            .filter(RecoveredItem.tenant_id == tenant.id,
                    RecoveredItem.destroyed.is_(False))
            .order_by(RecoveredItem.created_at.desc()).all())
    return {"items": [_view(r) for r in rows]}


@router.get("/{item_id}/content")
def view_content(item_id: str,
                 principal: security.Principal = Depends(security.require_passkey),
                 tenant: Tenant = Depends(security.get_tenant),
                 db: Session = Depends(get_db)):
    item = db.get(RecoveredItem, item_id)
    if not item or item.tenant_id != tenant.id or item.destroyed:
        raise HTTPException(404, "recovered item not found or destroyed")
    if item.expires_at < _now().replace(tzinfo=None):
        _destroy(item)
        db.commit()
        raise HTTPException(410, "recovery window expired")
    try:
        data = _path(item.id).read_bytes()
    except Exception:
        raise HTTPException(410, "recovered content no longer available")
    item.viewed_at = _now().replace(tzinfo=None)
    db.commit()
    audit.record(db, actor=principal.user_id, action="recovery.viewed",
                 tenant_id=tenant.id, resource=item.object_id,
                 detail={"location": item.location, "bytes": item.size_bytes})
    return Response(content=data, media_type=item.mime,
                    headers={"Content-Disposition": f'inline; filename="{item.title}"'})


@router.delete("/{item_id}")
def destroy(item_id: str,
            principal: security.Principal = Depends(security.get_principal),
            tenant: Tenant = Depends(security.get_tenant),
            db: Session = Depends(get_db)):
    item = db.get(RecoveredItem, item_id)
    if not item or item.tenant_id != tenant.id:
        raise HTTPException(404, "recovered item not found")
    _destroy(item)
    db.commit()
    audit.record(db, actor=principal.user_id, action="recovery.destroyed",
                 tenant_id=tenant.id, resource=item.object_id,
                 detail={"location": item.location})
    return {"ok": True}
