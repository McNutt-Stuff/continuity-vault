"""Node-side replication loop (federated data-plane).

Runs on a customer-tenant node. It cannot reach the control plane's database, so
it talks to the control plane over HTTPS (authenticated by the fleet secret):

  * PULL — fetch the config for the tenants assigned to this node and upsert it
    into the LOCAL database (+ local key store), so the node's own scheduler and
    workers can run sync against local data; then
  * PUSH — send the recovery points and search-index rows this node produced
    back to the control plane so the portal stays authoritative.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .. import keybroker
from ..config import get_settings
from ..db import SessionLocal
from ..models import (
    Appliance,
    ApplianceStorage,
    Collection,
    ConfigObject,
    ConnectorAccount,
    DesktopAgent,
    Node,
    PricingConfig,
    SearchDocument,
    ServiceObject,
    SnapshotReceipt,
    SyncJob,
    Tenant,
    User,
    Vault,
)

logger = logging.getLogger("cv.replication")

_thread: threading.Thread | None = None
_running_jobs: set[str] = set()

# Upsert in FK-dependency order so a strict database accepts the rows.
_PULL_ORDER = [
    ("config_objects", ConfigObject),
    ("service_objects", ServiceObject),
    ("nodes", Node),
    ("tenants", Tenant),
    ("users", User),
    ("vaults", Vault),
    ("desktop_agents", DesktopAgent),
    ("appliances", Appliance),
    ("appliance_storages", ApplianceStorage),
    ("connector_accounts", ConnectorAccount),
    ("collections", Collection),
]

# Fields owned by the NODE, never overwritten by a pull (agent command queue +
# filesystem scan state live on the node the device reports to).
_PULL_EXCLUDE = {
    "desktop_agents": {"pending_commands", "last_scan", "fs_expansions"},
}


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _state_path() -> Path:
    base = Path(os.environ.get("CV_KEY_STORE", "./cv_keystore")).parent
    return base / "replication_state.json"


def _load_cursor() -> str | None:
    try:
        return json.loads(_state_path().read_text()).get("push_cursor")
    except Exception:
        return None


def _save_cursor(iso: str) -> None:
    try:
        _state_path().write_text(json.dumps({"push_cursor": iso}))
    except Exception:
        logger.debug("could not persist replication cursor", exc_info=True)


def _post(path: str, payload: dict) -> dict | None:
    s = get_settings()
    if not s.control_plane_url or not s.node_secret:
        return None
    url = s.control_plane_url.rstrip("/") + "/api" + path
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {s.node_secret}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        logger.warning("replication %s rejected: %s", path, e.code)
    except Exception as e:  # noqa: BLE001
        logger.warning("replication %s failed to reach control plane: %s", path, e)
    return None


def _upsert(db, model, data: dict) -> None:
    from sqlalchemy import DateTime
    cols = {c.name: c for c in model.__table__.columns}
    kw = {}
    for k, v in data.items():
        col = cols.get(k)
        if col is None:
            continue
        if isinstance(col.type, DateTime) and isinstance(v, str):
            try:
                dt = datetime.fromisoformat(v)
                v = dt.replace(tzinfo=None) if dt.tzinfo else dt
            except ValueError:
                v = None
        kw[k] = v
    pk = list(model.__table__.primary_key.columns)[0].name
    obj = db.get(model, kw.get(pk))
    if obj is not None:
        for k, v in kw.items():
            if k != pk:
                setattr(obj, k, v)
    else:
        db.add(model(**kw))


def _pull(s) -> int:
    bundle = _post("/nodes/sync/pull",
                   {"name": s.node_name or s.domain, "role": s.node_role or "customer-tenant"})
    if not bundle:
        return 0
    n = 0
    with SessionLocal() as db:
        # Pricing is a singleton row.
        if bundle.get("pricing"):
            _upsert(db, PricingConfig, bundle["pricing"])
        for key, model in _PULL_ORDER:
            exclude = _PULL_EXCLUDE.get(key)
            for row in bundle.get(key, []) or []:
                if exclude:
                    row = {k: v for k, v in row.items() if k not in exclude}
                _upsert(db, model, row)
                n += 1
        db.commit()
    # Wrapped key records → local key store (fleet-shared KEK makes them usable).
    for vid, recs in (bundle.get("key_records") or {}).items():
        try:
            keybroker.import_key_records(vid, recs)
        except Exception:  # noqa: BLE001
            logger.debug("could not import key records for vault %s", vid, exc_info=True)
    # Portal-initiated "Back up now" jobs the control plane assigned to us: mirror
    # them locally and run them against the local DB.
    for j in bundle.get("pending_jobs", []) or []:
        _run_pending_job(j)
    logger.info("replication pull: %d assigned tenant(s), %d row(s) synced",
                bundle.get("assigned", 0), n)
    return n


def _run_pending_job(j: dict) -> None:
    jid = j.get("id")
    if not jid or jid in _running_jobs:
        return
    with SessionLocal() as db:
        local = db.get(SyncJob, jid)
        if local is None:
            _upsert(db, SyncJob, j)
            db.commit()
        elif local.status != "queued":
            return  # already run/running here; the next push syncs the CP
    _running_jobs.add(jid)

    def _go() -> None:
        try:
            from .jobs import _run
            _run(jid, None)
        finally:
            _running_jobs.discard(jid)
            try:
                _push(get_settings())  # flush job status + receipts promptly
            except Exception:  # noqa: BLE001
                logger.debug("post-job push failed", exc_info=True)

    threading.Thread(target=_go, name=f"cv-node-job-{jid[:8]}", daemon=True).start()


def _push(s) -> int:
    cursor = _load_cursor()
    since = None
    if cursor:
        try:
            since = datetime.fromisoformat(cursor)
        except ValueError:
            since = None
    high = since
    receipts, documents, accounts = [], [], []
    jobs, agents, appliances = [], [], []
    with SessionLocal() as db:
        rq = db.query(SnapshotReceipt)
        if since is not None:
            rq = rq.filter(SnapshotReceipt.created_at > since)
        for r in rq.order_by(SnapshotReceipt.created_at.asc()).limit(2000).all():
            receipts.append(_row(r))
            if r.created_at and (high is None or r.created_at > high):
                high = r.created_at
        dq = db.query(SearchDocument)
        if since is not None:
            dq = dq.filter(SearchDocument.created_at > since)
        for d in dq.order_by(SearchDocument.created_at.asc()).limit(5000).all():
            documents.append(_row(d))
            if d.created_at and (high is None or d.created_at > high):
                high = d.created_at
        for a in db.query(ConnectorAccount).all():
            accounts.append(_row(a))
        # Job progress (so the portal's Activity reflects node-run backups) and
        # device liveness (so the portal shows agents/appliances that now report
        # to this node).
        for j in db.query(SyncJob).filter(SyncJob.status != "queued").order_by(
                SyncJob.created_at.desc()).limit(200).all():
            jobs.append(_row(j))
        for ag in db.query(DesktopAgent).all():
            agents.append(_row(ag))
        for ap in db.query(Appliance).all():
            appliances.append(_row(ap))
    if not (receipts or documents or accounts or jobs or agents or appliances):
        return 0
    res = _post("/nodes/sync/push", {
        "node": s.node_name or s.domain, "role": s.node_role or "customer-tenant",
        "receipts": receipts, "documents": documents, "connector_accounts": accounts,
        "jobs": jobs, "agents": agents, "appliances": appliances,
    })
    if res and res.get("ok"):
        if high is not None:
            _save_cursor(high.isoformat())
        logger.info("replication push: receipts=%d documents=%d jobs=%d agents=%d appliances=%d",
                    len(receipts), len(documents), len(jobs), len(agents), len(appliances))
        return len(receipts) + len(documents)
    return 0


def _row(obj) -> dict:
    out = {}
    for c in obj.__table__.columns:
        v = getattr(obj, c.name)
        out[c.name] = v.isoformat() if isinstance(v, datetime) else v
    return out


def start_replication() -> None:
    """Start the node replication loop (customer-tenant nodes only)."""
    global _thread
    s = get_settings()
    if _thread is not None:
        return
    if not s.control_plane_url or not s.node_secret:
        logger.warning("replication disabled: control_plane_url / node_secret not set")
        return
    interval = max(30, s.sync_interval_minutes * 60 // 2 or 120)

    def loop() -> None:
        time.sleep(10)
        while True:
            try:
                _pull(s)
            except Exception:  # noqa: BLE001
                logger.exception("replication pull cycle failed")
            try:
                _push(s)
            except Exception:  # noqa: BLE001
                logger.exception("replication push cycle failed")
            time.sleep(interval)

    _thread = threading.Thread(target=loop, name="cv-node-replication", daemon=True)
    _thread.start()
    logger.info("node replication started (control plane=%s, every %ds)",
                s.control_plane_url, interval)
