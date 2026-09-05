"""Unified-search index replication.

Serializes a scope's search index (the ``search_documents`` rows that let Arkive
piece its stored data back together) into an ENCRYPTED SQLite file and writes it
alongside the data it describes on every storage destination the scope uses. This
gives a disaster-recovery copy of the index and, in future, lets an on-prem
appliance run a localized unified search after a catastrophe.

Scope (isolation is critical — only ever the owner's own index):
  * personal accounts (shared tenant) → the USER's index (their owned vaults);
  * dedicated/org tenants → the whole TENANT index (appliances are tenant-assigned).

Runs on whichever server owns the data (its local DB only holds its own
SearchDocuments), so a federated node replicates its tenants' indexes itself.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import func

from .. import credstore
from ..config import get_settings
from ..db import SessionLocal
from ..models import (Appliance, CustomerStorage, IndexReplica, Node,
                      SearchDocument, SnapshotReceipt, Tenant, Vault)

logger = logging.getLogger("cv.index_replication")

# How often to re-check a scope; a rebuild only happens when the index actually
# changed (content signature differs), so this is cheap when idle.
REPLICATE_INTERVAL_SECONDS = 6 * 3600
_INDEX_SCHEMA_VERSION = 1
_INDEX_KEY = "search-index/index.sqlite.enc"  # object key under the scope prefix

# Columns copied into the portable index (everything unified search needs to
# rebuild results locally). search_blob/meta are the heavy text/JSON fields.
_COLS = ["id", "tenant_id", "vault_id", "collection_id", "source_type",
         "object_id", "doc_type", "category", "title", "preview", "search_blob",
         "size_bytes", "is_current"]


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# Scope discovery                                                             #
# --------------------------------------------------------------------------- #

def _scopes(db) -> list[dict]:
    """Every (scope, scope_id, tenant, vault_ids) THIS server owns. In a federated
    fleet each server replicates only its own tenants' indexes (the assigned node
    for node-routed tenants, the control plane for the rest) so work isn't doubled."""
    s = get_settings()
    federated = bool(getattr(s, "node_sync_scope", False))
    self_node = db.query(Node).filter(Node.is_self == True).first() if federated else None  # noqa: E712
    self_id = self_node.id if self_node else None
    is_cp = (getattr(s, "node_role", "") or "") == "control-plane" or self_node is None

    def _owned(t: Tenant) -> bool:
        if not federated:
            return True
        if t.node_id:                      # assigned to a node
            return t.node_id == self_id
        return is_cp                        # unassigned -> control plane owns it

    out: list[dict] = []
    for t in db.query(Tenant).all():
        if not _owned(t):
            continue
        vaults = db.query(Vault).filter(Vault.tenant_id == t.id).all()
        if not vaults:
            continue
        if (t.tenant_type or "dedicated") == "shared":
            # Personal accounts pooled in a shared tenant — one scope per owner.
            by_owner: dict[str, list[str]] = {}
            for v in vaults:
                if v.owner_user_id:
                    by_owner.setdefault(v.owner_user_id, []).append(v.id)
            for uid, vids in by_owner.items():
                out.append({"scope": "user", "scope_id": uid, "tenant": t, "vault_ids": vids})
        else:
            out.append({"scope": "tenant", "scope_id": t.id, "tenant": t,
                        "vault_ids": [v.id for v in vaults]})
    return out


def _index_signature(db, vault_ids: list[str]) -> tuple[str, int]:
    """A cheap fingerprint of the scope's current index (count + newest row), so an
    unchanged index is never rebuilt/re-uploaded. Returns (signature, count)."""
    q = (db.query(func.count(SearchDocument.id), func.max(SearchDocument.created_at))
         .filter(SearchDocument.vault_id.in_(vault_ids),
                 SearchDocument.is_current == True))  # noqa: E712
    count, newest = q.one()
    sig = hashlib.sha256(f"{count}|{newest}".encode()).hexdigest()[:16]
    return sig, int(count or 0)


def _destinations(db, vault_ids: list[str]) -> list[str]:
    """Distinct storage destinations where this scope's data lives — the index is
    replicated to each so it sits with the data it describes."""
    rows = (db.query(SnapshotReceipt.destination)
            .filter(SnapshotReceipt.vault_id.in_(vault_ids))
            .distinct().all())
    return sorted({r[0] for r in rows if r[0]})


# --------------------------------------------------------------------------- #
# Serialize + encrypt                                                         #
# --------------------------------------------------------------------------- #

def _build_index_blob(db, scope: dict, signature: str) -> tuple[bytes, int]:
    """Build the encrypted SQLite index for a scope. Returns (ciphertext, count)."""
    vault_ids = scope["vault_ids"]
    tmp = Path(tempfile.mkstemp(prefix="arkive-index-", suffix=".sqlite")[1])
    count = 0
    try:
        con = sqlite3.connect(str(tmp))
        con.execute("PRAGMA journal_mode=OFF")
        con.execute(
            "CREATE TABLE search_documents ("
            + ", ".join(f'"{c}" TEXT' for c in _COLS) + ", modified_at TEXT, created_at TEXT)")
        con.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
        placeholders = ", ".join("?" for _ in _COLS) + ", ?, ?"
        insert = f"INSERT INTO search_documents VALUES ({placeholders})"
        # Stream rows in batches so a large index never all sits in memory.
        q = (db.query(SearchDocument)
             .filter(SearchDocument.vault_id.in_(vault_ids),
                     SearchDocument.is_current == True)  # noqa: E712
             .yield_per(500))
        batch = []
        for d in q:
            row = [getattr(d, c) for c in _COLS]
            # meta is JSON; store as text.
            row = [json.dumps(v, default=str) if isinstance(v, (dict, list)) else v for v in row]
            batch.append(row + [
                d.modified_at.isoformat() if d.modified_at else None,
                d.created_at.isoformat() if d.created_at else None,
            ])
            if len(batch) >= 500:
                con.executemany(insert, batch)
                count += len(batch)
                batch = []
        if batch:
            con.executemany(insert, batch)
            count += len(batch)
        con.executemany("INSERT INTO meta VALUES (?, ?)", [
            ("schema_version", str(_INDEX_SCHEMA_VERSION)),
            ("scope", scope["scope"]), ("scope_id", scope["scope_id"]),
            ("tenant_id", scope["tenant"].id),
            ("object_count", str(count)), ("signature", signature),
            ("generated_at", _now().isoformat()),
        ])
        con.commit()
        con.close()
        raw = tmp.read_bytes()
    finally:
        for p in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
            try:
                p.unlink()
            except OSError:
                pass
    # Encrypt with the fleet KEK scoped to this tenant so only Arkive (or a node
    # sharing CV_KEK_SECRET) can decrypt the replicated index.
    cipher = credstore.encrypt_bytes(f"index:{scope['tenant'].id}", raw)
    return cipher, count


# --------------------------------------------------------------------------- #
# Destination resolution + write                                             #
# --------------------------------------------------------------------------- #

def _dest_label(db, tenant_id: str, dest: str) -> str:
    try:
        from ..api.search import _location_label, _store_label_map
        return _location_label(dest, _store_label_map(db, tenant_id))
    except Exception:  # noqa: BLE001
        return dest


def _resolve_object_dest(db, tenant: Tenant, dest: str):
    """Return (destination, tenant_prefix) for an OBJECT-storage destination the
    index can be written to now, or (None, "") for appliance destinations (their
    localized-index support is a future build) so we record them as pending."""
    from ..storage import build_destination
    from .. import customer_storage as _cs
    prefix = tenant.storage_prefix or f"t-{tenant.id[:8]}"
    if dest in ("cv-cloud", "customer-s3"):
        return build_destination(dest), prefix
    if dest.startswith(_cs.DEST_PREFIX):
        sid = _cs.storage_id_from_dest(dest)
        cs = db.get(CustomerStorage, sid) if sid else None
        if cs and cs.tenant_id == tenant.id:
            d = _cs.build_destination(db, cs, mode="write")
            return d, _cs.object_prefix(cs)
        return None, ""
    # Appliance (store:<id> / appliance:<id>) — localized index is future work.
    return None, ""


def _upsert_replica(db, scope: dict, dest: str, **fields) -> IndexReplica:
    r = (db.query(IndexReplica)
         .filter(IndexReplica.scope == scope["scope"],
                 IndexReplica.scope_id == scope["scope_id"],
                 IndexReplica.destination == dest).first())
    if r is None:
        r = IndexReplica(tenant_id=scope["tenant"].id, scope=scope["scope"],
                         scope_id=scope["scope_id"], destination=dest)
        db.add(r)
    r.destination_label = _dest_label(db, scope["tenant"].id, dest)
    for k, v in fields.items():
        setattr(r, k, v)
    return r


def _self_node_id(db) -> Optional[str]:
    n = db.query(Node).filter(Node.is_self == True).first()  # noqa: E712
    return n.id if n else None


def replicate_scope(db, scope: dict, node_id: Optional[str]) -> None:
    dests = _destinations(db, scope["vault_ids"])
    if not dests:
        return
    signature, _ = _index_signature(db, scope["vault_ids"])
    cipher: Optional[bytes] = None
    count = 0
    for dest in dests:
        destination, prefix = _resolve_object_dest(db, scope["tenant"], dest)
        if destination is None:
            # Appliance / unsupported target — record intent as pending.
            _upsert_replica(db, scope, dest, status="pending", signature="",
                            node_id=node_id,
                            error="localized appliance index not yet supported")
            continue
        existing = (db.query(IndexReplica)
                    .filter(IndexReplica.scope == scope["scope"],
                            IndexReplica.scope_id == scope["scope_id"],
                            IndexReplica.destination == dest).first())
        if existing and existing.status == "ok" and existing.signature == signature:
            continue  # index unchanged since last replica for this destination
        if cipher is None:
            cipher, count = _build_index_blob(db, scope, signature)
        try:
            destination.put_object(prefix, _INDEX_KEY, cipher, immutable=False)
            _upsert_replica(db, scope, dest, status="ok", object_count=count,
                            bytes=len(cipher), signature=signature, key=_INDEX_KEY,
                            node_id=node_id, last_replicated_at=_now(), error="")
        except Exception as exc:  # noqa: BLE001
            _upsert_replica(db, scope, dest, status="error", node_id=node_id,
                            error=str(exc)[:400])
            logger.warning("index replica failed scope=%s:%s dest=%s: %s",
                           scope["scope"], scope["scope_id"], dest, exc)
    db.commit()


_last_run: Optional[datetime] = None


def replicate_due(force: bool = False) -> None:
    """Scheduler entry point. Interval-gated; a scope is skipped when its index
    signature is unchanged, so idle cycles are cheap."""
    global _last_run
    now = _now()
    if not force and _last_run and (now - _last_run).total_seconds() < REPLICATE_INTERVAL_SECONDS:
        return
    _last_run = now
    with SessionLocal() as db:
        node_id = _self_node_id(db)
        for scope in _scopes(db):
            try:
                replicate_scope(db, scope, node_id)
            except Exception:  # noqa: BLE001
                db.rollback()
                logger.exception("index replication failed for scope %s:%s",
                                 scope["scope"], scope["scope_id"])
