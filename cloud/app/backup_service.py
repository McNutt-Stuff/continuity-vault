"""
Infrastructure backup service.

Backs up a node's (or the control plane's) core operational state — its Postgres
database (which holds config, tenants, recovery-point receipts and the search
index), plus the local key store and fleet signer — into one or more *backup
storage service objects* (Amazon S3 / Azure Blob) for disaster recovery.

The bundle is a single gzip tar, encrypted under the fleet KEK (so it is
restorable from any fleet member that shares ``CV_KEK_SECRET`` — never from the
ciphertext alone), and uploaded to EVERY assigned destination for resiliency
(assign different services, not the same one twice). Each run records a
``BackupRun`` with per-destination results.

Runs from the standalone backup worker (a systemd timer, outside the API
process) — see ``app.backup_worker``.
"""

from __future__ import annotations

import gzip
import io
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from . import credstore
from .config import get_settings
from .storage import destination_from_service

logger = logging.getLogger("cv.backup")

# Where the encrypted node-state bundles land in the backup bucket/container.
_BACKUP_PREFIX = "_backups"
# Fleet-KEK scope for the archive (shared, so any node/CP can restore it).
_ENC_SCOPE = "platform"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "-" for c in (name or "node"))[:80]


def _work_dir() -> Optional[str]:
    """Directory to stage the backup bundle in. Prefers CV_BACKUP_WORK_DIR, then
    the data volume (next to the object store), so a large DB dump doesn't fill a
    small root/``/tmp`` filesystem. Returns None (→ system temp) if none writable."""
    candidates = [
        get_settings().backup_work_dir,
        os.path.dirname(os.environ.get("CV_OBJECT_STORE", "").rstrip("/")) or None,
        "/var/lib/continuity-vault",
    ]
    for base in candidates:
        if not base:
            continue
        path = os.path.join(base, "backup-tmp")
        try:
            os.makedirs(path, exist_ok=True)
            if os.access(path, os.W_OK):
                return path
        except Exception:  # noqa: BLE001
            continue
    return None  # fall back to the system default temp dir


# --------------------------------------------------------------------------- #
# Bundle assembly                                                             #
# --------------------------------------------------------------------------- #

def _dump_database(dest_path: str) -> Tuple[bool, str]:
    """Dump the node's database to ``dest_path`` (gzip-compressed). Postgres →
    pg_dump piped through gzip (bounded memory, small on disk); SQLite → gzip copy.
    Returns (ok, note)."""
    url = get_settings().database_url
    if url.startswith("sqlite"):
        # sqlite:///relative or sqlite:////absolute
        path = url.split("://", 1)[1].lstrip("/")
        if url.startswith("sqlite:////"):
            path = "/" + path
        if not os.path.exists(path):
            return False, "sqlite file not found"
        try:
            with open(path, "rb") as src, gzip.open(dest_path, "wb", compresslevel=6) as gz:
                shutil.copyfileobj(src, gz, length=1024 * 1024)
            return True, "sqlite.gz"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)[:300]
    # Postgres — pg_dump/libpq only understand a bare postgres[ql]:// URI, so
    # strip any SQLAlchemy driver suffix (e.g. postgresql+psycopg:// → postgresql://).
    scheme, sep, rest = url.partition("://")
    if sep and "+" in scheme:
        url = scheme.split("+", 1)[0] + "://" + rest
    pg_dump = shutil.which("pg_dump") or "pg_dump"
    logger.info("backup: pg_dump starting (%s)", pg_dump)
    try:
        # Stream pg_dump → gzip so the SQL is never fully buffered and the staged
        # file stays small (a plain dump can be many GB and fill the disk).
        # stderr goes to a TEMP FILE, not a PIPE: if diagnostics exceed the ~64KB
        # pipe buffer while we're busy reading stdout, pg_dump would block writing
        # stderr and we'd block reading stdout — a deadlock that hangs the whole
        # backup forever ("runs but never finishes").
        with tempfile.TemporaryFile() as errf:
            proc = subprocess.Popen(
                [pg_dump, "--no-owner", "--no-privileges", url],
                stdout=subprocess.PIPE, stderr=errf)
            with gzip.open(dest_path, "wb", compresslevel=6) as gz:
                shutil.copyfileobj(proc.stdout, gz, length=1024 * 1024)
            proc.stdout.close()
            try:
                proc.wait(timeout=1800)
            except subprocess.TimeoutExpired:
                proc.kill()
                logger.error("backup: pg_dump timed out after 1800s")
                return False, "pg_dump timed out"
            errf.seek(0)
            err = errf.read()
        if proc.returncode != 0:
            msg = err.decode(errors="replace")[:300] or "pg_dump failed"
            logger.error("backup: pg_dump exited %s: %s", proc.returncode, msg)
            return False, msg
        size = os.path.getsize(dest_path) if os.path.exists(dest_path) else 0
        logger.info("backup: pg_dump done (%d bytes gzipped)", size)
        return True, "pg_dump.gz"
    except FileNotFoundError:
        logger.error("backup: pg_dump not installed")
        return False, "pg_dump not installed"
    except Exception as exc:  # noqa: BLE001
        logger.exception("backup: pg_dump failed")
        return False, str(exc)[:300]


def _copy_tree(src: str, dst: str) -> bool:
    try:
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
            return True
        if os.path.isfile(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            return True
    except Exception:  # noqa: BLE001
        logger.debug("backup: could not copy %s", src, exc_info=True)
    return False


def build_bundle(node_name: str, role: str, version: str) -> Tuple[bytes, List[str], str]:
    """Assemble the encrypted backup archive. Returns (encrypted_bytes,
    components, note). ``components`` = the parts that made it in."""
    settings = get_settings()
    components: List[str] = []
    notes: List[str] = []
    with tempfile.TemporaryDirectory(prefix="arkive-backup-", dir=_work_dir()) as tmp:
        staging = os.path.join(tmp, "bundle")
        os.makedirs(staging, exist_ok=True)

        # 1) Database (config + tenants + receipts + search index all live here).
        #    Gzipped so a large dump stays small on disk.
        db_ok, db_note = _dump_database(os.path.join(staging, "database.sql.gz"))
        if db_ok:
            components.append("database")
        else:
            logger.warning("backup: database NOT captured — %s", db_note)
        notes.append(f"db:{db_note}")

        # 2) Key store — wrapped vault/recovery keys + fleet signer + client regs.
        ks = os.environ.get("CV_KEY_STORE", "./cv_keystore")
        if _copy_tree(ks, os.path.join(staging, "keystore")):
            components.append("keystore")
        signer = os.environ.get("CV_FLEET_SIGNER", "./cv_fleet_signer.json")
        if _copy_tree(signer, os.path.join(staging, "config", os.path.basename(signer))):
            components.append("config")
        logger.info("backup: staged components: %s", ", ".join(components) or "none")

        # 3) Manifest.
        meta = {
            "node": node_name, "role": role, "version": version,
            "created_at": _now().isoformat() + "Z",
            "database_url_scheme": settings.database_url.split(":", 1)[0],
            "components": components, "notes": notes,
        }
        import json
        with open(os.path.join(staging, "meta.json"), "w") as f:
            json.dump(meta, f, indent=2)

        # 4) Tar the staging tree into memory (stored, not gzipped — the DB dump,
        #    the dominant member, is already gzipped).
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            tar.add(staging, arcname="arkive-backup")
        raw = buf.getvalue()

    logger.info("backup: archive assembled (%d bytes), encrypting…", len(raw))
    encrypted = credstore.encrypt_bytes(_ENC_SCOPE, raw)
    logger.info("backup: encrypted archive is %d bytes", len(encrypted))
    return encrypted, components, ";".join(notes)


# --------------------------------------------------------------------------- #
# Destination resolution + run                                               #
# --------------------------------------------------------------------------- #

def _service_config(db, svc) -> dict:
    """Merge a ServiceObject's linked ConfigObject credentials with its settings."""
    from .models import ConfigObject
    values: dict = {}
    if svc.config_object_id:
        obj = db.get(ConfigObject, svc.config_object_id)
        if obj and obj.encrypted_values:
            try:
                values = credstore.decrypt("platform", obj.encrypted_values)
            except Exception:  # noqa: BLE001
                values = {}
    return {**values, **(svc.settings or {})}


def _resolve_self_node(db):
    """The Node row for the running instance (by name+role, falling back to
    is_self) — carries this node's assigned backup_service_ids (replicated from
    the control plane in federated mode)."""
    from .models import Node
    s = get_settings()
    name = s.node_name or s.domain
    role = s.node_role or "control-plane"
    n = (db.query(Node).filter(Node.name == name, Node.role == role).first()
         or db.query(Node).filter(Node.is_self.is_(True)).first())
    return n


def reap_stale_runs(db, *, node_id=None, node_name=None, older_than_minutes: int = 0) -> int:
    """Mark backup runs stuck in ``running`` as failed — a run only stays running
    if the worker/process died mid-backup (restart, OOM, kill), which no in-thread
    handler can catch. Scoped to a node when given; ``older_than_minutes`` guards
    against reaping a genuinely in-flight run (0 = reap all for the node)."""
    from .models import BackupRun
    q = db.query(BackupRun).filter(BackupRun.status == "running")
    if node_id:
        q = q.filter(BackupRun.node_id == node_id)
    elif node_name:
        q = q.filter(BackupRun.node_name == node_name)
    if older_than_minutes > 0:
        cutoff = _now() - timedelta(minutes=older_than_minutes)
        q = q.filter(BackupRun.started_at < cutoff)
    reaped = 0
    for run in q.all():
        run.status = "failed"
        run.error = "interrupted — the backup process stopped before finishing"
        run.finished_at = _now()
        reaped += 1
    if reaped:
        db.commit()
        logger.warning("backup: reaped %d stale running run(s)", reaped)
    return reaped


_BACKUP_LOG_MAX = 400


def run_backup_once(db) -> Optional[dict]:
    """Run one infrastructure backup of this node to its assigned destinations.
    Records and returns a BackupRun view (dict). Returns a 'skipped' run when no
    backup destinations are configured. Captures a verbose per-run process log so
    the Backups page can show exactly what happened (and where it stalled)."""
    from .models import BackupRun
    s = get_settings()
    node = _resolve_self_node(db)
    node_id = node.id if node else None
    node_name = (node.name if node else (s.node_name or s.domain)) or "node"
    role = (node.role if node else s.node_role) or "control-plane"

    # A new run means any prior "running" run for this node is dead — but only
    # if it's genuinely STALE. Reaping a fresh, still-in-progress run (pg_dump can
    # take tens of minutes on a busy DB) is exactly what produced the spurious
    # "interrupted" failures, so only reap runs older than the pg_dump timeout.
    reap_stale_runs(db, node_id=node_id, node_name=node_name, older_than_minutes=40)
    # If a backup for this node is already in progress, don't start a second one:
    # a concurrent run would race and could mark the first "interrupted".
    running = (db.query(BackupRun)
               .filter(BackupRun.status == "running")
               .filter(BackupRun.node_id == node_id if node_id else BackupRun.node_name == node_name)
               .order_by(BackupRun.started_at.desc()).first())
    if running:
        logger.info("backup: a run is already in progress for %s — not starting another", node_name)
        return _run_view(running)

    run = BackupRun(node_id=node_id, node_name=node_name, role=role, kind="node",
                    status="running", started_at=_now(), components=[], destinations=[])
    db.add(run)
    db.commit()

    from .workers.jobs import capture_job_log
    with capture_job_log() as cap:
        try:
            _execute_backup(db, run, node, node_name, role)
        except Exception as exc:  # noqa: BLE001
            logger.exception("backup: run failed unexpectedly")
            run.status = "failed"
            run.error = (str(exc)[:500]) or "unexpected error"
        finally:
            # Never leave a run stuck "running" — every exit path must land a
            # terminal status + the captured log, or the dashboard shows a phantom.
            if run.status == "running":
                run.status = "failed"
                if not run.error:
                    run.error = "backup ended before reporting a final status"
            if not run.finished_at:
                run.finished_at = _now()
            run.log = (cap.records or [])[-_BACKUP_LOG_MAX:]
            db.commit()
    return _run_view(run)


def _execute_backup(db, run, node, node_name: str, role: str) -> None:
    """Do the actual backup work, recording results onto ``run``. Returns early
    (no value) on skip / fatal build error; run.status carries the outcome."""
    from .models import ServiceObject
    svc_ids = list((node.backup_service_ids or []) if node else [])
    # Capture everything we need from the ORM (incl. decrypted destination config)
    # WHILE the session is open, then release it — the pg_dump + upload below can
    # take minutes, and holding an open transaction the whole time leaves the
    # connection "idle in transaction", which blocks autovacuum (→ table bloat →
    # slow control-plane queries) and pins a pooled connection.
    svc_meta: list = []  # (id, name, kind, config)
    for sid in svc_ids:
        svc = db.get(ServiceObject, sid)
        if svc and svc.enabled and svc.kind.startswith("storage-") and "backup" in svc.storage_capabilities():
            svc_meta.append((svc.id, svc.name, svc.kind, _service_config(db, svc)))
    if not svc_meta:
        run.status = "skipped"
        run.message = "No backup destinations assigned"
        run.finished_at = _now()
        db.commit()
        logger.info("backup: %s has no backup destinations — skipped", node_name)
        return

    run_id = run.id  # capture before releasing the session (avoids a mid-backup reload)
    logger.info("backup: %d destination(s): %s", len(svc_meta),
                ", ".join(m[1] for m in svc_meta))
    db.commit()  # release the transaction/connection for the duration of the dump+upload

    version = ""
    try:
        with open("/etc/arkive/version") as fh:
            version = fh.read().strip()
    except Exception:
        pass

    logger.info("backup: building bundle for %s (%s)", node_name, role)
    try:
        encrypted, components, note = build_bundle(node_name, role, version)
    except Exception as exc:  # noqa: BLE001
        logger.exception("backup: bundle build failed")
        run.status = "failed"
        run.error = str(exc)[:500]
        run.finished_at = _now()
        db.commit()
        return

    # The database holds config, tenants, receipts and the search index — it is the
    # critical component, so a bundle without it must never look like a clean success.
    db_captured = "database" in components
    # A distinct object per run (timestamp + run id) so every backup is retained
    # as its own snapshot on the storage service rather than overwriting the last.
    ts = _now().strftime("%Y%m%dT%H%M%SZ")
    key = f"node-backups/{_safe(node_name)}/{ts}-{(run_id or '')[:8]}.arkbak"
    size = len(encrypted)

    results = []
    ok_count = 0
    for sid, name, kind, cfg in svc_meta:
        entry = {"service_id": sid, "name": name, "kind": kind,
                 "status": "pending", "bytes": 0, "key": key, "error": None}
        try:
            dest = destination_from_service(kind, cfg)
            if dest is None:
                entry.update(status="failed", error="missing required settings (bucket/container)")
                logger.warning("backup: %s has no usable destination config", name)
            else:
                logger.info("backup: uploading %d bytes to %s (%s)…", size, name, kind)
                dest.put_object(_BACKUP_PREFIX, key, encrypted, immutable=True)
                entry.update(status="ok", bytes=size)
                ok_count += 1
                logger.info("backup: %s uploaded %d bytes to %s", node_name, size, name)
        except Exception as exc:  # noqa: BLE001
            entry.update(status="failed", error=str(exc)[:300])
            logger.warning("backup: upload to %s failed: %s", name, exc)
        results.append(entry)

    run.components = components
    run.message = note
    run.destinations = results
    run.total_bytes = size if ok_count else 0
    if not db_captured:
        # Flag the missing database prominently so the operator can fix it (e.g.
        # install pg_dump / a matching client version) rather than trusting a
        # backup that can't actually restore the platform.
        run.error = (f"database NOT captured — {note}")[:500]
        logger.error("backup: %s produced NO database dump (%s)", node_name, note)
    if ok_count == 0:
        run.status = "failed"
    elif ok_count == len(svc_meta) and db_captured:
        run.status = "success"
    else:
        run.status = "partial"
    run.finished_at = _now()
    db.commit()
    logger.info("backup: %s complete — %s (%d/%d destinations, db=%s, %d bytes)",
                node_name, run.status, ok_count, len(svc_meta), db_captured, size)


def _run_view(run) -> dict:
    return {
        "id": run.id, "node_id": run.node_id, "node_name": run.node_name,
        "role": run.role, "kind": run.kind, "status": run.status,
        "components": run.components or [], "destinations": run.destinations or [],
        "total_bytes": run.total_bytes or 0, "message": run.message or "",
        "error": run.error or "", "log": run.log or [], "has_log": bool(run.log),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }
