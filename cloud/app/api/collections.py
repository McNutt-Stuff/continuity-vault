"""Collections + backup execution (sync worker trigger)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import audit, security
from ..config import get_settings
from ..db import get_db
from ..connectors import get_connector
from ..models import (
    Collection,
    ConnectorAccount,
    DesktopAgent,
    IntegrationInstance,
    SearchDocument,
    SnapshotReceipt,
    Tenant,
    Vault,
)
from ..workers.jobs import start_backup_job

router = APIRouter(prefix="/collections", tags=["collections"])
logger = logging.getLogger("cv.collections")

# Integrations whose per-source collections are governed as ONE Data Map entry
# (admin-only): the individual managed sources are never shown as standalone rows.
_MANAGED_INTEGRATIONS = {
    "microsoft365": "Microsoft 365",
    "google_workspace": "Google Workspace",
}

# Friendly labels for the managed workloads folded under an integration entry.
_WORKLOAD_LABEL = {
    "exchange": "Exchange", "onedrive": "OneDrive", "sharepoint": "SharePoint",
    "teams": "Teams", "teams_chat": "Teams chats", "calendar": "Calendar",
    "contacts": "Contacts", "onenote": "OneNote", "copilot": "Copilot",
    "gmail": "Gmail", "google_drive": "Drive", "google_calendar": "Calendar",
    "google_contacts": "Contacts",
}


def _managed_instance_id(c: Collection) -> str | None:
    """The integration instance a managed collection belongs to (M365 or Google
    Workspace), or None for a normal source."""
    cfg = c.config or {}
    if not cfg.get("managed"):
        return None
    return cfg.get("m365_instance_id") or cfg.get("gw_instance_id")


def _managed_workload(c: Collection) -> str | None:
    cfg = c.config or {}
    return cfg.get("m365_workload") or cfg.get("gw_workload")


class CreateCollectionRequest(BaseModel):
    vault_id: str
    name: str
    source_type: str
    connector_account_id: str | None = None
    agent_id: str | None = None  # bind an agent-collected source to a device
    sensitivity: str = "standard"
    destinations: list[str] = ["cv-cloud"]
    config: dict | None = None  # source-specific settings (e.g. endpoint-files selection)


def _dest_tier(dest: str) -> str:
    """Map a storage-target id to its Protection Setup tier."""
    if dest == "cv-cloud":
        return "cv-cloud"
    if dest == "customer-s3":
        return "customer-cloud"
    return "appliance"  # store:<id> / appliance*


def _require_protection(db, principal, tenant, dests: list[str] | None) -> None:
    """Gate mapping on Protection Setup: a destination can only be used if its tier
    is enabled there. Nothing enabled ⇒ the customer must choose one first."""
    from ..models import User
    from .billing import user_protection_options
    enabled = set(user_protection_options(db.get(User, principal.user_id), tenant))
    if not enabled:
        raise HTTPException(400, "Choose a protection destination in Protection Setup "
                                 "before mapping any data.")
    bad = [d for d in (dests or []) if _dest_tier(d) not in enabled]
    if bad:
        raise HTTPException(400, "That storage destination isn't enabled in Protection Setup.")


@router.post("")
def create_collection(body: CreateCollectionRequest,
                      principal: security.Principal = Depends(security.get_principal),
                      tenant: Tenant = Depends(security.get_tenant),
                      db: Session = Depends(get_db)):
    vault = db.get(Vault, body.vault_id)
    if not vault or vault.tenant_id != tenant.id:
        raise HTTPException(404, "vault not found")
    # A member can only map sources into a vault they own.
    if vault.owner_user_id and vault.owner_user_id != principal.user_id \
            and not security.is_org_admin(principal.role):
        raise HTTPException(403, "you can only add sources to your own vault")
    # Agent-collected sources are unique per (agent, source_type): if one already
    # exists, return it instead of creating a duplicate. This is what stops the
    # agent's push from ever spawning a second entry.
    if body.agent_id:
        existing = (db.query(Collection)
                    .filter(Collection.tenant_id == tenant.id,
                            Collection.agent_id == body.agent_id,
                            Collection.source_type == body.source_type).first())
        if existing:
            return _collection_view(db, existing)
    _require_protection(db, principal, tenant, body.destinations or ["cv-cloud"])
    coll = Collection(
        tenant_id=tenant.id,
        vault_id=vault.id,
        name=body.name,
        source_type=body.source_type,
        connector_account_id=body.connector_account_id,
        agent_id=body.agent_id,
        sensitivity=body.sensitivity,
        destinations=body.destinations or ["cv-cloud"],
        config=body.config or {},
    )
    db.add(coll)
    db.commit()
    db.refresh(coll)
    audit.record(db, actor=principal.user_id, action="collection.created",
                 tenant_id=tenant.id, resource=coll.id)
    return _collection_view(db, coll)


class UpdateCollectionRequest(BaseModel):
    name: str | None = None
    vault_id: str | None = None
    sensitivity: str | None = None
    destinations: list[str] | None = None
    index_fields: list[str] | None = None
    backup_interval_minutes: int | None = None  # NULL=default, 0=manual, >0=every N min
    config: dict | None = None


@router.put("/{collection_id}")
def update_collection(collection_id: str, body: UpdateCollectionRequest,
                      principal: security.Principal = Depends(security.get_principal),
                      tenant: Tenant = Depends(security.get_tenant),
                      db: Session = Depends(get_db)):
    coll = db.get(Collection, collection_id)
    if not coll or coll.tenant_id != tenant.id:
        raise HTTPException(404, "collection not found")
    if body.vault_id is not None:
        vault = db.get(Vault, body.vault_id)
        if not vault or vault.tenant_id != tenant.id:
            raise HTTPException(404, "vault not found")
        coll.vault_id = body.vault_id
    if body.name is not None:
        coll.name = body.name
    if body.sensitivity is not None:
        coll.sensitivity = body.sensitivity
    if body.destinations is not None:
        _require_protection(db, principal, tenant, body.destinations or ["cv-cloud"])
        coll.destinations = body.destinations or ["cv-cloud"]
    if body.index_fields is not None:
        coll.index_fields = body.index_fields
    if body.backup_interval_minutes is not None:
        # <0 → NULL (use the global default); 0 → manual only; >0 → every N min.
        coll.backup_interval_minutes = (None if body.backup_interval_minutes < 0
                                        else body.backup_interval_minutes)
    if body.config is not None:
        # Changing the crawl scope (folder selection) or the "back up from" date
        # must restart the crawl — drop the stored cursor so the next run re-scans
        # with the new scope instead of delta-continuing the old (wider) one.
        prev_since = (coll.config or {}).get("sinceDate") or ""
        new_since = (body.config or {}).get("sinceDate") or ""
        prev_roots = sorted((coll.config or {}).get("roots") or [])
        new_roots = sorted((body.config or {}).get("roots") or [])
        coll.config = body.config
        if (new_since != prev_since or new_roots != prev_roots) and coll.connector_account_id:
            acct = db.get(ConnectorAccount, coll.connector_account_id)
            if acct is not None:
                acct.sync_cursor = None
    db.commit()
    db.refresh(coll)
    audit.record(db, actor=principal.user_id, action="collection.updated",
                 tenant_id=tenant.id, resource=coll.id,
                 detail={"destinations": coll.destinations,
                         "index_fields": coll.index_fields})
    return _collection_view(db, coll)


def _available_fields(source_type: str) -> list[str]:
    conn = get_connector(source_type)
    if not conn:
        return []
    caps = conn.capabilities()
    out: list[str] = []
    for k in [*(caps.facet_fields or []), *(caps.searchable_fields or [])]:
        if k and k != "*" and k not in out:
            out.append(k)
    return out


def _collection_view(db: Session, c: Collection) -> dict:
    vault = db.get(Vault, c.vault_id)
    account = db.get(ConnectorAccount, c.connector_account_id) if c.connector_account_id else None
    agent = db.get(DesktopAgent, c.agent_id) if c.agent_id else None
    conn = get_connector(c.source_type)
    available = _available_fields(c.source_type)
    # Last backup status across this mapping's snapshots.
    last = (db.query(SnapshotReceipt)
            .filter(SnapshotReceipt.collection_id == c.id)
            .order_by(SnapshotReceipt.created_at.desc()).first())
    is_agent = bool(conn and conn.capabilities().requires_agent)
    agent_label = (agent.hostname or agent.name) if agent else None
    # Total DISTINCT objects indexed for this source (what unified search shows),
    # not the last batch — the agent pushes in batches, so the last receipt's
    # count is only a slice of the whole.
    indexed = (db.query(SearchDocument.object_id)
               .filter(SearchDocument.collection_id == c.id)
               .distinct().count())
    # Recovery points stored at destinations no longer in this mapping's routing
    # (e.g. old cloud copies after the mapping was switched to an appliance).
    keep = set(c.destinations or ["cv-cloud"])
    offpolicy_points = (db.query(SnapshotReceipt)
                        .filter(SnapshotReceipt.collection_id == c.id,
                                SnapshotReceipt.destination.notin_(list(keep)))
                        .count())
    return {
        "id": c.id, "name": c.name, "source_type": c.source_type,
        "source_display": conn.display_name if conn else c.source_type,
        "source_label": account.account_label if account else (agent_label or c.name),
        "is_agent": is_agent,
        "agent_id": c.agent_id,
        "agent_label": agent_label,
        "vault_id": c.vault_id, "vault_name": vault.name if vault else None,
        "connector_account_id": c.connector_account_id,
        "account_label": account.account_label if account else None,
        "account_username": account.account_username if account else None,
        # Last sync health (from the connector account) so the Data Map can show
        # failures inline, not just successful recovery points.
        "last_error": account.last_error if account else None,
        "last_error_at": (account.last_error_at.isoformat()
                          if account and account.last_error_at else None),
        "fail_count": int(account.fail_count or 0) if account else 0,
        "needs_reauth": bool(account and account.auth_status == "needs-reauth"),
        "sensitivity": c.sensitivity,
        "destinations": c.destinations or ["cv-cloud"],
        "index_fields": list(c.index_fields or []),
        "available_fields": available,
        "last_backup_at": last.created_at.isoformat() if last else None,
        "last_object_count": indexed,
        "last_recoverable": bool(last.recoverable) if last else False,
        "offpolicy_points": offpolicy_points,
        "backup_interval_minutes": c.backup_interval_minutes,  # NULL = use default
        "default_interval_minutes": get_settings().sync_interval_minutes,
        "last_backup_run_at": c.last_backup_run_at.isoformat() if c.last_backup_run_at else None,
        "config": c.config or {},
        # Big-history sources support a "back up from this date" window; crawling
        # runs in resumable chunks (Google Photos, etc.).
        "supports_since": bool(conn and conn.capabilities().historical),
        "since_date": (c.config or {}).get("sinceDate") or "",
        # Picker sources are imported interactively (user picks items each session);
        # a reminder nudges them on a cadence.
        "is_picker": bool(conn and conn.capabilities().picker),
        "reminder_days": int((c.config or {}).get("reminderDays") or 3),
        # Deep-history backfill status (dual-track sources opted in by an admin):
        # its own paced background crawl, separate from the recent/scheduled sync.
        "backfill": _backfill_view(c, conn, account),
    }


def _backfill_view(c: Collection, conn, account) -> dict | None:
    from .. import platform_config
    caps = conn.capabilities() if conn else None
    if not (caps and caps.dual_track and platform_config.source_backfill_enabled(c.source_type)):
        return None
    return {
        "enabled": True,
        "done": bool(account.backfill_done) if account else False,
        "count": int(account.backfill_count or 0) if account else 0,
        "started_at": (account.backfill_started_at.isoformat()
                       if account and account.backfill_started_at else None),
        "completed_at": (account.backfill_completed_at.isoformat()
                         if account and account.backfill_completed_at else None),
    }


@router.get("")
def list_collections(principal: security.Principal = Depends(security.get_principal),
                     tenant: Tenant = Depends(security.get_tenant),
                     db: Session = Depends(get_db)):
    # Data partitioning: a member only ever sees mappings in vaults they own.
    allowed = security.content_vault_ids(db, principal)
    # Stable order (oldest first) so the Data Map list never reshuffles on edits.
    colls = (db.query(Collection)
             .filter(Collection.tenant_id == tenant.id,
                     Collection.vault_id.in_(allowed))
             .order_by(Collection.created_at.asc(), Collection.id.asc()).all()) if allowed else []
    # Managed integration sources (Microsoft 365 / Google Workspace) are governed by
    # the integration, not shown as individual Data Map rows — and never shown to
    # standard members, even for their own account's data. An org admin sees ONE
    # grouped entry per integration instead (added below).
    views = [_collection_view(db, c) for c in colls if not (c.config or {}).get("managed")]
    if security.is_org_admin(principal.role):
        views.extend(_integration_mapping_views(db, tenant))
    return views


def _integration_mapping_views(db: Session, tenant: Tenant) -> list[dict]:
    """One grouped Data Map entry per managed integration instance (all of its
    per-user/per-site managed collections collapsed into a single row)."""
    managed = [c for c in db.query(Collection)
               .filter(Collection.tenant_id == tenant.id).all()
               if (c.config or {}).get("managed")]
    by_inst: dict[str, list[Collection]] = {}
    for c in managed:
        iid = _managed_instance_id(c)
        if iid:
            by_inst.setdefault(iid, []).append(c)
    out: list[dict] = []
    for iid, children in by_inst.items():
        inst = db.get(IntegrationInstance, iid)
        if inst is None or inst.integration_type not in _MANAGED_INTEGRATIONS:
            continue
        out.append(_integration_mapping_view(db, inst, children))
    # Stable order by integration label.
    out.sort(key=lambda v: v.get("name") or "")
    return out


def _integration_mapping_view(db: Session, inst: IntegrationInstance,
                              children: list[Collection]) -> dict:
    cfg = inst.config or {}
    prof = cfg.get("managed_profile") or {}
    child_ids = [c.id for c in children]
    objects = (db.query(SearchDocument.object_id)
               .filter(SearchDocument.collection_id.in_(child_ids))
               .distinct().count()) if child_ids else 0
    last = (db.query(SnapshotReceipt)
            .filter(SnapshotReceipt.collection_id.in_(child_ids))
            .order_by(SnapshotReceipt.created_at.desc()).first()) if child_ids else None
    # Distinct workloads present under this integration (Exchange, OneDrive, …).
    workloads: list[dict] = []
    seen: set[str] = set()
    for c in children:
        w = _managed_workload(c)
        if w and w not in seen:
            seen.add(w)
            workloads.append({"id": w, "label": _WORKLOAD_LABEL.get(w, w)})
    workloads.sort(key=lambda x: x["label"])
    # Routing/schedule come from the integration's authoritative managed profile;
    # editing the entry writes back to it and cascades to every child.
    union: list[str] = []
    for c in children:
        for d in (c.destinations or []):
            if d not in union:
                union.append(d)
    dests = list(prof.get("destinations") or union or ["cv-cloud"])
    label = _MANAGED_INTEGRATIONS.get(inst.integration_type, inst.integration_type)
    name = inst.label or label
    return {
        "id": f"integration:{inst.id}",
        "integration": True,
        "integration_type": inst.integration_type,
        "integration_label": label,
        "instance_id": inst.id,
        "name": name,
        "source_type": inst.integration_type,
        "source_display": label,
        "workloads": workloads,
        "child_count": len(children),
        "destinations": dests,
        "backup_interval_minutes": prof.get("backup_interval_minutes"),
        "default_interval_minutes": get_settings().sync_interval_minutes,
        "last_backup_at": last.created_at.isoformat() if last else None,
        "last_object_count": objects,
        "last_recoverable": bool(last.recoverable) if last else False,
    }


class UpdateIntegrationMappingRequest(BaseModel):
    destinations: list[str] | None = None
    backup_interval_minutes: int | None = None  # NULL/<0 = default, 0 = manual, >0 = every N min


@router.put("/integration/{instance_id}")
def update_integration_mapping(instance_id: str, body: UpdateIntegrationMappingRequest,
                               principal: security.Principal = Depends(security.get_principal),
                               tenant: Tenant = Depends(security.get_tenant),
                               db: Session = Depends(get_db)):
    """Edit the single Data Map entry for a managed integration (Microsoft 365 /
    Google Workspace). Org-admin only. The routing + schedule set here are written
    to the integration's managed profile and cascade to EVERY managed source it
    protects (per-source logic is handled by Rules, not the Data Map)."""
    if not security.is_org_admin(principal.role):
        raise HTTPException(403, "Only an organization admin can edit a managed integration mapping.")
    inst = db.get(IntegrationInstance, instance_id)
    if not inst or inst.tenant_id != tenant.id \
            or inst.integration_type not in _MANAGED_INTEGRATIONS:
        raise HTTPException(404, "integration not found")
    cfg = dict(inst.config or {})
    prof = dict(cfg.get("managed_profile") or {})
    if body.destinations is not None:
        dests = [str(d) for d in body.destinations if d] or ["cv-cloud"]
        _require_protection(db, principal, tenant, dests)
        prof["destinations"] = dests
    if body.backup_interval_minutes is not None:
        prof["backup_interval_minutes"] = (None if body.backup_interval_minutes < 0
                                           else int(body.backup_interval_minutes))
    cfg["managed_profile"] = prof
    inst.config = cfg
    # Cascade to every managed child collection so the change is immediate here;
    # the integration's provisioner re-syncs children from the profile too (which is
    # what makes it durable on node-hosted tenants that reconcile from the CP).
    children = [c for c in db.query(Collection)
                .filter(Collection.tenant_id == tenant.id).all()
                if _managed_instance_id(c) == inst.id]
    for c in children:
        if body.destinations is not None:
            c.destinations = list(prof.get("destinations") or ["cv-cloud"])
        if body.backup_interval_minutes is not None:
            c.backup_interval_minutes = prof.get("backup_interval_minutes")
    db.commit()
    _reprovision_integration(db, inst)
    audit.record(db, actor=principal.user_id, action="integration.mapping_updated",
                 category="admin", tenant_id=tenant.id, resource=inst.id,
                 detail={"integration": inst.integration_type,
                         "destinations": prof.get("destinations"),
                         "backup_interval_minutes": prof.get("backup_interval_minutes"),
                         "children": len(children)})
    logger.info("integration mapping updated (instance=%s type=%s): destinations=%s interval=%s children=%d",
                inst.id, inst.integration_type, prof.get("destinations"),
                prof.get("backup_interval_minutes"), len(children))
    return _integration_mapping_view(db, inst, children)


def _reprovision_integration(db: Session, inst: IntegrationInstance) -> None:
    """Apply a managed-profile change to the integration's sources now. On a
    node-hosted tenant the node reconciles from the replicated profile (bump its
    desired state); on the control plane, re-provision inline. Best-effort — the
    child cascade above already applied the routing/schedule locally."""
    t = db.get(Tenant, inst.tenant_id)
    node_hosted = bool(t and t.node_id)
    try:
        if inst.integration_type == "microsoft365":
            from ..integrations.microsoft365 import api as m365api, collect as m365collect
            if node_hosted:
                m365api._bump_desired(db, inst)
                db.commit()
            else:
                m365collect.provision_sources(db, inst)
        elif inst.integration_type == "google_workspace":
            from ..integrations.google_workspace import collect as gwcollect
            if not node_hosted:
                gwcollect.provision_sources(db, inst)
    except Exception:  # noqa: BLE001 — routing already cascaded; provisioning is a follow-up
        logger.exception("reprovision after integration mapping update failed (instance=%s)", inst.id)



@router.delete("/{collection_id}")
def delete_collection(collection_id: str,
                      principal: security.Principal = Depends(security.get_principal),
                      tenant: Tenant = Depends(security.get_tenant),
                      db: Session = Depends(get_db)):
    c = db.get(Collection, collection_id)
    if not c or c.tenant_id != tenant.id:
        raise HTTPException(404, "collection not found")
    # Remove dependent rows first — snapshot receipts and search-index entries
    # reference this collection (no DB cascade), so deleting the mapping while it
    # still has backup history would otherwise fail with a foreign-key error.
    db.query(SearchDocument).filter(SearchDocument.collection_id == collection_id).delete()
    db.query(SnapshotReceipt).filter(SnapshotReceipt.collection_id == collection_id).delete()
    db.delete(c)
    db.commit()
    audit.record(db, actor=principal.user_id, action="collection.deleted",
                 tenant_id=tenant.id, resource=collection_id)
    return {"ok": True}


@router.get("/{collection_id}/purge-targets")
def collection_purge_targets(collection_id: str,
                             principal: security.Principal = Depends(security.get_principal),
                             tenant: Tenant = Depends(security.get_tenant),
                             db: Session = Depends(get_db)):
    """Destinations an agent-collected source's data is stored at, so the customer
    can choose which storage locations to purge from (parity with connector
    sources). Also reports whether a data map still routes it (re-accrues)."""
    from .search import _location_label, _store_label_map
    c = db.get(Collection, collection_id)
    if not c or c.tenant_id != tenant.id:
        raise HTTPException(404, "collection not found")
    store_labels = _store_label_map(db, tenant.id)
    agg: dict[str, dict] = {}
    for rc in (db.query(SnapshotReceipt)
               .filter(SnapshotReceipt.collection_id == c.id).all()):
        d = agg.setdefault(rc.destination, {"id": rc.destination,
                           "label": _location_label(rc.destination, store_labels),
                           "recovery_points": 0, "bytes": 0})
        d["recovery_points"] += 1
        d["bytes"] += int(rc.total_bytes or 0)
    return {"active": True,
            "destinations": sorted(agg.values(), key=lambda x: x["label"])}


class CollectionPurgeBody(BaseModel):
    destinations: list[str] | None = None  # subset of destination ids, or None/["all"] = everywhere
    execute_now: bool = False
    keep_source: bool = False  # purge the data but keep the agent source collecting


@router.post("/{collection_id}/purge")
def collection_purge(collection_id: str, body: CollectionPurgeBody = CollectionPurgeBody(),
                     principal: security.Principal = Depends(security.get_principal),
                     tenant: Tenant = Depends(security.get_tenant),
                     db: Session = Depends(get_db)):
    """Schedule an irreversible purge of an agent-collected source's data,
    optionally only from chosen destinations, keeping or removing the source.
    Runs after a 24h grace window unless execute_now. Requires the
    ``purge_enabled`` capability (an admin clears it for a legal hold). The
    destinations are wiped on the tenant's node AND the control plane."""
    from datetime import timedelta
    from .. import features
    from ..models import PurgeRequest, User
    from ..api.connectors import _execute_purge, _purge_request_view, PURGE_GRACE_HOURS, _now_naive
    c = db.get(Collection, collection_id)
    if not c or c.tenant_id != tenant.id:
        raise HTTPException(404, "collection not found")
    user = db.get(User, principal.user_id)
    if not features.resolve(user, tenant, "purge_enabled"):
        raise HTTPException(403, "Data purge is disabled for this account (legal hold).")
    existing = db.query(PurgeRequest).filter(
        PurgeRequest.collection_id == collection_id,
        PurgeRequest.status.in_(["scheduled", "running"])).first()
    if existing:
        raise HTTPException(409, "A purge is already scheduled for this source.")
    dests = body.destinations
    all_mode = (not dests) or ("all" in dests)
    grace = timedelta(0) if body.execute_now else timedelta(hours=PURGE_GRACE_HOURS)
    req = PurgeRequest(
        tenant_id=tenant.id, owner_user_id=principal.user_id,
        connector_account_id=None, collection_id=collection_id,
        source_type=c.source_type, source_label=c.name, all_destinations=all_mode,
        destinations=[] if all_mode else list(dests),
        keep_source=bool(body.keep_source),
        data_map_active=True,
        created_by=principal.user_id, execute_at=_now_naive() + grace,
        status="scheduled",
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    audit.record(db, actor=principal.user_id, action="connector.purge_scheduled",
                 tenant_id=tenant.id, resource=collection_id, category="security",
                 severity="warning",
                 detail={"label": c.name, "agent_source": True,
                         "execute_now": body.execute_now,
                         "destinations": "all" if all_mode else dests})
    if body.execute_now:
        req.status = "running"
        db.commit()
        counts = _execute_purge(db, req)
        return {"ok": True, "executed": True, "request": _purge_request_view(db, req), **counts}
    return {"ok": True, "executed": False, "request": _purge_request_view(db, req)}


@router.post("/{collection_id}/prune")
def prune_collection(collection_id: str,
                     principal: security.Principal = Depends(security.get_principal),
                     tenant: Tenant = Depends(security.get_tenant),
                     db: Session = Depends(get_db)):
    """Prune off-policy recovery points: delete snapshot receipts stored at
    destinations that are no longer part of this mapping's routing. Removes them
    from search locations so recovery only draws from the current destinations.
    Immutable object-store bytes age out under retention; this drops the pointers.
    """
    c = db.get(Collection, collection_id)
    if not c or c.tenant_id != tenant.id:
        raise HTTPException(404, "collection not found")
    keep = set(c.destinations or ["cv-cloud"])
    stale = (db.query(SnapshotReceipt)
             .filter(SnapshotReceipt.collection_id == c.id,
                     SnapshotReceipt.tenant_id == tenant.id,
                     SnapshotReceipt.destination.notin_(list(keep)))
             .all())
    from .search import _location_label, _store_label_map
    store_labels = _store_label_map(db, tenant.id)
    dests = sorted({r.destination for r in stale})
    for r in stale:
        db.delete(r)
    db.commit()
    audit.record(db, actor=principal.user_id, action="collection.pruned",
                 tenant_id=tenant.id, resource=collection_id,
                 detail={"pruned": len(stale), "destinations": dests})
    return {"pruned": len(stale),
            "destinations": [_location_label(d, store_labels) for d in dests]}


class BackupRequest(BaseModel):
    destinations: list[str] | None = None  # falls back to the mapping's destinations


@router.post("/{collection_id}/backup")
def backup(collection_id: str, body: BackupRequest,
           principal: security.Principal = Depends(security.get_principal),
           tenant: Tenant = Depends(security.get_tenant),
           db: Session = Depends(get_db)):
    coll = db.get(Collection, collection_id)
    if not coll or coll.tenant_id != tenant.id:
        raise HTTPException(404, "collection not found")
    dests = body.destinations or coll.destinations or ["cv-cloud"]
    # Long pulls run as a tracked background job so the UI can show progress.
    job = start_backup_job(db, tenant.id, coll.id, kind="backup", destinations=dests)
    coll.last_backup_run_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    audit.record(db, actor=principal.user_id, action="source.sync_requested",
                 tenant_id=tenant.id, resource=coll.id, detail={"kind": "connector"})
    return {"job_id": job.id, "status": job.status, "kind": "connector",
            "destinations": dests}


@router.post("/{collection_id}/sync")
def sync(collection_id: str,
         principal: security.Principal = Depends(security.get_principal),
         tenant: Tenant = Depends(security.get_tenant),
         db: Session = Depends(get_db)):
    """Trigger this source's natural sync, routing through the mapping.

    - Connector (cloud-pull) sources run a backup immediately.
    - Agent-collected sources (e.g. 1Password) queue a `collect` command to the
      matching desktop agent(s); the agent then collects locally and pushes
      through the same pipeline into the mapping's destinations.
    """
    coll = db.get(Collection, collection_id)
    if not coll or coll.tenant_id != tenant.id:
        raise HTTPException(404, "collection not found")

    conn = get_connector(coll.source_type)
    is_agent = bool(conn and conn.capabilities().requires_agent)

    if is_agent:
        agents_q = db.query(DesktopAgent).filter(DesktopAgent.tenant_id == tenant.id)
        # Prefer the agent this source is bound to; otherwise any agent that can
        # collect this source type.
        if coll.agent_id:
            targeted = [a for a in agents_q.filter(DesktopAgent.id == coll.agent_id).all()]
        else:
            targeted = [a for a in agents_q.all()
                        if coll.source_type in (a.collectors or [])]
        queued = 0
        for a in targeted:
            a.enqueue_command({"type": "collect",
                               "params": {"source_type": coll.source_type,
                                          "file_config": coll.config or {}}})
            queued += 1
        coll.last_backup_run_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()
        audit.record(db, actor=principal.user_id, action="source.sync_requested",
                     tenant_id=tenant.id, resource=coll.id,
                     detail={"kind": "agent", "agents": queued})
        if queued == 0:
            raise HTTPException(409, "the desktop agent for this source is not available")
        return {"kind": "agent", "queued_agents": queued,
                "message": f"Queued collection on {queued} agent(s); data will arrive shortly."}

    dests = coll.destinations or ["cv-cloud"]
    # Connector pull runs as a tracked background job (progress in Activity).
    job = start_backup_job(db, tenant.id, coll.id, kind="sync", destinations=dests)
    coll.last_backup_run_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    audit.record(db, actor=principal.user_id, action="source.sync_requested",
                 tenant_id=tenant.id, resource=coll.id, detail={"kind": "connector"})
    return {"kind": "connector", "job_id": job.id, "status": job.status,
            "destinations": dests}
