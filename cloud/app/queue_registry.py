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
                      label=label or target_label(target), attempts=1, max_attempts=0,
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
    single failed destination. Success → done; failure → re-armed with backoff.

    There is NO retry limit: an item keeps retrying (backoff capped at 1h) until it
    succeeds and self-clears, or an admin cancels it. Any items previously parked as
    ``failed`` (legacy cap) are revived so nothing stays stuck."""
    from .workers.sync_worker import run_backup
    revived = db.query(QueueItem).filter(QueueItem.status == "failed").all()
    for it in revived:
        it.status = "queued"
        it.next_attempt_at = _backoff(int(it.attempts or 1))
    if revived:
        db.commit()
        logger.info("revived %d queue item(s) from legacy failed state", len(revived))
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
            # before any destination was attempted). Never give up — always re-queue.
            if item.status == "delivering":
                item.attempts = int(item.attempts or 0) + 1
                item.last_error = str(exc)[:500]
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
        "max_attempts": int(item.max_attempts or 0),
        "next_attempt_at": item.next_attempt_at.isoformat() if item.next_attempt_at else None,
        "last_error": item.last_error or "",
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
        "resolved_at": item.resolved_at.isoformat() if item.resolved_at else None,
    }


def list_items(db: Session, *, node_id: str | None = None, include_self_null: bool = False,
               limit: int = 200) -> dict:
    """Active items first, then recently-resolved. When ``include_self_null`` the
    control-plane-owned items (node_id NULL) are included alongside ``node_id``.

    Also surfaces undeliverable **appliance commands** (pending / rejected) as
    active rows so an offline appliance's stuck commands show up next to backup
    retries — not just durable destination writes."""
    q = db.query(QueueItem)
    if node_id is not None:
        if include_self_null:
            q = q.filter(or_(QueueItem.node_id == node_id, QueueItem.node_id.is_(None)))
        else:
            q = q.filter(QueueItem.node_id == node_id)
    rows = q.order_by(QueueItem.created_at.desc()).limit(limit).all()
    q_active = [view(r) for r in rows if r.status in _ACTIVE]
    recent = [view(r) for r in rows if r.status not in _ACTIVE][:50]
    cmd_items = _appliance_command_items(db, node_id, include_self_null)
    active = q_active + cmd_items  # command rows are all current problems
    return {"active": active, "recent": recent,
            "counts": {"active": len(q_active) + sum(1 for c in cmd_items if c["status"] == "waiting"),
                       "failed": sum(1 for r in rows if r.status == "failed")
                       + sum(1 for c in cmd_items if c["status"] == "rejected")}}


_APPLIANCE_ONLINE_S = 120
_CMD_LABELS = {
    "OPEN_INGEST_WINDOW": "Store backup", "SETUP_STORAGE": "Set up storage",
    "RECONFIGURE_STORAGE": "Reconfigure storage",
}


def _command_label(t: str) -> str:
    return _CMD_LABELS.get(t or "", (t or "command").replace("_", " ").title())


def _appliance_command_items(db: Session, node_id: str | None,
                             include_self_null: bool) -> list[dict]:
    """Pending (undelivered) and rejected appliance commands as queue-style rows.
    Pending → ``waiting`` (delivers on the appliance's next heartbeat; flagged
    offline when it hasn't beaten recently); rejected → ``rejected``."""
    from .models import Appliance, ApplianceCommand, Tenant
    cmds = (db.query(ApplianceCommand)
            .filter(ApplianceCommand.status.in_(["pending", "rejected"]))
            .order_by(ApplianceCommand.created_at.desc()).limit(200).all())
    if not cmds:
        return []
    now = _now()
    appl: dict = {}
    tnode: dict = {}
    out: list[dict] = []
    for cmd in cmds:
        if cmd.appliance_id not in appl:
            appl[cmd.appliance_id] = db.get(Appliance, cmd.appliance_id)
        a = appl[cmd.appliance_id]
        if a is None:
            continue
        if node_id is not None:
            if a.tenant_id not in tnode:
                t = db.get(Tenant, a.tenant_id)
                tnode[a.tenant_id] = t.node_id if t else None
            nid = tnode[a.tenant_id]
            if include_self_null:
                if not (nid == node_id or nid is None):
                    continue
            elif nid != node_id:
                continue
        hb = a.last_heartbeat_at
        if hb is not None and hb.tzinfo is not None:
            hb = hb.replace(tzinfo=None)
        online = bool(hb and (now - hb).total_seconds() < _APPLIANCE_ONLINE_S)
        name = a.name or "Appliance"
        if cmd.status == "rejected":
            status = "rejected"
            res = cmd.result if isinstance(cmd.result, dict) else {}
            err = res.get("error") or res.get("message") or "Rejected by the appliance"
        else:
            status = "waiting"
            err = ("Appliance offline — will deliver when it reconnects" if not online
                   else "Waiting for the appliance to poll for commands")
        out.append({
            "id": f"cmd:{cmd.id}", "tenant_id": cmd.tenant_id, "node_id": node_id,
            "collection_id": None, "snapshot_id": None,
            "kind": "appliance_command", "target": "appliance",
            "target_label": name, "label": f"{name} · {_command_label(cmd.command_type)}",
            "status": status, "attempts": 0, "max_attempts": 0,
            "next_attempt_at": None, "last_error": err,
            "created_at": cmd.created_at.isoformat() if cmd.created_at else None,
            "updated_at": None, "resolved_at": None, "online": online,
        })
    return out
