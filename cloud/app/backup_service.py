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

import io
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
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


# --------------------------------------------------------------------------- #
# Bundle assembly                                                             #
# --------------------------------------------------------------------------- #

def _dump_database(dest_path: str) -> Tuple[bool, str]:
    """Dump the node's database to ``dest_path``. Postgres → pg_dump (plain SQL);
    SQLite → file copy. Returns (ok, note)."""
    url = get_settings().database_url
    if url.startswith("sqlite"):
        # sqlite:///relative or sqlite:////absolute
        path = url.split("://", 1)[1].lstrip("/")
        if url.startswith("sqlite:////"):
            path = "/" + path
        if not os.path.exists(path):
            return False, "sqlite file not found"
        shutil.copy2(path, dest_path)
        return True, "sqlite copy"
    # Postgres — pg_dump accepts the connection URI directly.
    pg_dump = shutil.which("pg_dump") or "pg_dump"
    try:
        with open(dest_path, "wb") as out:
            proc = subprocess.run(
                [pg_dump, "--no-owner", "--no-privileges", url],
                stdout=out, stderr=subprocess.PIPE, timeout=1800, check=False)
        if proc.returncode != 0:
            return False, (proc.stderr.decode(errors="replace")[:300] or "pg_dump failed")
        return True, "pg_dump"
    except FileNotFoundError:
        return False, "pg_dump not installed"
    except Exception as exc:  # noqa: BLE001
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
    with tempfile.TemporaryDirectory(prefix="arkive-backup-") as tmp:
        staging = os.path.join(tmp, "bundle")
        os.makedirs(staging, exist_ok=True)

        # 1) Database (config + tenants + receipts + search index all live here).
        db_ok, db_note = _dump_database(os.path.join(staging, "database.dump"))
        if db_ok:
            components.append("database")
        notes.append(f"db:{db_note}")

        # 2) Key store — wrapped vault/recovery keys + fleet signer + client regs.
        ks = os.environ.get("CV_KEY_STORE", "./cv_keystore")
        if _copy_tree(ks, os.path.join(staging, "keystore")):
            components.append("keystore")
        signer = os.environ.get("CV_FLEET_SIGNER", "./cv_fleet_signer.json")
        if _copy_tree(signer, os.path.join(staging, "config", os.path.basename(signer))):
            components.append("config")

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

        # 4) Tar+gzip the staging tree into memory.
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(staging, arcname="arkive-backup")
        raw = buf.getvalue()

    encrypted = credstore.encrypt_bytes(_ENC_SCOPE, raw)
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


def run_backup_once(db) -> Optional[dict]:
    """Run one infrastructure backup of this node to its assigned destinations.
    Records and returns a BackupRun view (dict). Returns a 'skipped' run when no
    backup destinations are configured."""
    from .models import BackupRun, ServiceObject
    s = get_settings()
    node = _resolve_self_node(db)
    node_id = node.id if node else None
    node_name = (node.name if node else (s.node_name or s.domain)) or "node"
    role = (node.role if node else s.node_role) or "control-plane"

    run = BackupRun(node_id=node_id, node_name=node_name, role=role, kind="node",
                    status="running", started_at=_now(), components=[], destinations=[])
    db.add(run)
    db.commit()

    svc_ids = list((node.backup_service_ids or []) if node else [])
    services = []
    for sid in svc_ids:
        svc = db.get(ServiceObject, sid)
        if svc and svc.enabled and svc.kind.startswith("storage-") and "backup" in svc.storage_capabilities():
            services.append(svc)
    if not services:
        run.status = "skipped"
        run.message = "No backup destinations assigned"
        run.finished_at = _now()
        db.commit()
        logger.info("backup: %s has no backup destinations — skipped", node_name)
        return _run_view(run)

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
        return _run_view(run)

    run.components = components
    run.message = note
    ts = _now().strftime("%Y%m%dT%H%M%SZ")
    key = f"node-backups/{_safe(node_name)}/{ts}.arkbak"
    size = len(encrypted)

    results = []
    ok_count = 0
    for svc in services:
        entry = {"service_id": svc.id, "name": svc.name, "kind": svc.kind,
                 "status": "pending", "bytes": 0, "key": key, "error": None}
        try:
            dest = destination_from_service(svc.kind, _service_config(db, svc))
            if dest is None:
                entry.update(status="failed", error="missing required settings (bucket/container)")
            else:
                dest.put_object(_BACKUP_PREFIX, key, encrypted, immutable=True)
                entry.update(status="ok", bytes=size)
                ok_count += 1
                logger.info("backup: %s uploaded %d bytes to %s", node_name, size, svc.name)
        except Exception as exc:  # noqa: BLE001
            entry.update(status="failed", error=str(exc)[:300])
            logger.warning("backup: upload to %s failed: %s", svc.name, exc)
        results.append(entry)

    run.destinations = results
    run.total_bytes = size if ok_count else 0
    run.status = "success" if ok_count == len(services) else ("partial" if ok_count else "failed")
    run.finished_at = _now()
    db.commit()
    logger.info("backup: %s complete — %s (%d/%d destinations, %d bytes)",
                node_name, run.status, ok_count, len(services), size)
    return _run_view(run)


def _run_view(run) -> dict:
    return {
        "id": run.id, "node_id": run.node_id, "node_name": run.node_name,
        "role": run.role, "kind": run.kind, "status": run.status,
        "components": run.components or [], "destinations": run.destinations or [],
        "total_bytes": run.total_bytes or 0, "message": run.message or "",
        "error": run.error or "",
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }
