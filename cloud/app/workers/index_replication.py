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
         "meta", "size_bytes", "is_current"]


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


def stage_dir() -> Path:
    """Where encrypted per-scope index blobs are staged for appliances to PULL.
    Under the object store's parent so it's inside the service's writable paths."""
    import os
    base = os.environ.get("CV_OBJECT_STORE") or "/var/lib/continuity-vault/object_store"
    d = Path(base).parent / "index-staging"
    d.mkdir(parents=True, exist_ok=True)
    return d


def stage_filename(scope: str, scope_id: str, store_id: str) -> str:
    safe = "".join(c for c in f"{scope}-{scope_id}-{store_id}" if c.isalnum() or c in "-_")
    return f"{safe}.sqlite.enc"


def staged_index_path(scope: str, scope_id: str, store_id: str) -> Path:
    return stage_dir() / stage_filename(scope, scope_id, store_id)


def _stage_index_to_appliance(db, scope: dict, dest: str, cipher: bytes,
                              count: int, signature: str, node_id: Optional[str]) -> None:
    """Stage the encrypted index for an appliance to PULL over its authenticated
    HTTPS channel, then issue a small signed STAGE_INDEX command pointing at it.
    The blob is NEVER embedded in the command envelope (that both capped the size
    at 24MB and bloated appliance_commands), so any index size can replicate."""
    from ..models import Appliance, ApplianceStorage
    from .. import fleet
    sid = dest.split(":", 1)[1] if ":" in dest else None
    store = db.get(ApplianceStorage, sid) if sid else None
    appliance = db.get(Appliance, store.appliance_id) if store else None
    if not appliance:
        _upsert_replica(db, scope, dest, status="error", node_id=node_id,
                        error="appliance for store not found")
        return
    existing = (db.query(IndexReplica)
                .filter(IndexReplica.scope == scope["scope"],
                        IndexReplica.scope_id == scope["scope_id"],
                        IndexReplica.destination == dest).first())
    if existing and existing.status == "ok" and existing.signature == signature:
        return  # unchanged since last stage
    # Write the encrypted blob to the staging dir (atomic) for the appliance to GET.
    try:
        path = staged_index_path(scope["scope"], scope["scope_id"], sid or "")
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(cipher)
        tmp.replace(path)
    except OSError as exc:
        _upsert_replica(db, scope, dest, status="error", node_id=node_id,
                        object_count=count, bytes=len(cipher),
                        error=f"could not stage index blob: {exc}"[:400])
        return
    try:
        fleet.issue_command(db, appliance, "STAGE_INDEX", "system", {
            "scope": scope["scope"], "scopeId": scope["scope_id"],
            "storeId": sid, "key": _INDEX_KEY, "objectCount": count,
            "bytes": len(cipher), "signature": signature, "pull": True})
        _upsert_replica(db, scope, dest, status="ok", object_count=count,
                        bytes=len(cipher), signature=signature, key=_INDEX_KEY,
                        node_id=node_id, last_replicated_at=_now(), error="")
        logger.info("index staged for appliance %s scope=%s:%s (%d bytes, pull) — "
                    "STAGE_INDEX issued", appliance.id, scope["scope"],
                    scope["scope_id"], len(cipher))
    except Exception as exc:  # noqa: BLE001
        _upsert_replica(db, scope, dest, status="error", node_id=node_id,
                        error=f"stage command failed: {exc}"[:400])


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
            # Appliance destination — stage the encrypted index over the signed
            # command channel so the box holds a DR copy (localized search later).
            if dest.startswith("store:") or dest.startswith("appliance"):
                if cipher is None:
                    cipher, count = _build_index_blob(db, scope, signature)
                _stage_index_to_appliance(db, scope, dest, cipher, count, signature, node_id)
                continue
            _upsert_replica(db, scope, dest, status="pending", signature="",
                            node_id=node_id, error="unsupported destination")
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


def rebuild_from_replica(db, replica) -> dict:
    """Reconstruct a scope's search index (SearchDocument rows) from a replica —
    the disaster-recovery path when the live index is lost. Reads the encrypted
    SQLite from the replica's destination, decrypts it, and inserts any missing
    rows (upsert by id). Logs verbosely; returns a summary. Runs on the server
    that owns the scope."""
    import json as _json
    tenant = db.get(Tenant, replica.tenant_id)
    if tenant is None:
        return {"error": "tenant not found"}
    dest, prefix = _resolve_object_dest(db, tenant, replica.destination)
    if dest is None:
        return {"error": "destination not restorable (appliance/localized index)"}
    logger.info("index rebuild: reading replica scope=%s:%s dest=%s",
                replica.scope, replica.scope_id, replica.destination)
    cipher = dest.get_object(prefix, replica.key or _INDEX_KEY)
    raw = credstore.decrypt_bytes(f"index:{tenant.id}", cipher)
    tmp = Path(tempfile.mkstemp(prefix="arkive-rebuild-", suffix=".sqlite")[1])
    restored = skipped = 0
    try:
        tmp.write_bytes(raw)
        con = sqlite3.connect(str(tmp))
        con.row_factory = sqlite3.Row
        existing = {i for (i,) in db.query(SearchDocument.id).filter(
            SearchDocument.vault_id.in_(_scope_vault_ids(db, replica))).all()}
        for row in con.execute("SELECT * FROM search_documents"):
            rid = row["id"]
            if rid in existing:
                skipped += 1
                continue
            kw = {c: row[c] for c in row.keys() if c not in ("modified_at", "created_at")}
            kw["is_current"] = str(kw.get("is_current")) in ("1", "True", "true")
            kw["size_bytes"] = int(kw.get("size_bytes") or 0)
            meta = kw.get("meta")
            if isinstance(meta, str) and meta:
                try:
                    kw["meta"] = _json.loads(meta)
                except ValueError:
                    kw["meta"] = {}
            for tcol in ("modified_at", "created_at"):
                val = row[tcol] if tcol in row.keys() else None
                if val:
                    try:
                        d = datetime.fromisoformat(val)
                        kw[tcol] = d.replace(tzinfo=None) if d.tzinfo else d
                    except ValueError:
                        pass
            db.add(SearchDocument(**kw))
            restored += 1
            if restored % 500 == 0:
                db.commit()
        con.close()
        db.commit()
    finally:
        tmp.unlink(missing_ok=True)
    logger.warning("index rebuild COMPLETE scope=%s:%s dest=%s — restored=%d skipped=%d",
                   replica.scope, replica.scope_id, replica.destination, restored, skipped)
    return {"restored": restored, "skipped": skipped,
            "scope": replica.scope, "scope_id": replica.scope_id,
            "destination": replica.destination}


def _scope_vault_ids(db, replica) -> list[str]:
    if replica.scope == "user":
        return [v.id for v in db.query(Vault).filter(
            Vault.tenant_id == replica.tenant_id, Vault.owner_user_id == replica.scope_id).all()]
    return [v.id for v in db.query(Vault).filter(Vault.tenant_id == replica.tenant_id).all()]


def replicate_due(force: bool = False) -> None:
    """Scheduler entry point. Interval-gated; a scope is skipped when its index
    signature is unchanged, so idle cycles are cheap."""
    global _last_run
    now = _now()
    if not force and _last_run and (now - _last_run).total_seconds() < REPLICATE_INTERVAL_SECONDS:
        return
    _last_run = now
    from . import status as worker_status
    worker_status.record("index-replication", state="running", message="replicating indexes")
    scopes_done = 0
    with SessionLocal() as db:
        node_id = _self_node_id(db)
        for scope in _scopes(db):
            try:
                replicate_scope(db, scope, node_id)
                scopes_done += 1
            except Exception:  # noqa: BLE001
                db.rollback()
                logger.exception("index replication failed for scope %s:%s",
                                 scope["scope"], scope["scope_id"])
    worker_status.record("index-replication", state="idle",
                         message=f"replicated {scopes_done} scope(s)", scopes=scopes_done)
