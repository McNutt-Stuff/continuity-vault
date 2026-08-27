"""Unified search across all data types, accounts, and objects (spec 15 / user
requirement). Passkey step-up is required because results expose object metadata
for the user's protected data."""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from cv_crypto.envelope import EnvelopeKeyHierarchy, decrypt_object, unwrap_key
from cv_crypto.provider import get_provider

from .. import audit, fleet, keybroker, security
from ..db import get_db
from ..connectors import get_connector
from ..models import (
    Appliance,
    ApplianceCommand,
    ApplianceStorage,
    Collection,
    ConnectorAccount,
    DesktopAgent,
    ObjectVersion,
    SearchDocument,
    SnapshotReceipt,
    Tenant,
    Vault,
)
from ..storage import build_destination
from ..taxonomy import category_for_kind, describe, sensitivity_for

router = APIRouter(prefix="/search", tags=["search"])
logger = logging.getLogger("cv.search")


def _unb64(s: str) -> bytes:
    return base64.b64decode(s)


def _unwrap_agent_content(db: Session, receipt: SnapshotReceipt, inner: dict) -> bytes | None:
    """Decrypt an agent client-side envelope using the escrowed agent key.

    Agents (e.g. 1Password on a Mac) encrypt content locally under a data key and
    escrow that key wrapped to the vault recovery key. To recover in-portal we
    unwrap the escrowed key with the vault recovery private key, then decrypt the
    envelope. Returns None when no escrow is available (unrecoverable in-portal)."""
    coll = db.get(Collection, receipt.collection_id) if receipt.collection_id else None
    agent = db.get(DesktopAgent, coll.agent_id) if coll and coll.agent_id else None
    wrapped = (agent.config or {}).get("escrow_wrapped_key") if agent else None
    if not wrapped:
        return None
    # The agent escrowed to the tenant's first vault's recovery key at activation.
    rec_vault = (db.query(Vault).filter(Vault.tenant_id == agent.tenant_id).first()
                 if agent else None)
    recovery_priv = keybroker.release_recovery_private(
        rec_vault.id if rec_vault else receipt.vault_id)
    agent_key = unwrap_key(wrapped, recovery_priv)
    p = get_provider()
    wd = inner["wrappedDek"]
    dek = p.aes_decrypt(agent_key, _unb64(wd["nonce"]), _unb64(wd["ct"]), b"agent-dek")
    return p.aes_decrypt(dek, _unb64(inner["nonce"]), _unb64(inner["ct"]),
                         str(inner["objectId"]).encode())


def decrypt_recovered_units(db: Session, receipt: SnapshotReceipt, object_id: str,
                            units: dict) -> tuple[bytes | None, bool]:
    """Decrypt an object (and its chunks) from the in-memory envelope units an
    appliance returns for an OPEN_RECOVERY_WINDOW command. Mirrors the cloud
    retrieve path but reads envelopes from ``units`` rather than object storage.

    Returns ``(plaintext, client_encrypted)``. ``plaintext`` is None when the
    unit is missing/undecryptable; ``client_encrypted`` is True when the content
    is an agent envelope with no escrowed recovery key to open it."""
    obj = units.get(object_id)
    if not isinstance(obj, dict):
        return None, False
    try:
        root_key = keybroker.release_vault_root_key(receipt.vault_id)
        snapshot_key = EnvelopeKeyHierarchy(root_key).snapshot_key(
            receipt.vault_id, receipt.collection_id, receipt.snapshot_id)
        if obj.get("chunked"):
            buf = bytearray()
            for part in obj.get("parts", []):
                pdata = units.get(part["objectId"])
                if not isinstance(pdata, dict):
                    return None, False
                buf += decrypt_object(snapshot_key, pdata)
            plaintext = bytes(buf)
        else:
            plaintext = decrypt_object(snapshot_key, obj)
    except Exception as exc:  # noqa: BLE001
        logger.info("recover: could not decrypt %s (%s)", object_id, exc)
        return None, False
    # Agent-collected items are themselves client-encrypted envelopes.
    try:
        candidate = json.loads(plaintext.decode())
        if isinstance(candidate, dict) and candidate.get("wrappedDek") and candidate.get("v"):
            recovered = _unwrap_agent_content(db, receipt, candidate)
            if recovered is not None:
                return recovered, False
            return plaintext, True
    except Exception:
        pass
    return plaintext, False


def _location_label(destination: str, store_labels: dict[str, str] | None = None) -> str:
    if (destination.startswith("store:") or destination.startswith("byos:")) and store_labels:
        return store_labels.get(destination) or store_labels.get(destination, "Cloud storage")
    base = destination.split(":", 1)[0]
    return {
        "cv-cloud": "Arkive Cloud",
        "customer-s3": "Customer S3",
        "local-fs": "Local store",
        "appliance": "Appliance",
        "store": "Appliance storage",
        "byos": "Your cloud storage",
    }.get(base, destination)


def _as_str(v):
    """Coerce a label/meta value to a display string (objects → name/title)."""
    if isinstance(v, dict):
        return v.get("name") or v.get("title") or ""
    return v if isinstance(v, str) else str(v)


def _clean_labels(labels) -> list:
    """Labels must be plain strings for the UI — coerce objects, drop empties."""
    out = []
    for l in (labels or []):
        s = _as_str(l)
        if s:
            out.append(s)
    return out


def _clean_meta(meta) -> dict:
    """Coerce object/list-of-object meta values so the UI never renders a dict."""
    if not isinstance(meta, dict):
        return {}
    out = {}
    for k, v in meta.items():
        if isinstance(v, list):
            out[k] = [_as_str(x) if isinstance(x, dict) else x for x in v]
        elif isinstance(v, dict):
            out[k] = _as_str(v)
        else:
            out[k] = v
    return out


def _store_label_map(db: Session, tenant_id: str) -> dict[str, str]:
    """Map ``store:<id>`` → "<appliance> · <storage>" and ``byos:<id>`` → the
    customer storage name, for the tenant."""
    out: dict[str, str] = {}
    appliances = {a.id: a for a in db.query(Appliance)
                  .filter(Appliance.tenant_id == tenant_id).all()}
    for s in (db.query(ApplianceStorage)
              .filter(ApplianceStorage.tenant_id == tenant_id).all()):
        a = appliances.get(s.appliance_id)
        out[f"store:{s.id}"] = f"{a.name} · {s.name}" if a else s.name
    from ..models import CustomerStorage
    for cs in (db.query(CustomerStorage)
               .filter(CustomerStorage.tenant_id == tenant_id).all()):
        out[f"byos:{cs.id}"] = cs.name
    return out


def _byos_provider_map(db: Session, tenant_id: str) -> dict[str, str]:
    """Map ``byos:<id>`` → the provider (aws|azure|gcp) so the UI can show the
    right brand icon for a customer's own cloud storage destination."""
    from ..models import CustomerStorage
    return {f"byos:{cs.id}": cs.provider
            for cs in (db.query(CustomerStorage)
                       .filter(CustomerStorage.tenant_id == tenant_id).all())}


@router.get("/taxonomy")
def taxonomy(principal: security.Principal = Depends(security.get_principal)):
    """The canonical information model (categories + kinds) used across sources."""
    return describe()


@router.get("/thread")
def thread(chat_id: str, source_type: str = "imessage",
           date_from: str | None = None, date_to: str | None = None,
           principal: security.Principal = Depends(security.require_passkey),
           tenant: Tenant = Depends(security.get_tenant),
           db: Session = Depends(get_db)):
    """Reassemble a whole message conversation from its indexed messages +
    attachments (linked by chat_id / message_guid) so the user can read the full
    thread. Uses indexed metadata only — no content decryption/step-up needed.
    Optional date_from/date_to (YYYY-MM-DD) limit the window rebuilt."""
    vault_ids = security.content_vault_ids(db, principal)
    if not vault_ids:
        return {"chat_id": chat_id, "chat_name": "", "messages": []}

    def _parse_day(s: str | None, end: bool):
        if not s:
            return None
        try:
            d = datetime.fromisoformat(s.replace("Z", "").split("T")[0])
            return d.replace(hour=23, minute=59, second=59) if end else d
        except Exception:  # noqa: BLE001
            return None
    dt_from = _parse_day(date_from, False)
    dt_to = _parse_day(date_to, True)

    rows = (db.query(SearchDocument)
            .filter(SearchDocument.tenant_id == tenant.id,
                    SearchDocument.vault_id.in_(vault_ids),
                    SearchDocument.source_type == source_type)
            .order_by(SearchDocument.created_at.desc()).all())

    # Dedup to the latest row per object, keep only this conversation.
    seen: set = set()
    messages: list = []
    atts_by_msg: dict = {}
    chat_name = ""
    for r in rows:
        if r.object_id in seen:
            continue
        seen.add(r.object_id)
        meta = r.meta or {}
        if str(meta.get("chat_id")) != str(chat_id):
            continue
        chat_name = chat_name or meta.get("chat_name") or ""
        if r.modified_at is not None:
            if dt_from and r.modified_at < dt_from:
                continue
            if dt_to and r.modified_at > dt_to:
                continue
        when = r.modified_at.isoformat() if r.modified_at else None
        if r.doc_type == "message":
            messages.append({
                "object_id": r.object_id,
                "message_guid": meta.get("message_guid"),
                "title": r.title, "preview": r.preview,
                # `title` holds the message text (first 80 chars) captured at
                # collection; the composed preview is metadata only.
                "text": r.title,
                "from": meta.get("from"), "direction": meta.get("direction"),
                "service": meta.get("service"), "date": when,
                "has_attachments": bool(meta.get("has_attachments")),
            })
        else:  # attachment object
            atts_by_msg.setdefault(meta.get("message_guid"), []).append({
                "object_id": r.object_id, "filename": meta.get("filename") or r.title,
                "kind": r.doc_type, "mime": meta.get("mime"),
            })

    for m in messages:
        m["attachments"] = atts_by_msg.get(m["message_guid"], [])
    messages.sort(key=lambda m: m["date"] or "")
    return {"chat_id": chat_id, "chat_name": chat_name, "count": len(messages),
            "messages": messages}


@router.get("")
def search(q: str = "", source_type: str | None = None, doc_type: str | None = None,
           category: list[str] | None = Query(None), label: list[str] | None = Query(None),
           collection: list[str] | None = Query(None),
           attr: str | None = None, limit: int = 50, sort: str = "date",
           direction: str = "desc", date_from: str | None = None,
           date_to: str | None = None, date_field: str | None = None,
           principal: security.Principal = Depends(security.require_passkey),
           tenant: Tenant = Depends(security.get_tenant),
           db: Session = Depends(get_db)):
    # attr is an optional "key:value" attribute filter (e.g. "folder:Inbox"),
    # applied against the object's discrete indexed metadata.
    attr_key, attr_val = "", ""
    if attr and ":" in attr:
        attr_key, attr_val = attr.split(":", 1)
    # Type (category), label and source (per-account collection) filters are all
    # multi-select: an empty set means "no filter" (everything).
    cat_set = set(category or [])
    label_set = set(label or [])
    coll_set = set(collection or [])
    # Pull the whole tenant index newest-first, then de-duplicate: repeated
    # backups (and multi-destination stores) create a fresh index row per
    # snapshot for the same object. The UI must show each object once, so we keep
    # the newest row per (source, object) and remember every snapshot it appeared
    # in (for the "stored at" locations).
    # Data partitioning: search only ever returns items from the user's own
    # vaults — never another member's content.
    allowed = security.content_vault_ids(db, principal)
    # PERF: the free-text query is pushed to the DB (server-side filter) and the
    # heavy `search_blob` column is NEVER transferred — we select only the fields
    # the dedup, facets and result rows need. This keeps search responsive on
    # large indexes (previously every row incl. the full search text was hauled
    # into Python and filtered there).
    ql = q.lower().strip() if q else ""
    _cols = (SearchDocument.source_type, SearchDocument.object_id,
             SearchDocument.snapshot_id, SearchDocument.collection_id,
             SearchDocument.vault_id, SearchDocument.doc_type,
             SearchDocument.title, SearchDocument.preview, SearchDocument.meta,
             SearchDocument.labels, SearchDocument.size_bytes,
             SearchDocument.modified_at, SearchDocument.created_at)
    if allowed:
        _base = (db.query(*_cols)
                 .filter(SearchDocument.tenant_id == tenant.id,
                         SearchDocument.vault_id.in_(allowed)))
        if ql:
            _like = f"%{ql}%"
            _base = _base.filter(or_(
                func.lower(SearchDocument.search_blob).like(_like),
                func.lower(SearchDocument.title).like(_like),
                func.lower(SearchDocument.preview).like(_like)))
        all_docs = _base.order_by(SearchDocument.created_at.desc()).all()
    else:
        all_docs = []

    unique: list[SearchDocument] = []
    seen: set[tuple] = set()
    object_snapshots: dict[tuple, set[str]] = {}
    # Earliest index time per object = when the entity was first ingested. Rows
    # arrive newest-first, so the last row seen for a key is the oldest.
    first_ingested: dict[tuple, object] = {}
    for r in all_docs:
        key = (r.source_type, r.object_id)
        object_snapshots.setdefault(key, set())
        if r.snapshot_id:
            object_snapshots[key].add(r.snapshot_id)
        if r.created_at is not None:
            cur = first_ingested.get(key)
            if cur is None or r.created_at < cur:
                first_ingested[key] = r.created_at
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)

    # Every facet is cross-filtered: its counts reflect the set narrowed by the
    # OTHER active filters (so choosing a source updates the type/category/label
    # options), while its own filter is excluded. Categories are normalized via
    # the current taxonomy so stale stored rows (e.g. images once tagged "media")
    # are corrected on read.

    # Optional date-range filter, scoped to either the object's own date
    # ("date") or its capture/ingest date ("captured").
    def _parse_bound(val: str | None, end: bool) -> datetime | None:
        if not val:
            return None
        try:
            d = datetime.fromisoformat(val.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if d.tzinfo is not None:
            d = d.astimezone(timezone.utc).replace(tzinfo=None)
        # A bare date ("2026-08-22") covers the whole day.
        if len(val.strip()) <= 10:
            d = d.replace(hour=23, minute=59, second=59, microsecond=999999) if end \
                else d.replace(hour=0, minute=0, second=0, microsecond=0)
        return d

    dt_from = _parse_bound(date_from, end=False)
    dt_to = _parse_bound(date_to, end=True)
    scope_captured = (date_field == "captured")

    def _norm_dt(d):
        if d is None:
            return None
        return d.astimezone(timezone.utc).replace(tzinfo=None) if getattr(d, "tzinfo", None) else d

    def _scoped_date(r: SearchDocument):
        key = (r.source_type, r.object_id)
        if scope_captured:
            return _norm_dt(first_ingested.get(key) or r.created_at)
        return _norm_dt(r.modified_at or first_ingested.get(key) or r.created_at)

    def _in_range(r: SearchDocument) -> bool:
        if not dt_from and not dt_to:
            return True
        sd = _scoped_date(r)
        if sd is None:
            return False
        if dt_from and sd < dt_from:
            return False
        if dt_to and sd > dt_to:
            return False
        return True

    def _cat(r: SearchDocument) -> str:
        return category_for_kind(r.doc_type)

    def _attr_match(r: SearchDocument) -> bool:
        if not attr_key:
            return True
        v = (r.meta or {}).get(attr_key)
        if isinstance(v, (list, tuple)):
            return attr_val in [str(x) for x in v]
        return str(v) == attr_val

    def _matches(r: SearchDocument, skip: str = "") -> bool:
        if skip != "source":
            if source_type and r.source_type != source_type:
                return False
            if coll_set and r.collection_id not in coll_set:
                return False
        if skip != "type" and doc_type and r.doc_type != doc_type:
            return False
        if skip != "category" and cat_set and _cat(r) not in cat_set:
            return False
        if skip != "label" and label_set and not (
                label_set & {str(x) for x in (r.labels or [])}):
            return False
        if skip != "attr" and not _attr_match(r):
            return False
        if not _in_range(r):
            return False
        return True

    by_source: dict[str, int] = {}
    by_type: dict[str, int] = {}
    by_category: dict[str, int] = {}
    by_label: dict[str, int] = {}
    # Per-account (collection) breakdown under each source type.
    acct_counts: dict[tuple, int] = {}
    for r in unique:
        if _matches(r, skip="source"):
            by_source[r.source_type] = by_source.get(r.source_type, 0) + 1
            akey = (r.source_type, r.collection_id)
            acct_counts[akey] = acct_counts.get(akey, 0) + 1
        if _matches(r, skip="type"):
            by_type[r.doc_type] = by_type.get(r.doc_type, 0) + 1
        if _matches(r, skip="category"):
            cat = _cat(r)
            by_category[cat] = by_category.get(cat, 0) + 1
        if _matches(r, skip="label"):
            for lbl in (r.labels or []):
                # Labels must be simple strings; coerce anything else so a bad
                # ingest can never make an unhashable facet key crash search.
                if not isinstance(lbl, str):
                    lbl = (lbl.get("name") or lbl.get("title")) if isinstance(lbl, dict) else str(lbl)
                if not lbl:
                    continue
                by_label[lbl] = by_label.get(lbl, 0) + 1

    # Resolve friendly source titles (the linked account label / mapping name)
    # and connector display names for every collection referenced by results.
    coll_ids = {r.collection_id for r in unique if r.collection_id}
    coll_label: dict[str, str] = {}
    coll_username: dict[str, str] = {}
    coll_display: dict[str, str] = {}
    if coll_ids:
        for c in (db.query(Collection)
                  .filter(Collection.id.in_(coll_ids)).all()):
            account = (db.get(ConnectorAccount, c.connector_account_id)
                       if c.connector_account_id else None)
            coll_label[c.id] = account.account_label if account else c.name
            if account and account.account_username:
                coll_username[c.id] = account.account_username
            conn = get_connector(c.source_type)
            coll_display[c.id] = conn.display_name if conn else c.source_type
    source_display: dict[str, str] = {}
    for st in {r.source_type for r in unique}:
        conn = get_connector(st)
        source_display[st] = conn.display_name if conn else st

    # Per-account facet: each source type expands to the individual linked
    # accounts (collections) it holds, so the source filter can drill into a
    # specific account (e.g. "Rob's Gmail" vs "Home Gmail").
    source_accounts: dict[str, list] = {}
    for (st, cid), cnt in acct_counts.items():
        source_accounts.setdefault(st, []).append({
            "id": cid,
            "label": coll_label.get(cid) or source_display.get(st, st),
            "username": coll_username.get(cid),
            "count": cnt,
        })
    for st in source_accounts:
        source_accounts[st].sort(key=lambda a: a["count"], reverse=True)

    # Attribute facets over the set filtered by every OTHER filter (not attr).
    meta_facets: dict[str, dict[str, int]] = {}
    for r in unique:
        if not _matches(r, skip="attr"):
            continue
        for k, v in (r.meta or {}).items():
            if k == "client_encrypted" or v in (None, "", [], {}):
                continue
            values = v if isinstance(v, (list, tuple)) else [v]
            for val in values:
                sval = str(val)
                if not sval or len(sval) > 60:
                    continue
                meta_facets.setdefault(k, {})
                meta_facets[k][sval] = meta_facets[k].get(sval, 0) + 1
    # Keep only keys with more than one distinct value (useful as filters), and
    # cap values per key.
    meta_facets = {
        k: dict(sorted(vals.items(), key=lambda kv: kv[1], reverse=True)[:8])
        for k, vals in meta_facets.items() if len(vals) > 1
    }

    # Final rows: every active filter applied.
    filtered = [r for r in unique if _matches(r)]
    # Order by the object's OWN timestamp ("date", default) or when we first
    # ingested it ("captured"); direction defaults to newest-first ("desc").
    _MIN = datetime.min
    if sort == "captured":
        def _sort_key(r: SearchDocument):
            return _norm_dt(first_ingested.get((r.source_type, r.object_id))
                            or r.created_at or r.modified_at) or _MIN
    else:
        def _sort_key(r: SearchDocument):
            return _norm_dt(r.modified_at
                            or first_ingested.get((r.source_type, r.object_id))
                            or r.created_at) or _MIN
    filtered.sort(key=_sort_key, reverse=(direction != "asc"))
    rows = filtered[:limit]

    # Map each result's object to every destination it is stored in (across all
    # of its snapshots): cloud, appliance, customer S3.
    all_snap_ids: set[str] = set()
    for r in rows:
        all_snap_ids |= object_snapshots.get((r.source_type, r.object_id), set())
    receipts_by_snap: dict[str, list] = {}
    if all_snap_ids:
        for rc in (db.query(SnapshotReceipt)
                   .filter(SnapshotReceipt.tenant_id == tenant.id,
                           SnapshotReceipt.snapshot_id.in_(all_snap_ids)).all()):
            receipts_by_snap.setdefault(rc.snapshot_id, []).append(rc)

    store_labels = _store_label_map(db, tenant.id)
    byos_providers = _byos_provider_map(db, tenant.id)

    # Version history per object (content-addressed versioning): identical
    # re-collections dedupe; real changes accrue versions so tampering, edits, or
    # deletions in the source stay recoverable.
    result_oids = list({r.object_id for r in rows})
    versions_by_oid: dict[tuple, list] = {}
    if result_oids:
        for ov in (db.query(ObjectVersion)
                   .filter(ObjectVersion.tenant_id == tenant.id,
                           ObjectVersion.object_id.in_(result_oids)).all()):
            versions_by_oid.setdefault((ov.source_type, ov.object_id), []).append(ov)

    def _versions_for(r: SearchDocument) -> list:
        ovs = sorted(versions_by_oid.get((r.source_type, r.object_id), []),
                     key=lambda v: v.version, reverse=True)
        return [{"version": v.version, "snapshot_id": v.snapshot_id,
                 "size_bytes": v.size_bytes,
                 "created_at": v.created_at.isoformat() if v.created_at else None,
                 "is_current": bool(v.is_current)} for v in ovs]

    def _locations_for(r: SearchDocument) -> list:
        out: dict[str, dict] = {}
        for snap in object_snapshots.get((r.source_type, r.object_id), set()):
            for rc in receipts_by_snap.get(snap, []):
                # Prefer the recoverable copy when the same destination recurs.
                existing = out.get(rc.destination)
                if existing and existing["recoverable"]:
                    continue
                out[rc.destination] = {
                    "destination": rc.destination,
                    "label": _location_label(rc.destination, store_labels),
                    "provider": byos_providers.get(rc.destination),
                    "recoverable": bool(rc.recoverable),
                }
        return list(out.values())

    results = [{
        "object_id": r.object_id,
        "snapshot_id": r.snapshot_id,
        "collection_id": r.collection_id,
        "source_type": r.source_type,
        "source_label": coll_label.get(r.collection_id, source_display.get(r.source_type, r.source_type)),
        "source_username": coll_username.get(r.collection_id),
        "source_display": coll_display.get(r.collection_id, source_display.get(r.source_type, r.source_type)),
        "doc_type": r.doc_type,
        "category": _cat(r),
        "sensitivity": sensitivity_for(_cat(r)),
        "title": r.title,
        "preview": r.preview,
        "meta": _clean_meta(r.meta),
        "labels": _clean_labels(r.labels),
        "size_bytes": r.size_bytes,
        "modified_at": r.modified_at.isoformat() if r.modified_at else None,
        "first_ingested_at": (first_ingested.get((r.source_type, r.object_id)).isoformat()
                              if first_ingested.get((r.source_type, r.object_id)) else None),
        "locations": _locations_for(r),
        "versions": _versions_for(r),
        "version_count": len(versions_by_oid.get((r.source_type, r.object_id), [])),
    } for r in rows]

    return {
        "count": len(results),
        "total_indexed": len(unique),
        "results": results,
        "facets": {"source": by_source, "type": by_type,
                   "category": by_category, "label": by_label,
                   "attributes": meta_facets, "source_accounts": source_accounts},
        "source_display": source_display,
    }


class RetrieveRequest(BaseModel):
    snapshot_id: str
    object_id: str
    destination: str  # the chosen storage location for this item


@router.post("/retrieve")
def retrieve(body: RetrieveRequest,
             principal: security.Principal = Depends(security.require_passkey),
             tenant: Tenant = Depends(security.get_tenant),
             db: Session = Depends(get_db)):
    """Retrieve an item from wherever the customer stored it. Cloud/S3 objects are
    read directly (returned client-encrypted); appliance-stored objects are pulled
    via a signed recovery-window command to the offline appliance."""
    base = body.destination.split(":", 1)[0]
    store_labels = _store_label_map(db, tenant.id)
    label = _location_label(body.destination, store_labels)

    if base in ("appliance", "store"):
        appliance = None
        if base == "store":
            store = db.get(ApplianceStorage, body.destination.split(":", 1)[1])
            if store and store.tenant_id == tenant.id:
                appliance = db.get(Appliance, store.appliance_id)
        elif ":" in body.destination:
            aid = body.destination.split(":", 1)[1]
            appliance = db.get(Appliance, aid)
        if not appliance or appliance.tenant_id != tenant.id:
            appliance = (db.query(Appliance)
                         .filter(Appliance.tenant_id == tenant.id,
                                 Appliance.state.in_(["SEALED", "ONLINE_STAGING"]))
                         .order_by(Appliance.last_heartbeat_at.desc()).first())
        if not appliance:
            raise HTTPException(404, "no appliance available to retrieve from")
        cmd = fleet.issue_command(
            db, appliance, "OPEN_RECOVERY_WINDOW", principal.user_id,
            {"snapshotId": body.snapshot_id, "objectIds": [body.object_id],
             "operatorApproved": True, "approvedBy": principal.user_id})
        audit.record(db, actor=principal.user_id, action="search.retrieve",
                     tenant_id=tenant.id, resource=body.object_id,
                     detail={"location": label, "appliance": appliance.id})
        return {"status": "requested", "location": label, "async": True,
                "appliance_name": appliance.name,
                "message": f"Recovery requested from {appliance.name}. "
                           "The appliance unseals, retrieves, and re-seals; "
                           "local approval may be required.",
                "command_id": cmd.id}

    # Cloud / customer-S3 / customer-owned (byos) / local: read the stored
    # envelope and decrypt within the authorized key boundary (passkey step-up
    # already required) so the caller can download the original content.
    try:
        if base == "byos":
            # Customer's own storage — reads use the passkey-gated READ credential.
            from .. import customer_storage as _cs
            sid = _cs.storage_id_from_dest(body.destination)
            cstore = _cs.get_for_tenant(db, tenant.id, sid) if sid else None
            if cstore is None:
                raise HTTPException(404, "customer storage not found")
            dest = _cs.build_destination(db, cstore, "read")
            if dest is None:
                raise HTTPException(400, "no read credential configured for this storage")
            prefix = _cs.object_prefix(cstore)
        else:
            dest = build_destination(body.destination if base in ("cv-cloud", "customer-s3") else "cv-cloud")
            prefix = tenant.storage_prefix or tenant.id
        data = dest.get_object(prefix, f"{body.snapshot_id}/{body.object_id}")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(404, f"object not found at {label}: {exc}")

    # Resolve the snapshot's vault/collection to derive the decryption key.
    receipt = (db.query(SnapshotReceipt)
               .filter(SnapshotReceipt.tenant_id == tenant.id,
                       SnapshotReceipt.snapshot_id == body.snapshot_id).first())
    content_b64 = None
    size_bytes = len(data)
    client_encrypted = False
    plaintext_bytes: bytes | None = None
    try:
        obj = json.loads(data.decode())
        decryptable = isinstance(obj, dict) and ("wrappedDek" in obj or obj.get("chunked"))
        if receipt is not None and decryptable:
            root_key = keybroker.release_vault_root_key(receipt.vault_id)
            snapshot_key = EnvelopeKeyHierarchy(root_key).snapshot_key(
                receipt.vault_id, receipt.collection_id, body.snapshot_id)
            if obj.get("chunked"):
                # Reassemble large content from its encrypted chunks.
                buf = bytearray()
                for part in obj.get("parts", []):
                    pdata = dest.get_object(prefix, f"{body.snapshot_id}/{part['objectId']}")
                    buf += decrypt_object(snapshot_key, json.loads(pdata.decode()))
                plaintext = bytes(buf)
            else:
                plaintext = decrypt_object(snapshot_key, obj)
            # Agent-collected items are client-encrypted; the decrypted layer is
            # itself an agent envelope — unwrap it with the escrowed agent key.
            inner = None
            try:
                candidate = json.loads(plaintext.decode())
                if isinstance(candidate, dict) and candidate.get("wrappedDek") and candidate.get("v"):
                    inner = candidate
            except Exception:
                inner = None
            if inner is not None:
                recovered = _unwrap_agent_content(db, receipt, inner)
                if recovered is not None:
                    plaintext = recovered
                else:
                    client_encrypted = True  # no escrow available to open it
            plaintext_bytes = plaintext
            size_bytes = len(plaintext)
    except Exception as exc:  # noqa: BLE001 - legacy metadata-only objects
        logger.info("retrieve: could not decrypt %s (%s)", body.object_id, exc)

    doc = (db.query(SearchDocument)
           .filter(SearchDocument.tenant_id == tenant.id,
                   SearchDocument.object_id == body.object_id).first())
    title = (doc.title if doc else body.object_id) or body.object_id
    doc_type = doc.doc_type if doc else ""
    source_type = doc.source_type if doc else ""

    if plaintext_bytes is None:
        audit.record(db, actor=principal.user_id, action="search.retrieve",
                     tenant_id=tenant.id, resource=body.object_id,
                     detail={"location": label, "bytes": size_bytes})
        return {"status": "unavailable", "location": label, "async": False,
                "message": f"Object available at {label} ({size_bytes} bytes). "
                           "This item was captured before full-content backup; re-run a backup to store its content."}
    if client_encrypted:
        audit.record(db, actor=principal.user_id, action="search.retrieve",
                     tenant_id=tenant.id, resource=body.object_id,
                     detail={"location": label, "bytes": size_bytes})
        return {"status": "client-encrypted", "location": label, "async": False,
                "size_bytes": size_bytes, "encrypted": True,
                "content_b64": base64.b64encode(plaintext_bytes).decode(),
                "filename": title + ".enc",
                "message": f"Recovered {size_bytes} bytes from {label} (still client-encrypted — "
                           "the collecting agent has not escrowed a recovery key)."}

    # Stage the decrypted item into a time-limited recovery window.
    from . import recovery
    item = recovery.create_recovered(
        db, tenant.id, principal.user_id, object_id=body.object_id,
        snapshot_id=body.snapshot_id, title=title, doc_type=doc_type,
        source_type=source_type, location=label, content=plaintext_bytes)
    audit.record(db, actor=principal.user_id, action="recovery.opened",
                 tenant_id=tenant.id, resource=body.object_id,
                 detail={"location": label, "bytes": size_bytes,
                         "expires_at": item.expires_at.isoformat()})
    return {
        "status": "recovered", "async": False, "location": label,
        "recovered_id": item.id, "title": item.title, "mime": item.mime,
        "size_bytes": item.size_bytes, "doc_type": item.doc_type,
        "object_modified_at": item.object_modified_at.isoformat() if item.object_modified_at else None,
        "expires_in_seconds": recovery.get_settings().recovered_ttl_seconds,
        "message": f"Recovered {size_bytes} bytes from {label} — viewable until it expires.",
    }


@router.get("/retrieve-status/{command_id}")
def retrieve_status(command_id: str,
                    principal: security.Principal = Depends(security.get_principal),
                    tenant: Tenant = Depends(security.get_tenant),
                    db: Session = Depends(get_db)):
    """Poll an appliance recovery command. While the appliance unseals/retrieves
    the command is ``pending``/``delivered``; once it re-seals and returns the
    content the cloud decrypts and stages it, exposing the recovered item(s).

    Only status metadata is returned here (no protected content), so this does not
    require passkey step-up — otherwise the 2s poll would hit step-up expiry and
    the recovery modal would hang instead of showing the outcome."""
    cmd = db.get(ApplianceCommand, command_id)
    if not cmd or cmd.tenant_id != tenant.id:
        raise HTTPException(404, "command not found")
    result = cmd.result or {}
    # pending -> requested to the appliance; delivered -> appliance is working;
    # acked -> content returned; rejected/expired -> refused/timed out.
    stage = {
        "pending": "requested", "delivered": "retrieving",
        "acked": "ready", "rejected": "failed", "expired": "failed",
    }.get(cmd.status, cmd.status)
    recovered = result.get("recovered") or []
    if result.get("awaiting_local_approval"):
        stage = "awaiting_approval"
    if cmd.status == "acked" and not recovered and not result.get("awaiting_local_approval"):
        # Acked but nothing decryptable came back.
        stage = "unavailable"
    message = result.get("message")
    if cmd.status == "expired":
        message = "The appliance did not respond before the recovery command expired."
    elif cmd.status == "rejected":
        message = ("The appliance rejected the recovery command. This usually means its "
                   "pinned control-plane key is stale — it will re-pin on its next heartbeat; "
                   "try again in a moment.")
    return {
        "status": stage,
        "command_status": cmd.status,
        "recovered": recovered,
        "error": result.get("error"),
        "message": message,
    }
