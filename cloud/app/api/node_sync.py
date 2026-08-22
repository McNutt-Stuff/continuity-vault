"""Federated node replication (data-plane isolation).

A customer-tenant node keeps its OWN local database + search index for the
tenants assigned to it, and cannot reach the control plane's database directly.
Instead it authenticates with the shared fleet secret and:

  * PULLS the config for its assigned tenants (tenants, users, vaults + wrapped
    keys, mappings, connector accounts + encrypted creds, storage/email service
    objects, pricing) into its local DB, then runs sync locally; and
  * PUSHES the results it produces (recovery points + search index + connector
    status) back so the control plane's platform DB stays authoritative for the
    portal (search, recovery, billing, activity).

Key material and connector credentials are wrapped with the fleet-wide
``CV_KEK_SECRET`` (see keybroker / credstore), so the node can use them directly
— the whole fleet MUST share that secret for federation to work.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import DateTime
from sqlalchemy.orm import Session

from .. import keybroker
from ..config import get_settings
from ..db import get_db
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
from .site import _fleet_secret

router = APIRouter(prefix="/nodes/sync", tags=["node-sync"])


def _require_fleet(authorization: str) -> None:
    token = (authorization or "").replace("Bearer ", "").strip()
    if not token or token != _fleet_secret():
        raise HTTPException(401, "invalid node credentials")


def _ser(obj) -> dict:
    """Serialize a SQLAlchemy row to a JSON-safe dict (datetimes → ISO)."""
    out = {}
    for c in obj.__table__.columns:
        v = getattr(obj, c.name)
        out[c.name] = v.isoformat() if isinstance(v, datetime) else v
    return out


def _deser(model, data: dict) -> dict:
    """Coerce an inbound dict back to column values (ISO strings → datetimes)."""
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
    return kw


def _upsert(db: Session, model, data: dict):
    kw = _deser(model, data)
    pk = list(model.__table__.primary_key.columns)[0].name
    obj = db.get(model, kw.get(pk))
    if obj is not None:
        for k, v in kw.items():
            if k != pk:
                setattr(obj, k, v)
    else:
        obj = model(**kw)
        db.add(obj)
    return obj


class NodeIdent(BaseModel):
    name: str
    role: str = "customer-tenant"
    since: str | None = None  # ISO cursor for incremental pull (unused in v1)


@router.post("/pull")
def pull(body: NodeIdent, authorization: str = Header(default=""),
         db: Session = Depends(get_db)):
    """Return the full config bundle for the tenants assigned to this node."""
    _require_fleet(authorization)
    node = (db.query(Node)
            .filter(Node.name == body.name, Node.role == body.role).first())
    if node is None:
        # Not registered yet (heartbeat runs on its own cadence) — nothing to do.
        return {"node_id": None, "tenants": [], "assigned": 0}
    tenants = db.query(Tenant).filter(Tenant.node_id == node.id).all()
    tids = [t.id for t in tenants]
    if not tids:
        return {"node_id": node.id, "tenants": [], "assigned": 0}

    users = db.query(User).filter(User.tenant_id.in_(tids)).all()
    vaults = db.query(Vault).filter(Vault.tenant_id.in_(tids)).all()
    collections = db.query(Collection).filter(Collection.tenant_id.in_(tids)).all()
    accounts = db.query(ConnectorAccount).filter(ConnectorAccount.tenant_id.in_(tids)).all()
    agents = db.query(DesktopAgent).filter(DesktopAgent.tenant_id.in_(tids)).all()
    # Forward-queue: hand the node any commands the portal queued for these
    # agents (e.g. "Sync now" → collect), draining them here so they aren't sent
    # again. The node appends them to its local agent queue and delivers them to
    # the agent, which now reports to the node. (pending_commands is excluded from
    # the row upsert on the node, so this is the only delivery path — no loop.)
    agent_commands: dict = {}
    for a in agents:
        q = []
        if a.pending_command:
            q.append(a.pending_command)
        q.extend(a.pending_commands or [])
        if q:
            agent_commands[a.id] = q
            a.pending_command = None
            a.pending_commands = []
    if agent_commands:
        db.commit()
    appliances = db.query(Appliance).filter(Appliance.tenant_id.in_(tids)).all()
    aids = [a.id for a in appliances]
    storages = (db.query(ApplianceStorage).filter(ApplianceStorage.appliance_id.in_(aids)).all()
                if aids else [])
    # Portal-initiated "Back up now" jobs waiting for this node to run them.
    pending_jobs = (db.query(SyncJob)
                    .filter(SyncJob.node_id == node.id, SyncJob.status == "queued").all())

    # Wrapped key material for each vault (fleet-shared KEK → usable on the node).
    key_records = {v.id: keybroker.export_key_records(v.id) for v in vaults}

    pricing = db.get(PricingConfig, "default")
    return {
        "node_id": node.id,
        "assigned": len(tids),
        "tenants": [_ser(t) for t in tenants],
        "users": [_ser(u) for u in users],
        "vaults": [_ser(v) for v in vaults],
        "desktop_agents": [_ser(a) for a in agents],
        "appliances": [_ser(a) for a in appliances],
        "appliance_storages": [_ser(s) for s in storages],
        "collections": [_ser(c) for c in collections],
        "connector_accounts": [_ser(a) for a in accounts],
        "service_objects": [_ser(s) for s in db.query(ServiceObject).all()],
        "config_objects": [_ser(c) for c in db.query(ConfigObject).all()],
        "nodes": [_ser(n) for n in db.query(Node).all()],
        "pricing": _ser(pricing) if pricing else None,
        "pending_jobs": [_ser(j) for j in pending_jobs],
        "agent_commands": agent_commands,
        "key_records": key_records,
    }


class PushPayload(BaseModel):
    node: str
    role: str = "customer-tenant"
    receipts: list[dict] = []
    documents: list[dict] = []
    connector_accounts: list[dict] = []
    jobs: list[dict] = []
    agents: list[dict] = []
    appliances: list[dict] = []


_JOB_FIELDS = ("status", "processed", "total", "message", "error", "snapshot_id",
               "started_at", "finished_at")
_AGENT_FIELDS = ("state", "version", "telemetry", "last_heartbeat_at", "collectors")
_APPLIANCE_FIELDS = ("state", "isolation_state", "software_version", "telemetry",
                     "tamper_state", "attestation_ok", "last_heartbeat_at",
                     "last_attestation_at")


def _apply(obj, data: dict, fields: tuple) -> None:
    for f in fields:
        if f not in data:
            continue
        v = data[f]
        if f.endswith("_at") and isinstance(v, str):
            try:
                dt = datetime.fromisoformat(v)
                v = dt.replace(tzinfo=None) if dt.tzinfo else dt
            except ValueError:
                v = None
        setattr(obj, f, v)


@router.post("/push")
def push(body: PushPayload, authorization: str = Header(default=""),
         db: Session = Depends(get_db)):
    """Ingest the results a node produced so the control-plane platform DB stays
    authoritative for the portal (search / recovery / billing / activity)."""
    _require_fleet(authorization)
    counts = {"receipts": 0, "documents": 0, "connector_accounts": 0,
              "jobs": 0, "agents": 0, "appliances": 0}
    for r in body.receipts:
        _upsert(db, SnapshotReceipt, r)
        counts["receipts"] += 1
    for d in body.documents:
        _upsert(db, SearchDocument, d)
        counts["documents"] += 1
    # Only status/cursor fields for accounts — never overwrite the encrypted
    # credentials the control plane owns.
    for a in body.connector_accounts:
        acct = db.get(ConnectorAccount, a.get("id"))
        if not acct:
            continue
        for f in ("last_sync_at", "sync_cursor", "last_object_count",
                  "last_error", "last_error_at", "auth_status"):
            if f in a:
                val = a[f]
                if f.endswith("_at") and isinstance(val, str):
                    try:
                        dt = datetime.fromisoformat(val)
                        val = dt.replace(tzinfo=None) if dt.tzinfo else dt
                    except ValueError:
                        val = None
                setattr(acct, f, val)
        counts["connector_accounts"] += 1
    # Progress of portal-initiated jobs the node ran, plus device liveness.
    for j in body.jobs:
        job = db.get(SyncJob, j.get("id"))
        if job:
            _apply(job, j, _JOB_FIELDS)
            counts["jobs"] += 1
    for a in body.agents:
        ag = db.get(DesktopAgent, a.get("id"))
        if ag:
            _apply(ag, a, _AGENT_FIELDS)
            counts["agents"] += 1
    for a in body.appliances:
        ap = db.get(Appliance, a.get("id"))
        if ap:
            _apply(ap, a, _APPLIANCE_FIELDS)
            counts["appliances"] += 1
    db.commit()
    return {"ok": True, **counts}


# --------------------------------------------------------------------------- #
# Node telemetry — live metrics, logs, controls, key/cert health.             #
# The control plane calls these on remote nodes (fleet-authed) and runs the    #
# same logic locally for its own (self) node.                                  #
# --------------------------------------------------------------------------- #

def _db_stats(db: Session) -> dict:
    from sqlalchemy import text
    out: dict = {}
    try:
        out["size_bytes"] = int(db.execute(
            text("SELECT pg_database_size(current_database())")).scalar() or 0)
        out["connections"] = int(db.execute(
            text("SELECT count(*) FROM pg_stat_activity")).scalar() or 0)
    except Exception:
        pass  # sqlite dev / permission — best-effort
    return out


def keys_report(db: Session) -> dict:
    """Key + crypto integrity for the tenants whose data lives on this node."""
    from .. import fleet
    from ..models import Vault
    from cv_crypto.provider import get_provider
    vaults = db.query(Vault).all()
    provisioned = 0
    for v in vaults:
        try:
            if keybroker.key_metadata(v.id).get("provisioned"):
                provisioned += 1
        except Exception:
            pass
    try:
        signer_id = fleet.cloud_public_bundle().get("keyId")
    except Exception:
        signer_id = None
    prov = get_provider()
    return {
        "vault_keys": {"total": len(vaults), "provisioned": provisioned},
        "pq_hybrid": bool(getattr(prov, "pq_available", False)),
        "signer_key_id": signer_id,
    }


@router.get("/live")
def node_live(authorization: str = Header(default=""), db: Session = Depends(get_db)):
    _require_fleet(authorization)
    from .. import sysinfo
    s = get_settings()
    out = sysinfo.live(cert_host=s.domain)
    out["db"] = _db_stats(db)
    return out


@router.get("/logs")
def node_logs(source: str = "app", lines: int = 200,
              authorization: str = Header(default="")):
    _require_fleet(authorization)
    from .. import sysinfo
    return {"source": source, "lines": sysinfo.logs(source, lines)}


@router.get("/keys")
def node_keys(authorization: str = Header(default=""), db: Session = Depends(get_db)):
    _require_fleet(authorization)
    from .. import sysinfo
    s = get_settings()
    out = keys_report(db)
    out["certificate"] = sysinfo.cert_info(s.domain)
    return out


class ControlReq(BaseModel):
    action: str
    unit: str = ""


@router.post("/control")
def node_control(body: ControlReq, authorization: str = Header(default="")):
    _require_fleet(authorization)
    from .. import sysinfo
    s = get_settings()
    return sysinfo.control(body.action, body.unit, s.node_role or "control-plane")


class PurgeReq(BaseModel):
    account_id: str
    tenant_id: str


@router.post("/purge")
def node_purge(body: PurgeReq, authorization: str = Header(default=""),
               db: Session = Depends(get_db)):
    """Purge a source's local data on this node (index + recovery points +
    mappings + account), called by the control plane during a source purge."""
    _require_fleet(authorization)
    acct = db.get(ConnectorAccount, body.account_id)
    if not acct or acct.tenant_id != body.tenant_id:
        return {"ok": True, "documents": 0, "recovery_points": 0, "collections": 0}
    from .connectors import _purge_source_local
    counts = _purge_source_local(db, acct)
    db.commit()
    return {"ok": True, **counts}

