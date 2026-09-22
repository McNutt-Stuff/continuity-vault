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
from json import JSONDecodeError
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .. import keybroker
from ..config import get_settings
from ..db import WorkerSessionLocal as SessionLocal
from ..models import (
    Appliance,
    ApplianceStorage,
    AdminAlertEvent,
    Collection,
    Communication,
    ConfigObject,
    ConnectorAccount,
    CustomerStorage,
    DesktopAgent,
    IntegrationConfig,
    IntegrationInstance,
    IntegrationRun,
    LogEntry,
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

logger = logging.getLogger("cv.replication")

_thread: threading.Thread | None = None
_log_thread: threading.Thread | None = None
_running_jobs: set[str] = set()
_running_insights: set[str] = set()

# Upsert in FK-dependency order so a strict database accepts the rows.
_PULL_ORDER = [
    ("config_objects", ConfigObject),
    ("source_configs", SourceConfig),
    ("integration_configs", IntegrationConfig),
    ("service_objects", ServiceObject),
    ("nodes", Node),
    ("tenants", Tenant),
    ("users", User),
    ("vaults", Vault),
    ("desktop_agents", DesktopAgent),
    ("appliances", Appliance),
    ("appliance_storages", ApplianceStorage),
    ("connector_accounts", ConnectorAccount),
    ("customer_storages", CustomerStorage),
    ("collections", Collection),
]

# Fields owned by the NODE, never overwritten by a pull (the node produces these
# at runtime and pushes them UP; pulling the control plane's stale copy back down
# would clobber a just-recorded sync error/cursor before it's ever pushed).
_PULL_EXCLUDE = {
    "desktop_agents": {"pending_commands", "last_scan", "fs_expansions"},
    "connector_accounts": {"sync_cursor", "last_sync_at", "last_object_count",
                           "last_error", "last_error_at", "auth_status"},
    # The node's scheduler owns each mapping's run stamp; pulling the control
    # plane's stale copy (NULL, since the node — not the CP — runs these tenants)
    # would make every source look "due" again and re-back-up every tick.
    "collections": {"last_backup_run_at"},
    # A node-routed appliance heartbeats its assigned NODE, so the node owns these
    # runtime fields. Pulling the control plane's stale copy back down would clobber
    # the just-recorded liveness/telemetry before it's pushed up — freezing the
    # appliance as perpetually "offline" in the portal.
    "appliances": {"last_heartbeat_at", "last_attestation_at", "telemetry",
                   "state", "isolation_state", "tamper_state", "attestation_ok",
                   "software_version", "version_updated_at"},
    # Per-storage capacity/health come from the appliance's heartbeat telemetry,
    # applied on the node — the CP's copy would be stale/empty.
    "appliance_storages": {"capacity_bytes", "used_bytes", "health"},
    # Integration instances are created + driven on the node (portal calls are
    # proxied there), so they're node-authoritative and pushed up, never pulled.
}

# Microsoft 365 managed integration: the CP owns connection/consent/scope/mapping
# (portal-set) and ships them down; the node runs discovery + collection. Applied
# AFTER the core tables (tenants/users present) so FKs resolve. Instance runtime is
# node-owned, so it's excluded on pull (flows UP via the push).
try:
    from ..integrations.microsoft365 import models as _m365m
    _PULL_ORDER += [
        ("m365_instances", IntegrationInstance),
        ("m365_credentials", _m365m.ManagedCredentialRef),
        ("m365_scope_policies", _m365m.IdentityScopePolicy),
        ("m365_bindings", _m365m.ExternalIdentityBinding),
        ("m365_desired_states", _m365m.IntegrationDesiredState),
    ]
    _PULL_EXCLUDE["m365_instances"] = {"status", "provision_state", "provision_message",
                                       "last_run_at", "last_success_at", "last_error",
                                       "last_stats"}
    # The node acknowledges desired state locally; don't let the CP's copy reset it.
    _PULL_EXCLUDE["m365_desired_states"] = {"applied_version", "applied_at", "status"}
except Exception:  # noqa: BLE001 — package optional
    _m365m = None

# Catalog / entitlements are CP-authoritative commercial state; ship them DOWN so a
# node can derive entitlements + resolve plan pricing locally. Global tables first,
# tenant-scoped ones AFTER tenants (FK order).
try:
    from ..catalog.models import Plan as _CatPlan, PlanVersion as _CatPlanVersion
    from ..entitlements.models import (AddOn as _AddOn, TenantAddOn as _TenantAddOn,
                                       EntitlementOverride as _EntOverride)
    _PULL_ORDER += [
        ("catalog_plans", _CatPlan),
        ("catalog_plan_versions", _CatPlanVersion),
        ("addons", _AddOn),
        ("tenant_addons", _TenantAddOn),
        ("entitlement_overrides", _EntOverride),
    ]
except Exception:  # noqa: BLE001 — packages optional
    pass

# Subscriptions are CP-authoritative commercial state; ship them DOWN so a node can
# read a tenant's subscription + items offline. After tenants (FK order).
try:
    from ..subscriptions import Subscription as _Sub, SubscriptionItem as _SubItem
    _PULL_ORDER += [
        ("subscriptions", _Sub),
        ("subscription_items", _SubItem),
    ]
except Exception:  # noqa: BLE001 — module optional
    pass

# Signed entitlement snapshots are minted on the CP and validated on the node
# before enforcement; ship them DOWN. After tenants (FK order).
try:
    from ..models import EntitlementSnapshot as _EntSnap
    _PULL_ORDER += [("entitlement_snapshots", _EntSnap)]
except Exception:  # noqa: BLE001 — optional
    pass


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _state_path() -> Path:
    base = Path(os.environ.get("CV_KEY_STORE", "./cv_keystore")).parent
    return base / "replication_state.json"


def _read_state() -> dict:
    try:
        return json.loads(_state_path().read_text())
    except Exception:
        return {}


def _write_state(d: dict) -> None:
    try:
        _state_path().write_text(json.dumps(d))
    except Exception:
        logger.debug("could not persist replication state", exc_info=True)


def _load_cursor() -> str | None:
    return _read_state().get("push_cursor")


def _save_cursor(iso: str) -> None:
    st = _read_state()
    st["push_cursor"] = iso
    _write_state(st)


def _load_insights_cursor() -> str | None:
    return _read_state().get("insights_cursor")


def _save_insights_cursor(iso: str) -> None:
    st = _read_state()
    st["insights_cursor"] = iso
    _write_state(st)


def _post(path: str, payload: dict) -> dict | None:
    s = get_settings()
    if not s.control_plane_url or not s.node_secret:
        return None
    url = s.control_plane_url.rstrip("/") + "/api" + path
    try:
        # default=str so a non-JSON-serializable value (e.g. raw bytes in a log
        # entry's meta) is coerced to a string instead of raising — a single
        # poison row must never wedge the whole replication push forever.
        body = json.dumps(payload, default=str).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {s.node_secret}"},
            method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode(errors="replace")
            try:
                data = json.loads(raw)
            except JSONDecodeError:
                logger.warning("replication %s returned non-JSON response", path)
                return None
            if not isinstance(data, dict):
                logger.warning("replication %s returned unexpected payload type %s",
                               path, type(data).__name__)
                return None
            return data
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode(errors="replace")[:240]
        except Exception:  # noqa: BLE001
            body = ""
        msg = f" body={body}" if body else ""
        logger.warning("replication %s rejected: %s%s", path, e.code, msg)
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
                logger.warning("replication: invalid datetime for %s.%s value=%r",
                               getattr(model, "__tablename__", model), k, v)
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
    _st0 = _read_state()
    bundle = _post("/nodes/sync/pull", {
        "name": s.node_name or s.domain, "role": s.node_role or "customer-tenant",
        # Warm-standby data cursors (receipts + search index for tenants we're the
        # STANDBY for) — advanced only after a confirmed apply below.
        "standby_rcpt_cursor": _st0.get("standby_rcpt_cursor"),
        "standby_doc_cursor": _st0.get("standby_doc_cursor"),
    })
    if not bundle:
        return 0
    n = 0
    skipped = 0
    with SessionLocal() as db:
        # Deterministic ordering: no autoflush surprises. Each row upserts inside
        # a SAVEPOINT so a single orphan/bad row is skipped (logged) instead of
        # aborting the whole pull, and we COMMIT after each table so a parent row
        # is durably present before its children (FK safety across tables).
        db.autoflush = False

        def apply_one(model, row) -> bool:
            try:
                pk = list(model.__table__.primary_key.columns)[0].name
                if not row.get(pk):
                    logger.warning("replication: skipped %s row with missing pk '%s'",
                                   getattr(model, "__tablename__", model), pk)
                    return False
                with db.begin_nested():
                    _upsert(db, model, row)
                    db.flush()
                return True
            except Exception as exc:  # noqa: BLE001
                logger.warning("replication: skipped a %s row: %s",
                               getattr(model, "__tablename__", model), str(exc)[:160])
                return False

        if bundle.get("pricing"):
            apply_one(PricingConfig, bundle["pricing"])
            db.commit()
        for key, model in _PULL_ORDER:
            exclude = _PULL_EXCLUDE.get(key)
            for row in bundle.get(key, []) or []:
                if exclude:
                    row = {k: v for k, v in row.items() if k not in exclude}
                if apply_one(model, row):
                    n += 1
                else:
                    skipped += 1
            db.commit()  # persist this table before dependent tables
    # New Config Objects / source links just landed — drop the platform-config
    # cache so OAuth client creds (needed to refresh tokens) resolve immediately.
    try:
        from .. import platform_config
        platform_config.invalidate()
    except Exception:
        pass
    # Warm-standby data replication: apply the recovery points (SnapshotReceipt) +
    # search index (SearchDocument) for tenants this node is the STANDBY for, so it
    # can serve search/recovery the moment it's promoted. Idempotent upsert by pk;
    # cursors advance only after a successful apply (retried whole next cycle).
    s_recs = bundle.get("standby_receipts") or []
    s_docs = bundle.get("standby_documents") or []
    if s_recs or s_docs:
        applied = 0
        with SessionLocal() as db:
            db.autoflush = False
            for model, rows in ((SnapshotReceipt, s_recs), (SearchDocument, s_docs)):
                for row in rows:
                    try:
                        with db.begin_nested():
                            _upsert(db, model, row)
                            db.flush()
                        applied += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("standby: skipped a %s row: %s",
                                     getattr(model, "__tablename__", model), str(exc)[:120])
            db.commit()
        st = _read_state()
        if bundle.get("standby_rcpt_cursor"):
            st["standby_rcpt_cursor"] = bundle["standby_rcpt_cursor"]
        if bundle.get("standby_doc_cursor"):
            st["standby_doc_cursor"] = bundle["standby_doc_cursor"]
        _write_state(st)
        logger.info("replication standby: warm-replicated %d receipt(s) + %d doc(s)%s",
                    len(s_recs), len(s_docs),
                    " (more pending)" if bundle.get("standby_more") else "")
    # Forwarded agent commands (portal "Sync now" for node-routed agents): append
    # to the local agent queue so the node delivers them on the agent's next
    # heartbeat. enqueue_command dedupes, so a re-forward is harmless.
    agent_cmds = bundle.get("agent_commands") or {}
    if agent_cmds:
        with SessionLocal() as db:
            forwarded = 0
            for aid, cmds in agent_cmds.items():
                ag = db.get(DesktopAgent, aid)
                if ag is None:
                    continue
                for c in cmds or []:
                    ag.enqueue_command(c)
                    forwarded += 1
            db.commit()
        if forwarded:
            logger.info("forwarded %d agent command(s) from control plane", forwarded)
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
    # Admin/user-requested insight (re)generation for our tenants: mine the local
    # index and push the report back to the control plane.
    for uid in bundle.get("pending_insights", []) or []:
        _run_insight_request(uid)
    logger.info("replication pull: %d assigned tenant(s), %d row(s) synced%s",
                bundle.get("assigned", 0), n, f", {skipped} skipped" if skipped else "")
    return n


def _run_insight_request(user_id: str) -> None:
    if not user_id or user_id in _running_insights:
        return
    _running_insights.add(user_id)

    def _go() -> None:
        try:
            from .insights import generate_for_user
            with SessionLocal() as db:
                u = db.get(User, user_id)
                if u is not None:
                    generate_for_user(db, u)
        except Exception:  # noqa: BLE001
            logger.exception("insight generation failed for user %s", user_id)
        finally:
            _running_insights.discard(user_id)
            try:
                _push(get_settings())  # report the fresh insight back promptly
            except Exception:  # noqa: BLE001
                logger.debug("post-insight push failed", exc_info=True)

    threading.Thread(target=_go, name=f"cv-node-insight-{user_id[:8]}", daemon=True).start()


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
    ins_cursor = _load_insights_cursor()
    ins_since = None
    if ins_cursor:
        try:
            ins_since = datetime.fromisoformat(ins_cursor)
        except ValueError:
            ins_since = None
    ins_high = ins_since
    receipts, documents, accounts = [], [], []
    jobs, agents, appliances = [], [], []
    appliance_storages = []
    insights = []
    integ_cursor = _read_state().get("integrations_cursor")
    integ_since = None
    if integ_cursor:
        try:
            integ_since = datetime.fromisoformat(integ_cursor)
        except ValueError:
            integ_since = None
    integ_high = integ_since
    integ_instances, net_clients, net_apps, net_usage, integ_runs = [], [], [], [], []
    communications = []
    comm_cursor = _read_state().get("communications_cursor")
    comm_since = None
    if comm_cursor:
        try:
            comm_since = datetime.fromisoformat(comm_cursor)
        except ValueError:
            comm_since = None
    comm_high = comm_since
    alerts = []
    alerts_cursor = _read_state().get("admin_alerts_cursor")
    alerts_since = None
    if alerts_cursor:
        try:
            alerts_since = datetime.fromisoformat(alerts_cursor)
        except ValueError:
            alerts_since = None
    alerts_high = alerts_since
    m365_identities: list = []
    m365_sources: list = []
    managed_collections: list = []
    m365_cursor = _read_state().get("m365_cursor")
    m365_since = None
    if m365_cursor:
        try:
            m365_since = datetime.fromisoformat(m365_cursor)
        except ValueError:
            m365_since = None
    m365_high = m365_since
    with SessionLocal() as db:
        # Only push data for tenants THIS node is ACTIVE for. A warm-standby node
        # also holds other tenants' replicated receipts/docs in its local DB — those
        # must NEVER be echoed back to the CP (it's their source). For a non-HA node
        # every local row is owned, so this filter is a no-op.
        self_node = (db.query(Node).filter(
                        Node.name == (s.node_name or s.domain),
                        Node.role == (s.node_role or "customer-tenant")).first()
                     or db.query(Node).filter(Node.is_self.is_(True)).first())
        owned_tids = ([tid for (tid,) in db.query(Tenant.id)
                       .filter(Tenant.node_id == self_node.id).all()]
                      if self_node else None)
        rq = db.query(SnapshotReceipt)
        if owned_tids is not None:
            rq = rq.filter(SnapshotReceipt.tenant_id.in_(owned_tids))
        if since is not None:
            rq = rq.filter(SnapshotReceipt.created_at > since)
        for r in rq.order_by(SnapshotReceipt.created_at.asc()).limit(2000).all():
            receipts.append(_row(r))
            if r.created_at and (high is None or r.created_at > high):
                high = r.created_at
        dq = db.query(SearchDocument)
        if owned_tids is not None:
            dq = dq.filter(SearchDocument.tenant_id.in_(owned_tids))
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
        # Per-volume capacity/health for node-routed appliances (built-in +
        # dedicated RAID/SMART), so the portal's appliance Storage view renders
        # the real drive-health drill-down instead of the telemetry fallback.
        for st in db.query(ApplianceStorage).all():
            appliance_storages.append(_row(st))
        # Digital-footprint insights refreshed since the last push (daily job or
        # an admin/user request) — report them so the portal stays authoritative.
        iq = db.query(UserInsights)
        if ins_since is not None:
            iq = iq.filter(UserInsights.generated_at > ins_since)
        for row in iq.order_by(UserInsights.generated_at.asc()).limit(1000).all():
            insights.append(_row(row))
            if row.generated_at and (ins_high is None or row.generated_at > ins_high):
                ins_high = row.generated_at
        # Integration telemetry the appliances reported (instance status + network
        # rows changed since the cursor). Curation (monitor_state/of_interest) is
        # CP-owned, so pushing telemetry back never clobbers it.
        for i in db.query(IntegrationInstance).all():
            integ_instances.append(_row(i))
        for model, sink in ((NetworkClient, net_clients), (NetworkApp, net_apps),
                            (NetworkUsage, net_usage)):
            q = db.query(model)
            if integ_since is not None:
                q = q.filter(model.updated_at > integ_since)
            for row in q.order_by(model.updated_at.asc()).limit(4000).all():
                sink.append(_row(row))
                if row.updated_at and (integ_high is None or row.updated_at > integ_high):
                    integ_high = row.updated_at
        rq2 = db.query(IntegrationRun)
        if integ_since is not None:
            rq2 = rq2.filter(IntegrationRun.created_at > integ_since)
        for row in rq2.order_by(IntegrationRun.created_at.asc()).limit(1000).all():
            integ_runs.append(_row(row))
        # Outbound-email history the node's email service recorded, so the admin's
        # per-user communications log on the control plane is complete.
        cq = db.query(Communication)
        if comm_since is not None:
            cq = cq.filter(Communication.created_at > comm_since)
        for row in cq.order_by(Communication.created_at.asc()).limit(2000).all():
            communications.append(_row(row))
            if row.created_at and (comm_high is None or row.created_at > comm_high):
                comm_high = row.created_at
        # Platform-admin alerts raised on this node (appliance/storage health) —
        # delivered by the control plane, where the admins + mail service live.
        aq = db.query(AdminAlertEvent)
        if alerts_since is not None:
            aq = aq.filter(AdminAlertEvent.created_at > alerts_since)
        for row in aq.order_by(AdminAlertEvent.created_at.asc()).limit(500).all():
            alerts.append(_row(row))
            if row.created_at and (alerts_high is None or row.created_at > alerts_high):
                alerts_high = row.created_at
        # Microsoft 365: Entra identities discovered + managed sources provisioned
        # on this node flow UP so the portal (CP) shows discovery + collection state.
        if _m365m is not None:
            iq = db.query(_m365m.ExternalIdentity)
            if m365_since is not None:
                iq = iq.filter(_m365m.ExternalIdentity.updated_at > m365_since)
            for row in iq.order_by(_m365m.ExternalIdentity.updated_at.asc()).limit(5000).all():
                m365_identities.append(_row(row))
                if row.updated_at and (m365_high is None or row.updated_at > m365_high):
                    m365_high = row.updated_at
            sq = db.query(_m365m.ManagedSource)
            if m365_since is not None:
                sq = sq.filter(_m365m.ManagedSource.updated_at > m365_since)
            for row in sq.order_by(_m365m.ManagedSource.updated_at.asc()).limit(5000).all():
                m365_sources.append(_row(row))
                if row.updated_at and (m365_high is None or row.updated_at > m365_high):
                    m365_high = row.updated_at
        # Managed integration Collections are CREATED ON THE NODE (during M365
        # provisioning) but Collections normally flow CP→node, so the CP never gets
        # them — and a SnapshotReceipt pushed for one violates the collection FK,
        # wedging the ENTIRE push. Ship managed collections UP (ahead of receipts on
        # the CP) so the FK target exists. Bounded + idempotent (few per tenant).
        for c in db.query(Collection).limit(5000).all():
            if (c.config or {}).get("managed"):
                managed_collections.append(_row(c))
    if not (receipts or documents or accounts or jobs or agents or appliances
            or appliance_storages or insights
            or integ_instances or net_clients or net_apps or net_usage or integ_runs
            or communications or alerts
            or m365_identities or m365_sources):
        # Nothing to replicate, but this node's own logs still must reach the CP.
        _push_logs(s)
        return 0
    node = s.node_name or s.domain
    role = s.node_role or "customer-tenant"
    # Push DATA and LOGS as two independent requests so a rejected/poison log
    # batch can never wedge managed-source / receipt / discovery replication (the
    # symptom: the portal stops seeing collection + SharePoint/Teams while the
    # node keeps heartbeating). Each advances only its own cursors on success.
    has_data = (receipts or documents or accounts or jobs or agents or appliances
                or appliance_storages or insights or integ_instances or net_clients
                or net_apps or net_usage or integ_runs or communications or alerts
                or m365_identities or m365_sources)
    if has_data:
        res = _post("/nodes/sync/push", {
            "node": node, "role": role,
            "receipts": receipts, "documents": documents, "connector_accounts": accounts,
            "jobs": jobs, "agents": agents, "appliances": appliances,
            "appliance_storages": appliance_storages, "insights": insights,
            "integration_instances": integ_instances, "network_clients": net_clients,
            "network_apps": net_apps, "network_usage": net_usage, "integration_runs": integ_runs,
            "communications": communications, "admin_alerts": alerts,
            "m365_external_identities": m365_identities, "m365_managed_sources": m365_sources,
            "managed_collections": managed_collections,
        })
        if res and res.get("ok"):
            if high is not None:
                _save_cursor(high.isoformat())
            if ins_high is not None:
                _save_insights_cursor(ins_high.isoformat())
            if integ_high is not None:
                st = _read_state(); st["integrations_cursor"] = integ_high.isoformat(); _write_state(st)
            if comm_high is not None:
                st = _read_state(); st["communications_cursor"] = comm_high.isoformat(); _write_state(st)
            if alerts_high is not None:
                st = _read_state(); st["admin_alerts_cursor"] = alerts_high.isoformat(); _write_state(st)
            if m365_high is not None:
                st = _read_state(); st["m365_cursor"] = m365_high.isoformat(); _write_state(st)
            logger.info("replication push: receipts=%d documents=%d jobs=%d agents=%d "
                        "appliances=%d storages=%d insights=%d integrations=%d network=%d m365=%d",
                        len(receipts), len(documents), len(jobs), len(agents),
                        len(appliances), len(appliance_storages), len(insights),
                        len(integ_instances),
                        len(net_clients) + len(net_apps) + len(net_usage),
                        len(m365_identities) + len(m365_sources))
    # Forward this node's logs (own cursor, independent of the data push above).
    _push_logs(s)
    return len(receipts) + len(documents)


def _row(obj) -> dict:
    out = {}
    for c in obj.__table__.columns:
        v = getattr(obj, c.name)
        out[c.name] = v.isoformat() if isinstance(v, datetime) else v
    return out


def _sanitize_log(d: dict) -> dict:
    """Bound a log-entry row so one oversized/odd entry can't poison the whole
    push batch (message capped; meta dropped if huge or non-serializable)."""
    msg = d.get("message")
    if isinstance(msg, (bytes, bytearray)):
        msg = msg.decode(errors="replace")
        d["message"] = msg
    if isinstance(msg, str) and len(msg) > 16000:
        d["message"] = msg[:16000] + "…[truncated]"
    meta = d.get("meta")
    if meta is not None:
        try:
            if len(json.dumps(meta, default=str)) > 16000:
                d["meta"] = {"_truncated": True}
        except Exception:  # noqa: BLE001
            d["meta"] = {"_unserializable": True}
    return d


def _push_logs(s) -> int:
    """Forward this node's captured log lines to the control plane's unified log
    store — INDEPENDENT of data replication, so EVERY non-control-plane node's logs
    reach Platform Logs even with no assigned tenants or with federation off. The
    logs_cursor advances only on confirmed delivery, so a failed push retries the
    whole batch next cycle and no line is dropped."""
    if not s.control_plane_url or not s.node_secret:
        return 0
    log_entries: list = []
    log_cursor = _read_state().get("logs_cursor")
    log_since = None
    if log_cursor:
        try:
            log_since = datetime.fromisoformat(log_cursor)
        except ValueError:
            log_since = None
    log_high = log_since
    with SessionLocal() as db:
        lq = db.query(LogEntry)
        if log_since is not None:
            lq = lq.filter(LogEntry.created_at > log_since)
        for row in lq.order_by(LogEntry.created_at.asc()).limit(5000).all():
            log_entries.append(_sanitize_log(_row(row)))
            if row.created_at and (log_high is None or row.created_at > log_high):
                log_high = row.created_at
    if not log_entries:
        return 0
    node = s.node_name or s.domain
    role = s.node_role or "customer-tenant"
    lres = _post("/nodes/sync/push", {"node": node, "role": role, "log_entries": log_entries})
    if lres and lres.get("ok"):
        if log_high is not None:
            st = _read_state(); st["logs_cursor"] = log_high.isoformat(); _write_state(st)
        logger.info("log forward: logs=%d", len(log_entries))
        return len(log_entries)
    return 0


def start_log_forwarder() -> None:
    """Forward node logs to the control plane on EVERY non-control-plane node,
    independent of federation/data replication, so all logs are viewable from the
    control plane (a node with no tenants, or with node_sync_scope off, still
    forwards its logs). On a federated node the replication loop already forwards
    logs via _push_logs, so this only runs when replication ISN'T."""
    global _log_thread
    s = get_settings()
    if _log_thread is not None:
        return
    if not s.control_plane_url or not s.node_secret:
        logger.warning("log forwarding disabled: control_plane_url / node_secret not set")
        return
    interval = max(15, min(45, s.heartbeat_interval_seconds or 30))

    def loop() -> None:
        time.sleep(8)
        while True:
            try:
                _push_logs(s)
            except Exception:  # noqa: BLE001
                logger.exception("log forward cycle failed")
            time.sleep(interval)

    _log_thread = threading.Thread(target=loop, name="cv-log-forwarder", daemon=True)
    _log_thread.start()
    logger.info("node log forwarding started (control plane=%s, every %ds)",
                s.control_plane_url, interval)


def start_replication() -> None:
    """Start the node replication loop (customer-tenant nodes only)."""
    global _thread
    s = get_settings()
    if _thread is not None:
        return
    if not s.control_plane_url or not s.node_secret:
        logger.warning("replication disabled: control_plane_url / node_secret not set")
        return
    # Poll frequently: this channel also delivers portal-initiated work (manual
    # "Back up now" jobs + forwarded agent commands), so responsiveness matters.
    interval = max(15, min(45, s.heartbeat_interval_seconds or 30))

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
