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

import logging
import secrets
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
    Communication,
    ConfigObject,
    ConnectorAccount,
    CustomerStorage,
    DesktopAgent,
    IntegrationConfig,
    IntegrationInstance,
    IntegrationRun,
    NetworkApp,
    NetworkClient,
    NetworkUsage,
    Node,
    PricingConfig,
    SearchDocument,
    ServiceObject,
    SnapshotReceipt,
    SourceConfig,
    SyncJob,
    Tenant,
    User,
    UserInsights,
    Vault,
)
from .site import _fleet_secret

router = APIRouter(prefix="/nodes/sync", tags=["node-sync"])
logger = logging.getLogger("cv.node-sync")


def _require_fleet(authorization: str) -> None:
    token = ""
    raw = (authorization or "").strip()
    if raw.lower().startswith("bearer "):
        token = raw.split(" ", 1)[1].strip()
    expected = _fleet_secret() or ""
    if not token or not expected or not secrets.compare_digest(token, expected):
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
                logger.warning("node-sync: invalid datetime for %s.%s value=%r",
                               getattr(model, "__tablename__", model), k, v)
                v = None
        kw[k] = v
    return kw


def _has_pk(model, row: dict) -> bool:
    pk = list(model.__table__.primary_key.columns)[0].name
    return bool(row.get(pk))


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

    # Users whose digital-footprint insights an admin (or the user) asked to
    # (re)generate — the node mines its local index and pushes the report back.
    uids = [u.id for u in users]
    pending_insights = ([uid for (uid,) in
                         db.query(UserInsights.user_id)
                         .filter(UserInsights.user_id.in_(uids),
                                 UserInsights.status == "pending").all()]
                        if uids else [])

    # Wrapped key material for each vault (fleet-shared KEK → usable on the node).
    key_records = {v.id: keybroker.export_key_records(v.id) for v in vaults}

    pricing = db.get(PricingConfig, "default")
    # Integrations for the node's tenants + platform enable/disable, so the node
    # can serve its appliances' integration pull/report locally.
    integration_instances = (db.query(IntegrationInstance)
                              .filter(IntegrationInstance.tenant_id.in_(tids)).all())
    customer_storages = (db.query(CustomerStorage)
                         .filter(CustomerStorage.tenant_id.in_(tids)).all()) if tids else []
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
        "source_configs": [_ser(sc) for sc in db.query(SourceConfig).all()],
        "integration_configs": [_ser(ic) for ic in db.query(IntegrationConfig).all()],
        "integration_instances": [_ser(i) for i in integration_instances],
        "customer_storages": [_ser(s) for s in customer_storages],
        "nodes": [_ser(n) for n in db.query(Node).all()],
        "pricing": _ser(pricing) if pricing else None,
        "pending_jobs": [_ser(j) for j in pending_jobs],
        "pending_insights": pending_insights,
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
    appliance_storages: list[dict] = []
    insights: list[dict] = []
    integration_instances: list[dict] = []
    network_clients: list[dict] = []
    network_apps: list[dict] = []
    network_usage: list[dict] = []
    integration_runs: list[dict] = []
    communications: list[dict] = []
    index_replicas: list[dict] = []


_JOB_FIELDS = ("status", "processed", "total", "message", "error", "snapshot_id",
               "log", "started_at", "finished_at")
_AGENT_FIELDS = ("state", "version", "telemetry", "last_heartbeat_at", "collectors")
_APPLIANCE_FIELDS = ("state", "isolation_state", "software_version", "telemetry",
                     "tamper_state", "attestation_ok", "last_heartbeat_at",
                     "last_attestation_at", "model", "version_updated_at")


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
              "jobs": 0, "agents": 0, "appliances": 0, "appliance_storages": 0,
              "insights": 0,
              "integrations": 0, "network": 0, "communications": 0,
              "index_replicas": 0}
    # A node can hold data for a tenant/user that was removed on the control
    # plane; inserting it would violate a FK and abort the whole push. Skip any
    # row whose tenant or owner isn't present here so one orphan can't block sync.
    valid_tenants = {t for (t,) in db.query(Tenant.id).all()}
    valid_users = {u for (u,) in db.query(User.id).all()}

    def _known(row: dict) -> bool:
        tid = row.get("tenant_id")
        return tid is None or tid in valid_tenants

    for r in body.receipts:
        if not _known(r) or not _has_pk(SnapshotReceipt, r):
            continue
        _upsert(db, SnapshotReceipt, r)
        counts["receipts"] += 1
    for d in body.documents:
        if not _known(d) or not _has_pk(SearchDocument, d):
            continue
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
    # Progress of jobs the node ran. Portal-initiated "Back up now" jobs already
    # exist here (created on the CP, dispatched to the node); node-originated jobs
    # (scheduled backups, created only in the node's DB) don't — create those so
    # the admin worker view reflects scheduled runs too, attributed to the node.
    push_node = (db.query(Node)
                 .filter(Node.name == body.node, Node.role == body.role).first()
                 or db.query(Node).filter(Node.name == body.node).first())
    for j in body.jobs:
        if not _known(j) or not _has_pk(SyncJob, j):
            continue
        job = db.get(SyncJob, j.get("id"))
        if job:
            _apply(job, j, _JOB_FIELDS)
        else:
            _upsert(db, SyncJob, j)
            job = db.get(SyncJob, j.get("id"))
        # Attribute any node-pushed job to the pushing node (fixes rows created
        # with a null node_id before this, since node_id isn't in _JOB_FIELDS).
        if job is not None and push_node is not None and not job.node_id:
            job.node_id = push_node.id
        counts["jobs"] += 1
    for a in body.agents:
        if not _has_pk(DesktopAgent, a):
            continue
        ag = db.get(DesktopAgent, a.get("id"))
        if ag:
            _apply(ag, a, _AGENT_FIELDS)
            counts["agents"] += 1
    for a in body.appliances:
        if not _has_pk(Appliance, a):
            continue
        ap = db.get(Appliance, a.get("id"))
        if ap:
            _apply(ap, a, _APPLIANCE_FIELDS)
            counts["appliances"] += 1
    # Per-volume storage the node's appliances reported (built-in + dedicated
    # RAID/SMART/capacity). A node-routed appliance's storages are created on the
    # node, so upsert (create-or-update) rather than update-only — but only for an
    # appliance that exists here, to avoid an appliance_id FK violation.
    valid_appliances = {i for (i,) in db.query(Appliance.id).all()}
    for st in body.appliance_storages:
        if (not _known(st) or not _has_pk(ApplianceStorage, st)
                or st.get("appliance_id") not in valid_appliances):
            continue
        _upsert(db, ApplianceStorage, st)
        counts["appliance_storages"] += 1
    # Digital-footprint insights the node computed for its tenants. Key on user_id
    # (not the row id, which differs between the node and any control-plane
    # pending marker) so there's exactly one report per user. Skip a row whose
    # tenant or user no longer exists here.
    for ins in body.insights:
        uid = ins.get("user_id")
        if not _known(ins) or not uid or uid not in valid_users:
            continue
        kw = _deser(UserInsights, ins)
        existing = db.query(UserInsights).filter(UserInsights.user_id == uid).first()
        if existing is not None:
            for k, v in kw.items():
                if k not in ("id", "user_id"):
                    setattr(existing, k, v)
        else:
            db.add(UserInsights(**kw))
        counts["insights"] += 1
    _ingest_integration_push(db, body, counts, valid_tenants, valid_users)
    # Search-index replica health (DR copies of the index the node produced). Key
    # on (scope, scope_id, destination) since the row id differs per DB.
    from ..models import IndexReplica
    for ir in body.index_replicas:
        if not _known(ir):
            continue
        kw = _deser(IndexReplica, ir)
        existing = (db.query(IndexReplica)
                    .filter(IndexReplica.scope == ir.get("scope"),
                            IndexReplica.scope_id == ir.get("scope_id"),
                            IndexReplica.destination == ir.get("destination")).first())
        if existing is not None:
            for k, v in kw.items():
                if k != "id":
                    setattr(existing, k, v)
        else:
            db.add(IndexReplica(**kw))
        counts["index_replicas"] += 1
    # Communications history from the node's email service. The control plane owns
    # the open fields (the tracking pixel always hits the CP), so never overwrite
    # them from a node push — which also preserves a stub created by an early open.
    for cm in body.communications:
        if not _known(cm) or not _has_pk(Communication, cm):
            continue
        kw = _deser(Communication, cm)
        existing = db.get(Communication, kw.get("id"))
        if existing is not None:
            for k, v in kw.items():
                if k in ("id", "opened_at", "open_count", "last_opened_ip"):
                    continue
                setattr(existing, k, v)
        else:
            for f in ("opened_at", "open_count", "last_opened_ip"):
                kw.pop(f, None)
            db.add(Communication(**kw))
        counts["communications"] += 1
    db.commit()
    return {"ok": True, **counts}


# Telemetry fields on network rows; monitor_state / of_interest are CP-curated.
_CLIENT_TELEMETRY = ("name", "hostname", "ip", "mac", "is_wired", "is_guest",
                     "device_type", "tx_bytes", "rx_bytes", "total_bytes",
                     "first_seen", "last_seen")
_APP_TELEMETRY = ("name", "category", "source_type", "tx_bytes", "rx_bytes",
                  "total_bytes", "sessions", "client_count", "first_seen", "last_seen")


def _ingest_integration_push(db: Session, body: "PushPayload", counts: dict,
                             valid_tenants: set, valid_users: set) -> None:
    """Fold node-reported integration telemetry into the control-plane DB without
    clobbering the user's monitor/interest curation. Instances are node-authoritative
    (portal calls are proxied there), so they upsert in full for the CP mirror.
    Rows for a tenant/owner no longer present here are skipped (would violate FKs)."""
    def _ok(row: dict) -> bool:
        tid = row.get("tenant_id")
        return tid is None or tid in valid_tenants

    for row in body.integration_instances:
        owner = row.get("owner_user_id")
        if not _ok(row) or (owner and owner not in valid_users):
            continue
        _upsert(db, IntegrationInstance, row)
        counts["integrations"] += 1
    for row in body.network_clients:
        if not _ok(row):
            continue
        kw = _deser(NetworkClient, row)
        cur = (db.query(NetworkClient)
               .filter(NetworkClient.tenant_id == kw.get("tenant_id"),
                       NetworkClient.integration_id == kw.get("integration_id"),
                       NetworkClient.client_key == kw.get("client_key")).first())
        if cur is None:
            db.add(NetworkClient(**{k: v for k, v in kw.items() if k != "id"}))
        else:
            for f in _CLIENT_TELEMETRY:
                if f in kw:
                    setattr(cur, f, kw[f])
        counts["network"] += 1
    for row in body.network_apps:
        if not _ok(row):
            continue
        kw = _deser(NetworkApp, row)
        cur = (db.query(NetworkApp)
               .filter(NetworkApp.tenant_id == kw.get("tenant_id"),
                       NetworkApp.integration_id == kw.get("integration_id"),
                       NetworkApp.app_key == kw.get("app_key")).first())
        if cur is None:
            db.add(NetworkApp(**{k: v for k, v in kw.items() if k != "id"}))
        else:
            for f in _APP_TELEMETRY:
                if f in kw:
                    setattr(cur, f, kw[f])
        counts["network"] += 1
    for row in body.network_usage:
        if not _ok(row):
            continue
        kw = _deser(NetworkUsage, row)
        cur = (db.query(NetworkUsage)
               .filter(NetworkUsage.tenant_id == kw.get("tenant_id"),
                       NetworkUsage.integration_id == kw.get("integration_id"),
                       NetworkUsage.client_key == kw.get("client_key"),
                       NetworkUsage.app_key == kw.get("app_key")).first())
        if cur is None:
            db.add(NetworkUsage(**{k: v for k, v in kw.items() if k != "id"}))
        else:
            for f in ("tx_bytes", "rx_bytes", "total_bytes", "sessions", "last_seen"):
                if f in kw:
                    setattr(cur, f, kw[f])
        counts["network"] += 1
    for row in body.integration_runs:
        if not _ok(row):
            continue
        if not db.get(IntegrationRun, row.get("id")):
            db.add(IntegrationRun(**_deser(IntegrationRun, row)))
            counts["network"] += 1


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
    from ..workers import status as worker_status
    s = get_settings()
    out = sysinfo.live(cert_host=s.domain)
    out["db"] = _db_stats(db)
    out["workers"] = worker_status.snapshot()
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


@router.get("/debug")
def node_debug(authorization: str = Header(default=""), db: Session = Depends(get_db)):
    """Fleet-authenticated DB health for this node (used by the control plane's
    debug API to diagnose which node's database is slow)."""
    _require_fleet(authorization)
    from .debug import _brief_stats
    return _brief_stats(db)


@router.get("/config")
def node_config_state(authorization: str = Header(default=""), db: Session = Depends(get_db)):
    """The settings this node currently has in effect (config profiles applied via
    its last heartbeat), so the control plane can show applied-vs-assigned drift."""
    _require_fleet(authorization)
    from .. import node_config
    return {"settings": node_config.effective(db)}


@router.get("/queue")
def node_queue(authorization: str = Header(default=""), db: Session = Depends(get_db)):
    """This node's durable activity queue (backups pending delivery to an offline
    appliance / unreachable storage), read by the control-plane admin."""
    _require_fleet(authorization)
    from .. import queue_registry
    return queue_registry.list_items(db)


class QueueActionReq(BaseModel):
    id: str
    action: str


@router.post("/queue/action")
def node_queue_action(body: QueueActionReq, authorization: str = Header(default=""),
                      db: Session = Depends(get_db)):
    _require_fleet(authorization)
    from .. import queue_registry
    if body.action == "retry":
        queue_registry.retry(db, body.id)
    elif body.action == "cancel":
        queue_registry.cancel(db, body.id)
    return {"ok": True}


class ControlReq(BaseModel):
    action: str
    unit: str = ""


@router.post("/control")
def node_control(body: ControlReq, authorization: str = Header(default="")):
    _require_fleet(authorization)
    from .. import sysinfo
    s = get_settings()
    return sysinfo.control(body.action, body.unit, s.node_role or "control-plane")


@router.post("/backup")
def node_backup(authorization: str = Header(default="")):
    """Run this node's infrastructure backup in-process now (no systemd/sudo, so
    it works in containers) and report the result to the control plane. Called by
    the control plane's per-node backup button."""
    _require_fleet(authorization)
    import threading
    from .. import backup_worker

    def _go():
        try:
            backup_worker.run_once()  # backs up AND reports centrally (success or failure)
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger("cv.node").exception("remote node backup failed")

    threading.Thread(target=_go, name="cv-backup-manual", daemon=True).start()
    return {"ok": True, "message": "Backup started"}


class PurgeReq(BaseModel):
    account_id: str
    tenant_id: str
    destinations: list[str] | None = None


@router.post("/purge")
def node_purge(body: PurgeReq, authorization: str = Header(default=""),
               db: Session = Depends(get_db)):
    """Purge a source's local data on this node (index + recovery points +
    mappings + account), called by the control plane during a source purge.
    ``destinations`` limits the purge to specific stores; None/["all"] = everywhere."""
    _require_fleet(authorization)
    acct = db.get(ConnectorAccount, body.account_id)
    if not acct or acct.tenant_id != body.tenant_id:
        return {"ok": True, "documents": 0, "recovery_points": 0, "collections": 0}
    from .connectors import _purge_destinations, _purge_source_local
    dests = body.destinations
    if not dests or "all" in dests:
        counts = _purge_source_local(db, acct)
    else:
        counts = _purge_destinations(db, acct, dests)
    db.commit()
    return {"ok": True, **counts}

