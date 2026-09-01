"""
Durable activity queue for protection writes to destinations that may be
temporarily unreachable — an offline appliance, or a cloud / customer-storage
backend that rejected a write. When a destination fails, ``enqueue`` records a
``QueueItem`` and a background worker (:mod:`app.workers.queue`) retries it with
backoff. Once the connection is restored the retry succeeds and the item is
marked ``done``, so the queue self-drains and no backup is silently lost.

The retry re-runs the source backup to the single failed destination (reusing the
normal ingest path), so no large ciphertext has to be persisted in the queue row.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

from .models import Collection, QueueItem

logger = logging.getLogger("cv.queue")

MAX_ATTEMPTS = 10
# Exponential backoff (minutes) by attempt count, capped at 1h.
_BACKOFF_MIN = [1, 2, 5, 10, 20, 30, 60]

_ACTIVE = ("queued", "delivering")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _backoff(attempts: int) -> datetime:
    idx = min(max(attempts - 1, 0), len(_BACKOFF_MIN) - 1)
    return _now() + timedelta(minutes=_BACKOFF_MIN[idx])


def kind_for_target(target: str) -> str:
    if target.startswith("appliance") or target.startswith("store:"):
        return "appliance_ingest"
    return "cloud_sync"


def target_label(target: str) -> str:
    if target == "cv-cloud":
        return "Arkive Cloud"
    if target == "customer-s3":
        return "Your cloud (S3)"
    if target.startswith("byos:"):
        return "Your cloud storage"
    if target.startswith("store:") or target.startswith("appliance"):
        return "Appliance"
    return target


def enqueue(db: Session, *, tenant_id: str, target: str, error: str,
            collection_id: str | None = None, snapshot_id: str | None = None,
            node_id: str | None = None, label: str = "") -> QueueItem:
    """Record (or refresh) a queued delivery for a failed destination. Idempotent
    per (tenant, collection, target): an existing active item is re-armed with a
    fresh backoff and a bumped attempt count rather than duplicated. Caller commits."""
    q = (db.query(QueueItem)
         .filter(QueueItem.tenant_id == tenant_id, QueueItem.target == target,
                 QueueItem.status.in_(_ACTIVE)))
    if collection_id is not None:
        q = q.filter(QueueItem.collection_id == collection_id)
    q = q.order_by(QueueItem.created_at.desc()).first()
    if q is None:
        q = QueueItem(tenant_id=tenant_id, node_id=node_id, collection_id=collection_id,
                      snapshot_id=snapshot_id, target=target, kind=kind_for_target(target),
                      label=label or target_label(target), attempts=1, max_attempts=MAX_ATTEMPTS,
                      status="queued", next_attempt_at=_backoff(1), last_error=str(error)[:500])
        db.add(q)
        logger.info("queued %s for tenant=%s collection=%s (%s)",
                    target, tenant_id, collection_id, error)
    else:
        q.attempts = int(q.attempts or 0) + 1
        q.status = "queued"
        q.snapshot_id = snapshot_id or q.snapshot_id
        q.last_error = str(error)[:500]
        q.next_attempt_at = _backoff(q.attempts)
        if node_id and not q.node_id:
            q.node_id = node_id
        if q.attempts >= q.max_attempts:
            q.status = "failed"
            q.next_attempt_at = None
            logger.warning("queue item %s gave up after %d attempts (%s)",
                           q.id, q.attempts, error)
    return q


def resolve(db: Session, *, tenant_id: str, target: str,
            collection_id: str | None = None) -> None:
    """Mark any active queued delivery for this destination done — called when a
    write to ``target`` succeeds, so a restored connection empties the queue.
    Caller commits."""
    q = (db.query(QueueItem)
         .filter(QueueItem.tenant_id == tenant_id, QueueItem.target == target,
                 QueueItem.status.in_(_ACTIVE)))
    if collection_id is not None:
        q = q.filter(QueueItem.collection_id == collection_id)
    for item in q.all():
        item.status = "done"
        item.resolved_at = _now()
        item.next_attempt_at = None
        logger.info("queue item %s resolved (%s delivered)", item.id, target)


def due_items(db: Session, limit: int = 25) -> list[QueueItem]:
    return (db.query(QueueItem)
            .filter(QueueItem.status == "queued",
                    QueueItem.next_attempt_at.isnot(None),
                    QueueItem.next_attempt_at <= _now())
            .order_by(QueueItem.next_attempt_at.asc()).limit(limit).all())


def run_due(db: Session) -> int:
    """Retry every due queued delivery by re-running the source backup to the
    single failed destination. Success → done; failure → backoff (or failed after
    MAX_ATTEMPTS). Returns the number of items that drained successfully."""
    from .workers.sync_worker import run_backup
    drained = 0
    for item in due_items(db):
        coll = db.get(Collection, item.collection_id) if item.collection_id else None
        if coll is None:
            item.status = "canceled"
            item.last_error = "collection no longer exists"
            item.next_attempt_at = None
            db.commit()
            continue
        item.status = "delivering"
        db.commit()
        try:
            run_backup(db, coll, [item.target])
            item.status = "done"
            item.resolved_at = _now()
            item.next_attempt_at = None
            item.last_error = ""
            drained += 1
            logger.info("queue item %s drained → %s", item.id, item.target)
        except Exception as exc:  # noqa: BLE001 — one bad item never stops the drain
            db.rollback()
            db.refresh(item)
            # If the write reached the per-destination stage, ingest_objects has
            # already re-armed this item (bumped attempt + backoff). Only apply
            # backoff here when it's still 'delivering' (e.g. a source-fetch error
            # before any destination was attempted).
            if item.status == "delivering":
                item.attempts = int(item.attempts or 0) + 1
                item.last_error = str(exc)[:500]
                if item.attempts >= item.max_attempts:
                    item.status = "failed"
                    item.next_attempt_at = None
                    logger.warning("queue item %s failed permanently: %s", item.id, exc)
                else:
                    item.status = "queued"
                    item.next_attempt_at = _backoff(item.attempts)
                    logger.info("queue item %s still unreachable (attempt %d): %s",
                                item.id, item.attempts, exc)
            else:
                logger.info("queue item %s still unreachable: %s", item.id, exc)
        db.commit()
    return drained


def retry(db: Session, qid: str) -> QueueItem | None:
    q = db.get(QueueItem, qid)
    if q is None:
        return None
    q.status = "queued"
    q.next_attempt_at = _now()
    db.commit()
    return q


def cancel(db: Session, qid: str) -> QueueItem | None:
    q = db.get(QueueItem, qid)
    if q is None:
        return None
    q.status = "canceled"
    q.next_attempt_at = None
    db.commit()
    return q


def view(item: QueueItem) -> dict:
    return {
        "id": item.id, "tenant_id": item.tenant_id, "node_id": item.node_id,
        "collection_id": item.collection_id, "snapshot_id": item.snapshot_id,
        "kind": item.kind, "target": item.target,
        "target_label": target_label(item.target), "label": item.label,
        "status": item.status, "attempts": int(item.attempts or 0),
        "max_attempts": int(item.max_attempts or MAX_ATTEMPTS),
        "next_attempt_at": item.next_attempt_at.isoformat() if item.next_attempt_at else None,
        "last_error": item.last_error or "",
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
        "resolved_at": item.resolved_at.isoformat() if item.resolved_at else None,
    }


def list_items(db: Session, *, node_id: str | None = None, include_self_null: bool = False,
               limit: int = 200) -> dict:
    """Active items first, then recently-resolved. When ``include_self_null`` the
    control-plane-owned items (node_id NULL) are included alongside ``node_id``."""
    q = db.query(QueueItem)
    if node_id is not None:
        if include_self_null:
            q = q.filter(or_(QueueItem.node_id == node_id, QueueItem.node_id.is_(None)))
        else:
            q = q.filter(QueueItem.node_id == node_id)
    rows = q.order_by(QueueItem.created_at.desc()).limit(limit).all()
    active = [view(r) for r in rows if r.status in _ACTIVE]
    recent = [view(r) for r in rows if r.status not in _ACTIVE][:50]
    return {"active": active, "recent": recent,
            "counts": {"active": len(active),
                       "failed": sum(1 for r in rows if r.status == "failed")}}
