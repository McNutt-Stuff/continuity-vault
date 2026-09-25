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


class EmailRelay(BaseModel):
    to: str
    subject: str
    html: str
    text: str = ""
    category: str = "email"


@router.post("/fleet-secrets")
def fleet_secrets(authorization: str = Header(default="")):
    """Hand the fleet-shared CV_KEK_SECRET + CV_SESSION_SECRET to an AUTHENTICATED
    fleet node so it can decrypt replicated connector credentials + vault keys — all
    fleet members must share the SAME KEK, else every cross-node decrypt fails with
    InvalidTag. HTTPS + fleet-secret authenticated; never exposed elsewhere."""
    _require_fleet(authorization)
    from .. import fleet_secrets as fs
    return fs.cp_bundle()


@router.post("/email-relay")
def email_relay(body: EmailRelay, authorization: str = Header(default="")):
    """Deliver an email on behalf of a fleet node that has no email service of its
    own (e.g. a tenant migrated to a node without one). The node composed + recorded
    it already; the CP just transports via its own service so mail still reaches the
    customer. Returns {ok, error, provider}."""
    _require_fleet(authorization)
    from .. import emailer
    res = emailer.transport_local(body.to, body.subject, body.html, body.text)
    if not res.get("ok"):
        logger.warning("email-relay transport failed (-> %s): %s", body.to, res.get("error"))
    return res


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


def _parse_iso(s: str | None) -> datetime | None:
    """A naive-UTC datetime from an ISO cursor string, else None."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except ValueError:
        return None


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


def _upsert_by_unique(db: Session, model, data: dict, unique_cols: list[str]):
    """Upsert reconciling on a SECONDARY unique key when the primary key doesn't
    match. A node can generate its own primary id for a row that already exists on
    the CP under a DIFFERENT id but the same natural key (e.g. an M365 identity keyed
    by ``(microsoft_tenant_id, entra_object_id)``); a blind insert then violates the
    unique constraint and — poisoning the transaction — aborts the whole push, which
    stalls the node's cursor and freezes replication. Reconciling here updates the
    existing row in place instead."""
    kw = _deser(model, data)
    pk = list(model.__table__.primary_key.columns)[0].name
    obj = db.get(model, kw.get(pk))
    if obj is None:
        obj = db.query(model).filter(
            *[getattr(model, c) == kw.get(c) for c in unique_cols]).first()
    if obj is not None:
        for k, v in kw.items():
            if k != pk:  # keep the CP's canonical id
                setattr(obj, k, v)
        return obj
    obj = model(**kw)
    db.add(obj)
    return obj


# State ranking for M365 managed sources: higher = more progressed. Used so a
# warm-STANDBY echo (which never collected) can't regress a collected row.
_MS_STATE_RANK = {
    "decommissioned": 0, "planned": 1, "provisioning": 2, "paused_by_admin": 3,
    "disconnected": 3, "baseline_pending": 4, "permission_required": 4,
    "credential_error": 4, "source_unavailable": 4, "retention_hold": 5,
    "empty": 6, "delayed": 6, "partial": 7, "active": 8,
}


def _upsert_managed_source(db: Session, model, data: dict):
    """Upsert an M365 managed source reconciling on its NATURAL key
    ``(tenant_id, integration_instance_id, workload, source_key)`` when the primary
    key doesn't match. A node can generate its own primary id for a source that
    already exists on the CP under a different id — e.g. after an instance was
    re-parented (see ``_reparent``) or a warm standby provisioned the same source —
    and a blind ``_upsert`` then creates a DUPLICATE row (one 'active', one
    'planned'). Reconciling on the natural key merges them instead.

    Guard: never let a lower-ranked push (a standby echo) regress a row that has
    actually collected — don't wipe a real ``last_collected_at`` and don't move a
    collected row backwards (e.g. active → planned)."""
    kw = _deser(model, data)
    pk = list(model.__table__.primary_key.columns)[0].name
    obj = db.get(model, kw.get(pk))
    if obj is None:
        obj = db.query(model).filter(
            model.tenant_id == kw.get("tenant_id"),
            model.integration_instance_id == kw.get("integration_instance_id"),
            model.workload == kw.get("workload"),
            model.source_key == kw.get("source_key")).first()
    if obj is None:
        db.add(model(**kw))
        return
    collected = getattr(obj, "last_collected_at", None) is not None
    for k, v in kw.items():
        if k == pk:  # keep the CP's canonical id
            continue
        if collected and k == "last_collected_at" and v is None:
            continue  # don't wipe a real collection timestamp with a standby echo
        if (collected and k == "state"
                and _MS_STATE_RANK.get(v, 5) < _MS_STATE_RANK.get(getattr(obj, "state", ""), 0)):
            continue  # don't regress a collected row's state (e.g. active → planned)
        setattr(obj, k, v)


class NodeIdent(BaseModel):
    name: str
    role: str = "customer-tenant"
    since: str | None = None  # ISO cursor for incremental pull (unused in v1)
    # Active/passive HA: separate ISO cursors for the incremental warm-standby data
    # replication (receipts + search index for tenants this node is the STANDBY for).
    standby_rcpt_cursor: str | None = None
    standby_doc_cursor: str | None = None
    # Warm-standby apply health from the node's PREVIOUS pull: which standby tenants
    # had rows fail to apply (pending>0 = incomplete replica) + whether the data
    # stream was caught up. Lets the CP show a truthful readiness + gate switchover.
    standby_report: dict | None = None


def _record_standby_report(db: Session, node: Node, report: dict) -> None:
    """Fold a standby node's apply-health report into per-tenant readiness. A tenant
    is marked synced (standby_synced_at=now, standby_pending=0) only when the node
    applied ALL its rows (pending==0) AND the data stream was caught up. Any pending
    rows leave standby_pending>0 so the UI shows "syncing" and switchover refuses."""
    try:
        pending = report.get("pending") or {}
        caught_up = bool(report.get("caught_up"))
        now = datetime.utcnow()
        for tid, cnt in pending.items():
            t = db.get(Tenant, tid)
            # Only trust the report for tenants this node is truly the standby for.
            if t is None or t.standby_node_id != node.id:
                continue
            t.standby_pending = int(cnt or 0)
            if int(cnt or 0) == 0 and caught_up:
                t.standby_synced_at = now
        db.commit()
    except Exception:  # noqa: BLE001 — a bad report must never break the pull
        db.rollback()
        logger.exception("failed to record standby report from node %s", node.id)


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
    # Record the warm-standby readiness the node reported for its LAST apply, so the
    # portal shows a TRUTHFUL "in sync" and switchover can refuse an incomplete
    # replica. Only tenants this node is actually the standby for are trusted.
    if body.standby_report:
        _record_standby_report(db, node, body.standby_report)
    # Active tenants (this node runs their workers) + STANDBY tenants (this node
    # keeps a warm read-only replica for HA). Config for BOTH goes down; the node's
    # scheduler naturally skips standby tenants because their node_id points at the
    # OTHER (active) node, so there's no double-collection.
    active_tenants = db.query(Tenant).filter(Tenant.node_id == node.id).all()
    standby_tenants = db.query(Tenant).filter(Tenant.standby_node_id == node.id,
                                              Tenant.node_id != node.id).all()
    tenants = active_tenants + standby_tenants
    tids = [t.id for t in tenants]
    standby_tids = [t.id for t in standby_tenants]
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

    # Warm-standby data replication: for tenants this node is the STANDBY for, ship
    # their recovery points (SnapshotReceipt) + search index (SearchDocument)
    # incrementally so the standby can serve search/recovery immediately on
    # switchover. Object BYTES aren't shipped — they live in shared cloud storage
    # (both nodes read the same bucket) or on the appliance (which re-targets the
    # new active node on switchover). Separate ISO cursors keep each stream correct.
    standby_receipts, standby_documents = [], []
    rcpt_next, doc_next = body.standby_rcpt_cursor, body.standby_doc_cursor
    standby_more = False
    if standby_tids:
        _LIMIT = 3000
        rcpt_since = _parse_iso(body.standby_rcpt_cursor)
        doc_since = _parse_iso(body.standby_doc_cursor)
        rq = db.query(SnapshotReceipt).filter(SnapshotReceipt.tenant_id.in_(standby_tids))
        if rcpt_since is not None:
            rq = rq.filter(SnapshotReceipt.created_at > rcpt_since)
        recs = rq.order_by(SnapshotReceipt.created_at.asc()).limit(_LIMIT).all()
        dq = db.query(SearchDocument).filter(SearchDocument.tenant_id.in_(standby_tids))
        if doc_since is not None:
            dq = dq.filter(SearchDocument.created_at > doc_since)
        docs = dq.order_by(SearchDocument.created_at.asc()).limit(_LIMIT).all()
        standby_receipts = [_ser(r) for r in recs]
        standby_documents = [_ser(d) for d in docs]
        if recs and recs[-1].created_at:
            rcpt_next = recs[-1].created_at.isoformat()
        if docs and docs[-1].created_at:
            doc_next = docs[-1].created_at.isoformat()
        standby_more = len(recs) >= _LIMIT or len(docs) >= _LIMIT

    # Stamp replication freshness so the admin can see how current each node is
    # (drives the HA "in sync" indicator for a standby node in the topology view).
    node.last_sync_at = datetime.utcnow()
    db.commit()

    pricing = db.get(PricingConfig, "default")
    # Integrations for the node's tenants + platform enable/disable, so the node
    # can serve its appliances' integration pull/report locally.
    integration_instances = (db.query(IntegrationInstance)
                              .filter(IntegrationInstance.tenant_id.in_(tids)).all())
    customer_storages = (db.query(CustomerStorage)
                         .filter(CustomerStorage.tenant_id.in_(tids)).all()) if tids else []
    # Microsoft 365 managed integration: the CP owns the connection, consent, scope
    # and identity-mapping decisions (set in the portal); the node runs discovery +
    # collection. Ship those CP-owned records down so the node can reconcile.
    m365_instances, m365_credentials, m365_scope = [], [], []
    m365_bindings, m365_desired = [], []
    m365_sources = []
    try:
        from ..integrations.microsoft365 import models as _m365
        m365_instances = [_ser(i) for i in integration_instances
                          if i.integration_type == "microsoft365"]
        m365_credentials = [_ser(c) for c in db.query(_m365.ManagedCredentialRef)
                            .filter(_m365.ManagedCredentialRef.tenant_id.in_(tids)).all()]
        m365_scope = [_ser(s) for s in db.query(_m365.IdentityScopePolicy)
                      .filter(_m365.IdentityScopePolicy.tenant_id.in_(tids)).all()]
        m365_bindings = [_ser(b) for b in db.query(_m365.ExternalIdentityBinding)
                         .filter(_m365.ExternalIdentityBinding.tenant_id.in_(tids)).all()]
        m365_desired = [_ser(d) for d in db.query(_m365.IntegrationDesiredState)
                        .filter(_m365.IntegrationDesiredState.tenant_id.in_(tids)).all()]
        # Managed sources are CP-authoritative (provisioned in the portal); the node
        # runs their Collections and MUST resolve them locally, else every managed
        # M365 collection logs "missing instance/source" and never collects.
        m365_sources = [_ser(s) for s in db.query(_m365.ManagedSource)
                        .filter(_m365.ManagedSource.tenant_id.in_(tids)).all()]
    except Exception:  # noqa: BLE001 — package optional; never break a pull
        pass
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
        "m365_instances": m365_instances,
        "m365_credentials": m365_credentials,
        "m365_scope_policies": m365_scope,
        "m365_bindings": m365_bindings,
        "m365_desired_states": m365_desired,
        "m365_managed_sources": m365_sources,
        "nodes": [_ser(n) for n in db.query(Node).all()],
        "pricing": _ser(pricing) if pricing else None,
        "pending_jobs": [_ser(j) for j in pending_jobs],
        "pending_insights": pending_insights,
        "agent_commands": agent_commands,
        "key_records": key_records,
        # Active/passive HA: which of the returned tenants this node holds as a
        # warm STANDBY replica, plus their incremental data + advanced cursors.
        "standby_tenant_ids": standby_tids,
        "standby_receipts": standby_receipts,
        "standby_documents": standby_documents,
        "standby_rcpt_cursor": rcpt_next,
        "standby_doc_cursor": doc_next,
        "standby_more": standby_more,
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
    # BYOS storage health probed on the owning node (health fields only, never creds).
    customer_storage_health: list[dict] = []
    # Compliance posture signals the node collected (M365 posture etc.) — the CP's
    # compliance engine evaluates them. Non-secret; upserted by natural key.
    compliance_signals: list[dict] = []
    insights: list[dict] = []
    integration_instances: list[dict] = []
    network_clients: list[dict] = []
    network_apps: list[dict] = []
    network_usage: list[dict] = []
    integration_runs: list[dict] = []
    communications: list[dict] = []
    admin_alerts: list[dict] = []
    log_entries: list[dict] = []
    # Microsoft 365 managed integration (node-owned runtime that flows UP): the
    # node discovers Entra identities + provisions/collects managed sources.
    m365_external_identities: list[dict] = []
    m365_managed_sources: list[dict] = []
    # Managed integration Collections created on the node (M365) that must exist on
    # the CP before their receipts (they normally flow CP→node only).
    managed_collections: list[dict] = []


_JOB_FIELDS = ("status", "processed", "total", "message", "error", "snapshot_id",
               "log", "started_at", "finished_at")
_AGENT_FIELDS = ("state", "version", "telemetry", "last_heartbeat_at", "collectors")
_APPLIANCE_FIELDS = ("state", "isolation_state", "software_version", "telemetry",
                     "tamper_state", "attestation_ok", "last_heartbeat_at",
                     "last_attestation_at", "model", "version_updated_at")
_CS_HEALTH_FIELDS = ("status", "used_bytes", "last_test_at", "last_test_ok",
                     "last_test_error")


def _push_is_stale(incoming: dict, stored, field: str = "last_heartbeat_at") -> bool:
    """True when a pushed device row is OLDER than what we already have — the
    appliance/agent is currently reporting elsewhere (e.g. the control plane after a
    switchover cooldown), so this node's copy is stale and must not clobber fresher
    liveness/attestation/state (which would flip a healthy device to a false alarm)."""
    cur = getattr(stored, field, None)
    if cur is None:
        return False  # we have no baseline — accept the push
    inc = _parse_iso(incoming.get(field))
    if inc is None:
        return True   # the node never heard from this device but we have — keep ours
    return inc < cur


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
              "integrations": 0, "network": 0, "communications": 0, "admin_alerts": 0,
              "logs": 0}
    # A node can hold data for a tenant/user that was removed on the control
    # plane; inserting it would violate a FK and abort the whole push. Skip any
    # row whose tenant or owner isn't present here so one orphan can't block sync.
    valid_tenants = {t for (t,) in db.query(Tenant.id).all()}
    valid_users = {u for (u,) in db.query(User.id).all()}

    def _known(row: dict) -> bool:
        tid = row.get("tenant_id")
        return tid is None or tid in valid_tenants

    # Managed integration Collections (M365) are created on the node but Collections
    # normally flow only CP→node, so upsert them here FIRST — a receipt referencing a
    # collection the CP has never seen would otherwise violate the FK and abort the
    # WHOLE push. Only for a vault that exists here (avoid a vault_id FK violation).
    valid_vaults = {v for (v,) in db.query(Vault.id).all()}
    for c in body.managed_collections:
        if (not _known(c) or not _has_pk(Collection, c)
                or c.get("vault_id") not in valid_vaults):
            continue
        _upsert(db, Collection, c)
        counts["collections"] = counts.get("collections", 0) + 1
    db.flush()  # make the new collections visible to the receipt FK below

    # Skip a receipt whose collection isn't present here (even after the upsert
    # above) so one orphaned receipt can never wedge the entire push again.
    valid_collections = {c for (c,) in db.query(Collection.id).all()}
    for r in body.receipts:
        if (not _known(r) or not _has_pk(SnapshotReceipt, r)
                or r.get("collection_id") not in valid_collections):
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
            if _push_is_stale(a, ag):
                continue
            _apply(ag, a, _AGENT_FIELDS)
            counts["agents"] += 1
    for a in body.appliances:
        if not _has_pk(Appliance, a):
            continue
        ap = db.get(Appliance, a.get("id"))
        if ap:
            # A node that ISN'T currently receiving this device's heartbeats (it's
            # riding the control plane after a switchover cooldown) would otherwise
            # push its STALE default row up and clobber the CP's fresh liveness +
            # attestation (→ a false "Attestation failed"). Only apply when the
            # pushed heartbeat is at least as new as what we already have.
            if _push_is_stale(a, ap):
                continue
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
    # BYOS storage HEALTH the owning node probed (it has the creds + network path).
    # Apply ONLY the health fields — never credentials/config (CP-authoritative).
    for ch in body.customer_storage_health:
        if not _known(ch) or not _has_pk(CustomerStorage, ch):
            continue
        row = db.get(CustomerStorage, ch.get("id"))
        if row is not None:
            _apply(row, ch, _CS_HEALTH_FIELDS)
            counts["customer_storage_health"] = counts.get("customer_storage_health", 0) + 1
    # Compliance posture signals the node collected (M365 posture etc.). The engine
    # runs on the CP, so it needs these. Upsert by the natural key (tenant, provider,
    # capability, scope) since row ids differ between node + CP. Skip unknown tenants.
    if body.compliance_signals:
        from ..compliance.models import ComplianceSignal
        _CS_COLS = {c.name for c in ComplianceSignal.__table__.columns}
        for cs in body.compliance_signals:
            if not _known(cs):
                continue
            existing = (db.query(ComplianceSignal)
                        .filter(ComplianceSignal.tenant_id == cs.get("tenant_id"),
                                ComplianceSignal.provider == (cs.get("provider") or ""),
                                ComplianceSignal.capability == (cs.get("capability") or ""),
                                ComplianceSignal.scope == (cs.get("scope") or "")).first())
            kw = _deser(ComplianceSignal, {k: v for k, v in cs.items() if k in _CS_COLS})
            if existing is not None:
                for k, v in kw.items():
                    if k != "id":
                        setattr(existing, k, v)
            else:
                db.add(ComplianceSignal(**kw))
            counts["compliance_signals"] = counts.get("compliance_signals", 0) + 1
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
    # Platform-admin alerts a node raised (appliance/storage health). Emit them
    # from the control plane, where the platform admins + mail service live. Dedupe
    # is enforced by admin_notifications.emit via AdminNotificationLog.
    if body.admin_alerts:
        from .. import admin_notifications
        for al in body.admin_alerts:
            try:
                admin_notifications.emit_pushed(db, al)
                counts["admin_alerts"] += 1
            except Exception:  # noqa: BLE001 — one bad alert must not fail the push
                logger.exception("admin alert emit failed (type=%s)", al.get("type"))
    # Unified logs the node captured (its app logs + managed appliances/agents +
    # audit dual-writes). Upsert by id; stamp the pushing node so the admin can
    # attribute + drill down. Record last_log_push_at so node details show the
    # push freshness.
    from ..models import LogEntry
    dropped_logs = 0
    for le in body.log_entries:
        if not _has_pk(LogEntry, le) or not _known(le):
            dropped_logs += 1
            continue
        if db.get(LogEntry, le.get("id")) is None:
            kw = _deser(LogEntry, le)
            # Attribute to the pushing node whenever the row lacks it. A node with
            # no is_self row in its (replicated) DB stamps node_id=None locally, so
            # setdefault would leave it unattributed — override empty values too so
            # EVERY node's logs are viewable + scopable in Platform Logs.
            if push_node is not None:
                if not kw.get("node_id"):
                    kw["node_id"] = push_node.id
                if not kw.get("node_name"):
                    kw["node_name"] = push_node.name
            db.add(LogEntry(**kw))
            counts["logs"] += 1
    if push_node is not None and body.log_entries:
        push_node.last_log_push_at = datetime.utcnow()
        if dropped_logs:
            logger.warning("node-sync: dropped %d/%d log rows from node %s "
                           "(unknown tenant or missing id)", dropped_logs,
                           len(body.log_entries), push_node.name)
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

    # Microsoft 365 instance config is CP-authoritative (portal-set); only the
    # node's RUNTIME status flows back up.
    _M365_RUNTIME = ("status", "provision_state", "provision_message", "last_run_at",
                     "last_success_at", "last_error", "last_stats")
    for row in body.integration_instances:
        owner = row.get("owner_user_id")
        if not _ok(row) or (owner and owner not in valid_users):
            continue
        if row.get("integration_type") == "microsoft365":
            inst = db.get(IntegrationInstance, row.get("id"))
            if inst is not None:
                for f in _M365_RUNTIME:
                    if f not in row:
                        continue
                    val = row[f]
                    if f.endswith("_at") and isinstance(val, str):
                        try:
                            dt = datetime.fromisoformat(val)
                            val = dt.replace(tzinfo=None) if dt.tzinfo else dt
                        except ValueError:
                            val = None
                    setattr(inst, f, val)
                counts["integrations"] += 1
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
    # Microsoft 365 node-discovered identities + provisioned managed sources.
    if body.m365_external_identities or body.m365_managed_sources:
        try:
            from ..integrations.microsoft365 import models as _m365
            # A node can discover under a STALE local M365 instance id (e.g. after the
            # instance was re-created on the CP), orphaning the pushed identities from
            # the CP's canonical instance so the portal shows "no identities". When a
            # tenant has exactly ONE M365 instance on the CP, re-parent any pushed row
            # that references a different id to it (self-heals on the next push).
            _canon: dict[str, str | None] = {}

            def _reparent(row: dict) -> dict:
                tid = row.get("tenant_id")
                iid = row.get("integration_instance_id")
                if not tid or not iid:
                    return row
                if tid not in _canon:
                    ids = [r[0] for r in db.query(IntegrationInstance.id).filter(
                        IntegrationInstance.tenant_id == tid,
                        IntegrationInstance.integration_type == "microsoft365").all()]
                    _canon[tid] = ids[0] if len(ids) == 1 else None
                canon = _canon[tid]
                if canon and iid != canon:
                    logger.info("m365 push: re-parenting %s from stale instance %s → %s",
                                row.get("id"), iid, canon)
                    return {**row, "integration_instance_id": canon}
                return row

            # SAVEPOINT: an M365 ingest hiccup must NEVER 500 the push (which would
            # stall the node's document cursor and freeze search replication). On
            # failure only this nested block rolls back; the already-flushed
            # receipts/documents/jobs and the outer transaction survive.
            with db.begin_nested():
                for row in body.m365_external_identities:
                    if not _ok(row):
                        continue
                    # Reconcile on the tenant-scoped durable identity key so a node's
                    # own id for an identity it already pushed updates in place, WITHOUT
                    # ever touching another tenant's row for the same Entra identity
                    # (two Arkive tenants can share one Microsoft 365 org).
                    _upsert_by_unique(db, _m365.ExternalIdentity, _reparent(row),
                                      ["tenant_id", "microsoft_tenant_id",
                                       "entra_object_id"])
                    counts["integrations"] += 1
                for row in body.m365_managed_sources:
                    owner = row.get("owner_user_id")
                    if not _ok(row) or (owner and owner not in valid_users):
                        continue
                    _upsert_managed_source(db, _m365.ManagedSource, _reparent(row))
                    counts["integrations"] += 1
        except Exception:  # noqa: BLE001 — package optional; never fail the push
            logger.exception("m365 push ingest failed")


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


@router.get("/debug")
def node_debug(authorization: str = Header(default=""), db: Session = Depends(get_db)):
    """Fleet-authenticated DB health for this node (used by the control plane's
    debug API to diagnose which node's database is slow)."""
    _require_fleet(authorization)
    from .debug import _brief_stats
    return _brief_stats(db)


@router.post("/debug-query")
def node_debug_query(body: dict, authorization: str = Header(default="")):
    """Fleet-authenticated READ-ONLY query against THIS node's local database, so
    the control plane's debug API can inspect node-local state (e.g. a promoted
    standby's Tenant.node_id / Node.is_self) without enabling a separate debug key
    on the node. Same read-only guardrails as the CP /debug/query."""
    _require_fleet(authorization)
    from .debug import _execute_readonly_query
    return _execute_readonly_query(str(body.get("sql", "")),
                                   int(body.get("limit", 200) or 200),
                                   int(body.get("timeout_ms", 15000) or 15000))


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


class TenantPlanReq(BaseModel):
    tenant_id: str
    actor: str | None = None
    role: str | None = None
    is_platform_admin: bool = False
    options: list[str] | None = None
    licensed_tb: float | None = None
    licensed_seats: int | None = None
    appliance_plan: list[dict] | None = None


@router.post("/tenant-plan")
def node_tenant_plan(body: TenantPlanReq, authorization: str = Header(default=""),
                     db: Session = Depends(get_db)):
    """A customer-tenant node forwards a Protection Setup change here so it's applied
    on the control plane — which OWNS Tenant/User and replicates them back down.
    Applying it only on the node would be reverted by the next CP→node pull."""
    _require_fleet(authorization)
    from ..models import Tenant, User
    from .billing import PlanUpdate, _apply_plan_change
    tenant = db.get(Tenant, body.tenant_id)
    if tenant is None:
        raise HTTPException(404, "unknown tenant")
    user = db.get(User, body.actor) if body.actor else None
    if user is None:
        user = (db.query(User)
                .filter(User.tenant_id == tenant.id, User.role == "owner").first())
    pu = PlanUpdate(options=body.options, licensed_tb=body.licensed_tb,
                    licensed_seats=body.licensed_seats, appliance_plan=body.appliance_plan)
    view = _apply_plan_change(db, tenant=tenant, user=user, actor_role=(body.role or ""),
                              is_platform_admin=bool(body.is_platform_admin), body=pu, notify=True)
    return {"ok": True, "view": view}

