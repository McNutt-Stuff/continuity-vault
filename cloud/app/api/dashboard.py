"""Customer Overview (Home) aggregation.

One call returns everything the landing page shows: the sources we protect (with
their types), a breakdown of the objects we've captured, data protected vs the
licensed allowance, vaults + storage destinations, and the protection/retention
posture. Keeping the aggregation server-side keeps the dashboard a single fast
request instead of half a dozen.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import security
from ..connectors import get_connector
from ..db import get_db
from ..models import (
    ApplianceStorage,
    Collection,
    ConnectorAccount,
    DesktopAgent,
    SearchDocument,
    SnapshotReceipt,
    Tenant,
    Vault,
)

router = APIRouter(prefix="/overview", tags=["overview"])


# Group the connector doc-type taxonomy into the buckets a customer thinks in.
_OBJECT_BUCKETS: list[dict] = [
    {"key": "email", "label": "Emails", "icon": "mail", "color": "#ea4335",
     "types": {"email"}},
    {"key": "credential", "label": "Credentials", "icon": "key", "color": "#0364d3",
     "types": {"secret", "note", "password", "login"}},
    {"key": "document", "label": "Documents", "icon": "file", "color": "#4f7cff",
     "types": {"pdf", "text", "spreadsheet", "presentation", "record", "resume"}},
    {"key": "developer", "label": "Developer", "icon": "code", "color": "#2dbe60",
     "types": {"code", "gist", "repository", "issue", "pull_request", "release"}},
    {"key": "photo", "label": "Photos & images", "icon": "image", "color": "#35d0a5",
     "types": {"image", "photo"}},
    {"key": "media", "label": "Audio & video", "icon": "activity", "color": "#f5a623",
     "types": {"video", "audio"}},    {"key": "message", "label": "Messages & posts", "icon": "mail", "color": "#c56cf0",
     "types": {"message", "post", "comment"}},
    {"key": "event", "label": "Calendar events", "icon": "calendar", "color": "#00b8d9",
     "types": {"event"}},    {"key": "file", "label": "Files & archives", "icon": "database", "color": "#7a5cff",
     "types": {"file", "archive"}},
    {"key": "contact", "label": "Contacts", "icon": "user", "color": "#c56cf0",
     "types": {"contact", "person", "organization", "group", "profile"}},
]
_ICON_MAP = {"folder": "file", "gear": "database"}  # connector icon → available UI icon


def _bucket_for(doc_type: str) -> dict:
    dt = (doc_type or "").lower()
    for b in _OBJECT_BUCKETS:
        if dt in b["types"]:
            return b
    return next(b for b in _OBJECT_BUCKETS if b["key"] == "file")  # default


def _source_meta(source_type: str) -> dict:
    conn = get_connector(source_type)
    if not conn:
        return {"type": source_type, "displayName": source_type,
                "icon": "database", "color": "#7a5cff"}
    spec = conn.oauth_spec()
    return {"type": source_type, "displayName": spec.display_name,
            "icon": _ICON_MAP.get(spec.icon, spec.icon), "color": spec.color}


def _storage_meta(dest: str, stores: dict) -> dict:
    if dest in ("cv-cloud",):
        return {"id": dest, "label": "Arkive Cloud", "kind": "cloud", "icon": "cloud"}
    if dest in ("customer-s3",):
        return {"id": dest, "label": "Your S3 bucket", "kind": "cloud", "icon": "cloud"}
    if dest.startswith("store:"):
        s = stores.get(dest.split(":", 1)[1])
        return {"id": dest, "label": s.name if s else "Appliance storage",
                "kind": "appliance", "icon": "server"}
    if dest == "appliance":
        return {"id": dest, "label": "Offline appliance", "kind": "appliance", "icon": "server"}
    return {"id": dest, "label": dest, "kind": "other", "icon": "database"}


@router.get("")
def overview(scope: str = "me",
             principal: security.Principal = Depends(security.get_principal),
             tenant: Tenant = Depends(security.get_tenant),
             db: Session = Depends(get_db)):
    # Data partitioning: a member sees only their own vaults; an org admin can
    # switch to the whole organization for aggregate statistics.
    vault_ids, eff_scope = security.scoped_vault_ids(db, principal, scope)
    can_switch = security.is_org_admin(principal.role) or principal.is_platform_admin

    collections = (db.query(Collection)
                   .filter(Collection.tenant_id == tenant.id,
                           Collection.vault_id.in_(vault_ids)).all()) if vault_ids else []
    if eff_scope == "org":
        accounts = db.query(ConnectorAccount).filter(
            ConnectorAccount.tenant_id == tenant.id).all()
        agents = db.query(DesktopAgent).filter(
            DesktopAgent.tenant_id == tenant.id,
            DesktopAgent.state != "retired").all()
    else:
        # A member's sources are those mapped into their own vaults.
        acct_ids = {c.connector_account_id for c in collections if c.connector_account_id}
        agent_ids = {c.agent_id for c in collections if c.agent_id}
        accounts = (db.query(ConnectorAccount)
                    .filter(ConnectorAccount.tenant_id == tenant.id,
                            ConnectorAccount.id.in_(acct_ids)).all()) if acct_ids else []
        agents = (db.query(DesktopAgent)
                  .filter(DesktopAgent.tenant_id == tenant.id,
                          DesktopAgent.id.in_(agent_ids)).all()) if agent_ids else []
    vaults = (db.query(Vault).filter(Vault.tenant_id == tenant.id,
                                     Vault.id.in_(vault_ids)).all()) if vault_ids else []

    # --- Protected sources + the mix of source types we cover ----------------
    type_counts: dict[str, int] = {}
    for c in collections:
        type_counts[c.source_type] = type_counts.get(c.source_type, 0) + 1
    if not type_counts:  # nothing mapped yet — seed from linked accounts/agents
        for a in accounts:
            type_counts[a.connector_type] = type_counts.get(a.connector_type, 0) + 1
        for ag in agents:
            for col in (ag.collectors or []):
                type_counts[col] = type_counts.get(col, 0) + 1
    source_types = [{**_source_meta(t), "count": n}
                    for t, n in sorted(type_counts.items(), key=lambda kv: -kv[1])]

    # --- Objects protected (deduped per logical object) + type breakdown -----
    docs = (db.query(SearchDocument)
            .filter(SearchDocument.tenant_id == tenant.id,
                    SearchDocument.vault_id.in_(vault_ids))
            .order_by(SearchDocument.created_at.desc()).all()) if vault_ids else []
    seen: set[tuple] = set()
    bucket_counts: dict[str, int] = {}
    source_obj_counts: dict[str, int] = {}
    protected_bytes = 0
    object_total = 0
    for d in docs:
        key = (d.source_type, d.object_id)
        if key in seen:
            continue
        seen.add(key)
        object_total += 1
        protected_bytes += int(d.size_bytes or 0)
        bucket_counts[_bucket_for(d.doc_type)["key"]] = \
            bucket_counts.get(_bucket_for(d.doc_type)["key"], 0) + 1
        source_obj_counts[d.source_type] = source_obj_counts.get(d.source_type, 0) + 1
    object_breakdown = [
        {"key": b["key"], "label": b["label"], "icon": b["icon"], "color": b["color"],
         "count": bucket_counts.get(b["key"], 0)}
        for b in _OBJECT_BUCKETS if bucket_counts.get(b["key"], 0) > 0
    ]
    # Objects grouped by source type (Gmail, iCloud, 1Password…), combining
    # multiple accounts of the same type — powers the overview pie chart.
    object_by_source = []
    for st, n in sorted(source_obj_counts.items(), key=lambda kv: -kv[1]):
        m = _source_meta(st)
        object_by_source.append({"key": st, "label": m["displayName"],
                                 "icon": m["icon"], "color": m["color"], "count": n})

    # --- Data protected vs licensed allowance --------------------------------
    licensed = int(tenant.licensed_bytes or 0)
    percent = round(min(100.0, protected_bytes / licensed * 100), 1) if licensed else None

    # --- Vaults + storage destinations ---------------------------------------
    stores = {s.id: s for s in db.query(ApplianceStorage)
              .filter(ApplianceStorage.tenant_id == tenant.id).all()}
    dest_ids: list[str] = []
    for c in collections:
        for d in (c.destinations or ["cv-cloud"]):
            if d not in dest_ids:
                dest_ids.append(d)
    if not dest_ids:
        dest_ids = ["cv-cloud"]
    destinations = [_storage_meta(d, stores) for d in dest_ids]

    # --- Data stored at each location + how far back the history reaches ------
    # Bytes actually written (cumulative across recovery points), grouped into
    # the three places data can live. Customer cloud is shown only when used.
    usage = {"cloud": 0, "appliance": 0, "customer": 0}
    receipts = (db.query(SnapshotReceipt)
                .filter(SnapshotReceipt.tenant_id == tenant.id,
                        SnapshotReceipt.vault_id.in_(vault_ids)).all()) if vault_ids else []
    for r in receipts:
        b = int(r.total_bytes or 0)
        dest = r.destination or ""
        if dest == "customer-s3":
            usage["customer"] += b
        elif dest == "cv-cloud":
            usage["cloud"] += b
        else:  # store:<id> / appliance / appliance:<id>
            usage["appliance"] += b
    # Oldest protected item by the CONTENT's own timestamp (file/email date),
    # not when we ingested it.
    oldest = (db.query(func.min(SearchDocument.modified_at))
              .filter(SearchDocument.tenant_id == tenant.id,
                      SearchDocument.vault_id.in_(vault_ids),
                      SearchDocument.modified_at.isnot(None)).scalar()) if vault_ids else None

    # --- Pending Arkive Cloud deletion (30-day grace after unsubscribe) -------
    from .billing import cloud_delete_target, cloud_stored_summary
    from ..models import User as _User
    _user = db.get(_User, principal.user_id)
    _target = cloud_delete_target(_user, tenant)
    cloud_deletion = None
    if _target is not None and getattr(_target, "cloud_delete_at", None):
        from datetime import datetime as _dt
        delete_at = _target.cloud_delete_at
        secs = (delete_at - _dt.utcnow()).total_seconds()
        cd_count, cd_bytes = cloud_stored_summary(db, tenant, vault_ids)
        cloud_deletion = {
            "pending": True,
            "delete_at": delete_at.isoformat(),
            "days_left": max(0, int(secs // 86400) + (1 if secs % 86400 else 0)),
            "object_count": cd_count,
            "bytes": cd_bytes,
        }

    return {
        "sources": {"count": sum(type_counts.values()), "types": source_types},
        "objects": {"total": object_total, "breakdown": object_breakdown,
                    "by_source": object_by_source},
        "data": {"protected_bytes": protected_bytes, "licensed_bytes": licensed,
                 "percent": percent},
        "storage": {"vault_count": len(vaults), "destinations": destinations,
                    "usage": usage,
                    "oldest_content_at": oldest.isoformat() if oldest else None},
        "protection": {
            "key_ownership_model": tenant.key_ownership_model,
            "encrypted": True,
        },
        "connector_health": {
            "issues": sum(1 for a in accounts if a.last_error or a.auth_status == "needs-reauth"),
            "needs_reauth": sum(1 for a in accounts if a.auth_status == "needs-reauth"),
        },
        "scope": eff_scope,
        "can_switch_scope": can_switch,
        "cloud_deletion": cloud_deletion,
    }


# period -> (number of buckets, size of each bucket). "all" is handled dynamically.
_PERIODS = {
    "week": (7, timedelta(days=1)),
    "month": (30, timedelta(days=1)),
    "quarter": (13, timedelta(weeks=1)),
    "year": (12, timedelta(days=30)),
}


def _bucket_edges(period: str, now: datetime, earliest: datetime) -> list[datetime]:
    """End-of-bucket cutoff datetimes spanning the requested period, oldest→newest."""
    if period == "all":
        span = (now - earliest) or timedelta(days=1)
        step = span / 12
        return [earliest + step * (i + 1) for i in range(12)]
    n, step = _PERIODS.get(period, _PERIODS["week"])
    start = now - step * (n - 1)
    return [start + step * i for i in range(n)]


@router.get("/trends")
def trends(period: str = "week", scope: str = "me",
           principal: security.Principal = Depends(security.get_principal),
           tenant: Tenant = Depends(security.get_tenant),
           db: Session = Depends(get_db)):
    """Cumulative count of protected objects over time, per object type, so the
    dashboard can draw a growth trend line per category over a rolling window."""
    vault_ids, _eff = security.scoped_vault_ids(db, principal, scope)
    docs = (db.query(SearchDocument.source_type, SearchDocument.object_id,
                     SearchDocument.doc_type, SearchDocument.created_at)
            .filter(SearchDocument.tenant_id == tenant.id,
                    SearchDocument.vault_id.in_(vault_ids))
            .order_by(SearchDocument.created_at.asc()).all()) if vault_ids else []
    # First time each logical object was protected + its bucket.
    first: dict[tuple, tuple] = {}
    earliest = None
    for st, oid, doc_type, created in docs:
        key = (st, oid)
        if key in first or created is None:
            continue
        first[key] = (created, _bucket_for(doc_type)["key"])
        earliest = created if earliest is None else min(earliest, created)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if earliest is None:
        earliest = now - timedelta(days=7)
    edges = _bucket_edges(period if period in _PERIODS or period == "all" else "week", now, earliest)

    counts: dict[str, list[int]] = {}
    for created, bk in first.values():
        row = counts.setdefault(bk, [0] * len(edges))
        for i, edge in enumerate(edges):
            if created <= edge:
                row[i] += 1  # cumulative — present at/after first ingest
    series = []
    for b in _OBJECT_BUCKETS:
        vals = counts.get(b["key"])
        if not vals or vals[-1] == 0:
            continue
        series.append({"key": b["key"], "label": b["label"], "icon": b["icon"],
                       "color": b["color"], "values": vals, "current": vals[-1]})
    series.sort(key=lambda s: -s["current"])
    return {
        "period": period,
        "points": [e.isoformat() for e in edges],
        "series": series,
    }
