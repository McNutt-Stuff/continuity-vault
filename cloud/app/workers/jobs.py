"""Background backup/sync jobs with live progress.

Long pulls (e.g. a full Gmail backup) run in a daemon thread and stream progress
into a ``SyncJob`` row so the Activity view and Data Map can show them in flight.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import WorkerSessionLocal as SessionLocal
from ..models import Collection, ConnectorAccount, SyncJob, Tenant
from .sync_worker import (
    JobCancelled,
    access_token_for_account,
    crawl_has_more,
    crawl_resume_after,
    existing_object_ids,
    ingest_objects,
    run_backup,
)

logger = logging.getLogger("cv.jobs")

# In-process set of job ids an operator asked to stop. The runner also checks the
# DB status ("cancelling") so a cancel issued from another process is honoured.
_CANCEL_REQUESTS: set = set()

_JOB_LOG_MAX = 800  # keep the tail; a full-library crawl can log a lot


class _JobLogCapture(logging.Handler):
    """Buffers THIS thread's ``cv.*`` log records so they can be attached to a
    SyncJob as a verbose process log (success and failure)."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self._tid = threading.get_ident()
        self.records: list[dict] = []
        self.setFormatter(logging.Formatter("%(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread != self._tid:
            return  # ignore other concurrent jobs sharing the cv logger
        try:
            self.records.append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "msg": self.format(record)[:1000],
            })
            if len(self.records) > _JOB_LOG_MAX * 2:
                del self.records[:_JOB_LOG_MAX]  # trim so memory stays bounded
        except Exception:
            pass


@contextlib.contextmanager
def capture_job_log():
    """Attach a thread-scoped capture handler to the ``cv`` logger for the duration
    of a job so all its INFO/WARN/ERROR records become the job's process log."""
    cvlog = logging.getLogger("cv")
    if cvlog.level == logging.NOTSET or cvlog.level > logging.INFO:
        cvlog.setLevel(logging.INFO)  # ensure INFO records reach handlers
    cap = _JobLogCapture()
    cvlog.addHandler(cap)
    try:
        yield cap
    finally:
        cvlog.removeHandler(cap)



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
    """Create a tracked job. If the tenant is assigned to a node and we're the
    control plane, the job is left QUEUED for that node to pick up on its next
    replication pull and run locally — a portal "Back up now" then executes on the
    assigned node. Otherwise it runs inline in a background thread (customer node
    running its own tenants, or an unassigned tenant)."""
    s = get_settings()
    t = db.get(Tenant, tenant_id)
    node_id = t.node_id if t else None
    is_cp = (s.node_role or "control-plane") == "control-plane"
    # Ownership is authoritative: if the tenant is assigned to a node, the control
    # plane must never run the job itself — queue it for the owning node regardless
    # of the federation flag, so an assigned tenant is never double-run here.
    dispatch_to_node = bool(is_cp and node_id)
    job = SyncJob(tenant_id=tenant_id, collection_id=collection_id, kind=kind,
                  node_id=node_id, status="queued",
                  message="Queued for node" if dispatch_to_node else "Queued")
    db.add(job)
    db.commit()
    db.refresh(job)
    if not dispatch_to_node:
        threading.Thread(target=_run, args=(job.id, destinations),
                         name=f"cv-job-{job.id[:8]}", daemon=True).start()
    return job


def ensure_backfill_running(db: Session, collection: Collection) -> Optional[SyncJob]:
    """For a dual-track source, make sure the independent deep-history backfill is
    progressing: start one backfill job if the crawl isn't finished and none is
    already active. Safe to call every scheduler tick (it's a cheap no-op once the
    backfill is complete or already running)."""
    if not collection.connector_account_id:
        return None
    from ..connectors import get_connector
    conn = get_connector(collection.source_type)
    if conn is None or not conn.capabilities().dual_track:
        return None
    acct = db.get(ConnectorAccount, collection.connector_account_id)
    if acct is None or acct.active is False or acct.backfill_done:
        return None
    # Back-compat: an account whose delta cursor is already established (old full
    # backup completed) is treated as fully backfilled — don't re-crawl it.
    if acct.backfill_cursor is None:
        old = acct.sync_cursor if isinstance(acct.sync_cursor, dict) else {}
        if old.get("history_id") and not old.get("has_more"):
            acct.backfill_done = True
            db.commit()
            return None
    # Only one backfill job per collection at a time.
    active = (db.query(SyncJob)
              .filter(SyncJob.collection_id == collection.id,
                      SyncJob.kind == "backfill",
                      SyncJob.status.in_(["queued", "running"])).first())
    if active is not None:
        return active
    logger.info("starting deep-history backfill for %s (%s)", collection.name,
                collection.source_type)
    return start_backup_job(db, collection.tenant_id, collection.id, kind="backfill",
                            destinations=collection.destinations or None)


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
        # Backfill jobs run the independent backward deep-history crawl; every
        # other job runs the fast forward/recent track.
        mode = "backfill" if job.kind == "backfill" else "recent"
        with capture_job_log() as cap:
            logger.info("%s job %s starting: %s (%s) → %s", mode, job.id, collection.name,
                        collection.source_type,
                        destinations or collection.destinations or ["cv-cloud"])
            try:
                receipt = None
                # Big-history sources (e.g. Gmail backfill, Google Photos) crawl in
                # resumable chunks: keep pulling while the persisted cursor reports
                # more, so one job can span hours without holding the whole library
                # in memory. Guarded by a wall-clock and iteration cap so a runaway
                # source can't loop forever.
                deadline = time.time() + 6 * 3600
                for _ in range(100000):
                    if _cancel_requested(db, job.id):
                        raise JobCancelled()
                    receipt = run_backup(db, collection, destinations, progress=progress, mode=mode)
                    base["n"] = job.processed  # carry the running total into the next chunk
                    db.refresh(collection)
                    if not crawl_has_more(db, collection, mode) or time.time() > deadline:
                        break
                    # A connector can ask us to wait before the next chunk (e.g. a
                    # GitHub rate-limit backoff): sleep until its reset, keeping the
                    # job "running" with a live countdown, bounded by the deadline.
                    resume_at = crawl_resume_after(db, collection, mode)
                    if resume_at and resume_at > time.time():
                        wait_end = min(resume_at + 2, deadline)
                        while time.time() < wait_end:
                            if _cancel_requested(db, job.id):
                                raise JobCancelled()
                            secs = int(wait_end - time.time())
                            job.message = (f"Waiting {secs}s for the source's rate limit "
                                           f"to reset… {job.processed:,} items so far")
                            db.commit()
                            time.sleep(min(15, max(1, wait_end - time.time())))
                    else:
                        job.message = f"Crawling… {job.processed:,} items so far"
                        db.commit()
                        time.sleep(1)  # gentle pacing between chunks
                job.snapshot_id = getattr(receipt, "snapshot_id", None)
                job.total = job.total or job.processed
                job.processed = max(job.processed, 0)
                job.status = "done"
                job.message = "Completed"
                logger.info("backup job %s complete: %d processed, snapshot=%s",
                            job.id, job.processed, job.snapshot_id or "—")
            except JobCancelled:
                db.rollback()
                j = db.get(SyncJob, job_id)
                if j is not None:
                    j.status = "cancelled"
                    j.message = "Stopped by operator"
                    j.log = (cap.records or [])[-_JOB_LOG_MAX:]
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
        job.log = (cap.records or [])[-_JOB_LOG_MAX:]
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
