"""Tamper-evident, hash-chained audit ledger (spec 2.5, 14)."""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from cv_crypto.provider import hexdigest

from .models import AuditEvent


def record(db: Session, actor: str, action: str, tenant_id: Optional[str] = None,
           resource: str = "", detail: Optional[dict] = None) -> AuditEvent:
    last = (
        db.query(AuditEvent)
        .order_by(AuditEvent.created_at.desc())
        .first()
    )
    prev_hash = last.entry_hash if last else ""
    body = f"{prev_hash}|{actor}|{action}|{resource}|{detail}"
    entry_hash = hexdigest(body.encode())
    event = AuditEvent(
        tenant_id=tenant_id,
        actor=actor,
        action=action,
        resource=resource,
        detail=detail or {},
        prev_hash=prev_hash,
        entry_hash=entry_hash,
    )
    db.add(event)
    db.commit()
    return event


def verify_chain(db: Session) -> bool:
    prev = ""
    for event in db.query(AuditEvent).order_by(AuditEvent.created_at.asc()).all():
        body = f"{prev}|{event.actor}|{event.action}|{event.resource}|{event.detail}"
        if hexdigest(body.encode()) != event.entry_hash:
            return False
        prev = event.entry_hash
    return True
