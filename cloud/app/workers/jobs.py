"""Background backup/sync jobs with live progress.

Long pulls (e.g. a full Gmail backup) run in a daemon thread and stream progress
into a ``SyncJob`` row so the Activity view and Data Map can show them in flight.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import Collection, ConnectorAccount, SyncJob
from .sync_worker import (
    JobCancelled,
    access_token_for_account,
    crawl_has_more,
    existing_object_ids,
    ingest_objects,
    run_backup,
)

logger = logging.getLogger("cv.jobs")

# In-process set of job ids an operator asked to stop. The runner also checks the
# DB status ("cancelling") so a cancel issued from another process is honoured.
_CANCEL_REQUESTS: set = set()


def request_cancel(job_id: str) -> None:
    _CANCEL_REQUESTS.add(job_id)


def _cancel_requested(db: Session, job_id: str) -> bool:
    if job_id in _CANCEL_REQUESTS:
        return True
    return db.query(SyncJob.status).filter(SyncJob.id == job_id).scalar() == "cancelling"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def start_backup_job(db: Session, tenant_id: str, collection_id: str,
                     kind: str = "backup",
                     destinations: Optional[List[str]] = None) -> SyncJob:
    """Create a tracked job and run the backup in the background."""
    job = SyncJob(tenant_id=tenant_id, collection_id=collection_id, kind=kind,
                  status="queued", message="Queued")
    db.add(job)
    db.commit()
    db.refresh(job)
    threading.Thread(target=_run, args=(job.id, destinations),
                     name=f"cv-job-{job.id[:8]}", daemon=True).start()
    return job


def _run(job_id: str, destinations: Optional[List[str]]) -> None:
    with SessionLocal() as db:
        job = db.get(SyncJob, job_id)
        if not job:
            return
        job.status = "running"
        job.started_at = _now()
        job.message = "Starting…"
        db.commit()

        collection = db.get(Collection, job.collection_id)
        if collection is None:
            job.status = "failed"
            job.error = "source no longer exists"
            job.finished_at = _now()
            db.commit()
            return

        def progress(done: int, total: int, message: str) -> None:
            if _cancel_requested(db, job.id):
                raise JobCancelled()
            job.processed = base["n"] + int(done)
            job.total = max(int(job.total or 0), job.processed, base["n"] + int(total))
            job.message = (message or "")[:200]
            db.commit()

        base = {"n": 0}
        try:
            receipt = None
            # Big-history sources (e.g. Google Photos) crawl in resumable chunks:
            # keep pulling while the persisted cursor reports more, so one job can
            # span hours without holding the whole library in memory. Guarded by a
            # wall-clock and iteration cap so a runaway source can't loop forever.
            deadline = time.time() + 6 * 3600
            for _ in range(100000):
                if _cancel_requested(db, job.id):
                    raise JobCancelled()
                receipt = run_backup(db, collection, destinations, progress=progress)
                base["n"] = job.processed  # carry the running total into the next chunk
                db.refresh(collection)
                if not crawl_has_more(db, collection) or time.time() > deadline:
                    break
                job.message = f"Crawling… {job.processed:,} items so far"
                db.commit()
                time.sleep(1)  # gentle pacing between chunks
            job.snapshot_id = getattr(receipt, "snapshot_id", None)
            job.total = job.total or job.processed
            job.processed = max(job.processed, 0)
            job.status = "done"
            job.message = "Completed"
        except JobCancelled:
            db.rollback()
            j = db.get(SyncJob, job_id)
            if j is not None:
                j.status = "cancelled"
                j.message = "Stopped by operator"
                j.finished_at = _now()
                db.commit()
            _CANCEL_REQUESTS.discard(job_id)
            logger.info("backup job %s cancelled", job_id)
            return
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            logger.exception("backup job %s failed", job_id)
            job.status = "failed"
            job.error = str(exc)[:400]
            job.message = "Failed"
        _CANCEL_REQUESTS.discard(job_id)
        job.finished_at = _now()
        db.commit()


def start_picker_import_job(db: Session, tenant_id: str, collection_id: str,
                            session_id: str) -> SyncJob:
    """Import the media a user selected in a Google Photos picker session, in the
    background (must finish within the session's validity window)."""
    job = SyncJob(tenant_id=tenant_id, collection_id=collection_id, kind="import",
                  status="queued", message="Queued")
    db.add(job)
    db.commit()
    db.refresh(job)
    threading.Thread(target=_run_picker_import, args=(job.id, session_id),
                     name=f"cv-pick-{job.id[:8]}", daemon=True).start()
    return job


def _run_picker_import(job_id: str, session_id: str) -> None:
    from ..config import get_settings
    from ..connectors import get_connector, live

    with SessionLocal() as db:
        job = db.get(SyncJob, job_id)
        if not job:
            return
        job.status = "running"
        job.started_at = _now()
        job.message = "Reading your selection…"
        db.commit()

        collection = db.get(Collection, job.collection_id)
        account = (db.get(ConnectorAccount, collection.connector_account_id)
                   if collection and collection.connector_account_id else None)
        token = access_token_for_account(db, account) if account else None
        if not collection or not token:
            job.status = "failed"
            job.error = "source not linked or token unavailable"
            job.finished_at = _now()
            db.commit()
            return

        caps = get_connector("google_photos").capabilities()
        cap_bytes = get_settings().content_max_bytes
        dests = collection.destinations or ["cv-cloud"]
        known = existing_object_ids(db, collection.id)
        batch: List = []
        seen = imported = 0

        def flush():
            nonlocal batch
            if not batch:
                return
            ingest_objects(db, collection, batch, dests,
                           searchable_fields=caps.searchable_fields,
                           facet_fields=caps.facet_fields, actor="picker-import")
            for o in batch:
                known.add(o.object_id)
            batch = []

        try:
            for item in live.iter_picker_media(token, session_id):
                seen += 1
                oid = f"google_photos:{item.get('id')}"
                if oid in known:  # already backed up in a prior session — skip
                    continue
                batch.append(live.picker_item_to_object(item, cap_bytes, token))
                imported += 1
                if len(batch) >= 50:
                    flush()
                    job.processed = imported
                    job.message = f"Imported {imported} new item(s)…"
                    db.commit()
            flush()
            account.last_sync_at = _now()
            live.delete_picker_session(token, session_id)
            job.processed = imported
            job.total = imported
            job.status = "done"
            job.message = (f"Imported {imported} new item(s)"
                           if imported else f"Nothing new ({seen} already backed up)")
        except Exception as exc:  # noqa: BLE001
            logger.exception("picker import %s failed", job_id)
            job.status = "failed"
            job.error = str(exc)[:400]
            job.message = "Failed"
        job.finished_at = _now()
        db.commit()
