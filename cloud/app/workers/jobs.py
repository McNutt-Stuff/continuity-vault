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
from ..models import Collection, SyncJob
from .sync_worker import crawl_has_more, run_backup

logger = logging.getLogger("cv.jobs")


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
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            logger.exception("backup job %s failed", job_id)
            job.status = "failed"
            job.error = str(exc)[:400]
            job.message = "Failed"
        job.finished_at = _now()
        db.commit()
