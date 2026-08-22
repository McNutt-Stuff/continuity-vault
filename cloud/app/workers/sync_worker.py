"""
Sync worker (spec 6): runs a backup for a collection.

Pipeline:
  connector.fetch_objects
    -> envelope-encrypt each object under the snapshot key
    -> write encrypted objects to the target ProtectionDestination(s)
    -> build a hybrid-signed snapshot manifest
    -> record a (not-yet-recoverable) SnapshotReceipt
    -> index policy-permitted metadata for unified search
    -> mark recoverable once the destination confirms commit (spec build-instr 18)

Content is encrypted before it enters any destination. The worker only ever
handles metadata previews permitted by the vault's key-ownership mode
(zero-knowledge vaults index title-only).
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Callable, List, Optional

from sqlalchemy.orm import Session

from cv_crypto.command import build_snapshot_manifest
from cv_crypto.envelope import EnvelopeKeyHierarchy, encrypt_object
from cv_crypto.provider import hexdigest
from cv_crypto.signing import HybridSigner

from .. import audit, credstore, fleet, keybroker
from ..connectors import get_connector
from ..connectors import oauth
from ..models import (
    Appliance,
    ApplianceStorage,
    Collection,
    ConnectorAccount,
    ObjectVersion,
    SearchDocument,
    SnapshotReceipt,
    Vault,
)
from ..storage import build_destination

# The cloud manifest signer (distinct from the fleet command signer).
from ..fleet import fleet_signer
from ..config import get_settings

logger = logging.getLogger("cv.sync")


class JobCancelled(Exception):
    """Raised (via the progress callback) when an operator cancels a running
    backup job so it aborts cleanly without being recorded as a source failure."""


def _is_auth_error(exc: Exception) -> bool:
    """Heuristic: does this failure mean the source needs re-authorization?"""
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) in (401, 403):
        return True
    s = str(exc).lower()
    tokens = ("401", "403", "invalid_grant", "invalid_token", "unauthorized",
              "forbidden", "token has expired", "token expired", "reauth",
              "access_denied", "revoked", "needs-reauth", "invalid credentials")
    return any(t in s for t in tokens)


def _record_sync_success(db: Session, account: Optional[ConnectorAccount], count: int) -> None:
    """Mark a source healthy after a successful sync (clears any prior error)."""
    if account is None:
        return
    account.last_sync_at = datetime.now(timezone.utc)
    account.last_object_count = int(count)
    account.last_error = None
    account.last_error_at = None
    if account.auth_status == "needs-reauth":
        account.auth_status = "linked"
    db.commit()
    logger.info("sync ok: source=%s account=%s captured=%d",
                getattr(account, "connector_type", "?"), account.account_label, count)


def _try_refresh_token(db: Session, account: ConnectorAccount) -> bool:
    """Force an OAuth token refresh (used after an auth failure). Returns True if a
    new access token was minted — the next run then recovers without a full re-auth."""
    if not account.encrypted_credentials:
        return False
    try:
        creds = credstore.decrypt(account.tenant_id, account.encrypted_credentials)
    except Exception:
        return False
    rt = creds.get("refresh_token")
    if not rt:
        return False
    try:
        creds.update(oauth.refresh_tokens(account.connector_type, rt))
        account.encrypted_credentials = credstore.encrypt(account.tenant_id, creds)
        account.auth_status = "linked"
        return True
    except Exception as exc:  # noqa: BLE001
        logger.info("token refresh after auth error failed for %s: %s",
                    account.account_label, exc)
        return False


def _record_sync_error(db: Session, account: Optional[ConnectorAccount],
                       collection: Collection, exc: Exception) -> None:
    """Persist a source's failure + emit an audit event; flag re-auth needs so
    the UI can offer a Reconnect button and the overview can warn."""
    if account is None:
        return
    msg = (str(exc)[:500] or exc.__class__.__name__)
    needs_auth = _is_auth_error(exc)
    # A 401 can mean the access token was invalidated (re-consent, clock skew,
    # a fresh grant on another node) while the refresh token still works — try a
    # refresh before demanding a full re-authorization.
    if needs_auth and _try_refresh_token(db, account):
        needs_auth = False
    account.last_error = msg
    account.last_error_at = datetime.now(timezone.utc)
    if needs_auth:
        account.auth_status = "needs-reauth"
    try:
        db.commit()
    except Exception:
        db.rollback()
    logger.error("sync failed: source=%s account=%s reauth=%s error=%s",
                 collection.source_type, account.account_label, needs_auth, msg)
    try:
        audit.record(
            db, actor="sync-worker",
            action="connector.reauth_required" if needs_auth else "connector.sync_failed",
            tenant_id=collection.tenant_id, resource=account.id,
            category="connector", severity="warning",
            detail={"type": collection.source_type, "account": account.account_label,
                    "error": msg, "needs_reauth": needs_auth})
    except Exception:
        db.rollback()


def _encrypt_content_units(snapshot_key: bytes, content: bytes, object_id: str,
                           chunk_size: int) -> list:
    """Encrypt one source object into storage units. Small content becomes a
    single envelope stored under ``object_id``; large content becomes per-chunk
    envelopes (``object_id#pN``) plus a small index stored under ``object_id``."""
    if len(content) <= chunk_size:
        enc = encrypt_object(snapshot_key, content, object_id)
        enc["plaintextBytes"] = len(content)
        return [enc]
    units: list = []
    parts: list = []
    for i in range(0, len(content), chunk_size):
        part = content[i:i + chunk_size]
        pid = f"{object_id}#p{i // chunk_size}"
        penc = encrypt_object(snapshot_key, part, pid)
        penc["plaintextBytes"] = len(part)
        units.append(penc)
        parts.append({"objectId": pid, "bytes": len(part)})
    # Index object (stored under the logical object id) drives reassembly.
    units.append({"objectId": object_id, "chunked": True, "parts": parts,
                  "plaintextBytes": 0})
    return units


# Nice, human labels for common discrete metadata keys shown in search.
_META_LABELS = {
    "from": "From", "to": "To", "folder": "Folder", "labels": "Labels",
    "tags": "Tags", "vault": "Vault", "kind": "Type", "url": "URL",
    "username": "Username", "path": "Path", "mime": "Type", "party": "Party",
    "sender": "From", "recipient": "To", "account": "Account",
}


def _pretty_key(key: str) -> str:
    return _META_LABELS.get(key, key.replace("_", " ").capitalize())


def _fmt_value(value) -> str:
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(v) for v in value if v not in (None, ""))
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _discrete_metadata(meta: dict, display_keys: List[str]) -> dict:
    """Keep only the connector-declared discrete keys that have a value. When a
    connector declares no fields, index nothing derived (title-only search)."""
    out: dict = {}
    for k in display_keys:
        v = (meta or {}).get(k)
        if v in (None, "", [], {}):
            continue
        out[k] = v
    return out


def _flatten_values(meta: dict) -> List[str]:
    parts: List[str] = []
    for v in (meta or {}).values():
        if isinstance(v, (list, tuple, set)):
            parts.extend(str(x) for x in v if x not in (None, ""))
        elif isinstance(v, dict):
            parts.extend(f"{k} {val}" for k, val in v.items())
        elif v not in (None, ""):
            parts.append(str(v))
    return parts


def _compose_preview(meta: dict, max_fields: int = 4) -> str:
    """A compact, non-content summary like 'From: a@b.com · Folder: Inbox'."""
    bits = [f"{_pretty_key(k)}: {_fmt_value(v)}"
            for k, v in list(meta.items())[:max_fields]]
    return " · ".join(bits)


def _resolve_appliance(db: Session, tenant_id: str, kind: str) -> Optional[Appliance]:
    """Resolve a destination to a live appliance. Accepts the canonical
    ``store:<storageId>`` form, the legacy ``appliance:<id>`` form, and the bare
    ``appliance`` (most-recent sealed unit)."""
    if kind.startswith("store:"):
        store = db.get(ApplianceStorage, kind.split(":", 1)[1])
        if not store or store.tenant_id != tenant_id:
            return None
        a = db.get(Appliance, store.appliance_id)
        return a if a and a.tenant_id == tenant_id else None
    if ":" in kind:
        aid = kind.split(":", 1)[1]
        a = db.get(Appliance, aid)
        return a if a and a.tenant_id == tenant_id else None
    return (db.query(Appliance)
            .filter(Appliance.tenant_id == tenant_id,
                    Appliance.state.in_(["SEALED", "ONLINE_STAGING", "READY_TO_SEAL"]))
            .order_by(Appliance.last_heartbeat_at.desc())
            .first())


def _storage_id(kind: str) -> Optional[str]:
    return kind.split(":", 1)[1] if kind.startswith("store:") else None


def crawl_has_more(db: Session, collection: Collection) -> bool:
    """True when a big-history source (e.g. Google Photos) still has more to crawl
    — its persisted cursor reports has_more. Lets a background job loop chunk by
    chunk until the whole library is captured."""
    if not collection.connector_account_id:
        return False
    acct = db.get(ConnectorAccount, collection.connector_account_id)
    cur = acct.sync_cursor if acct else None
    return bool(isinstance(cur, dict) and cur.get("has_more"))


def access_token_for_account(db: Session, account: ConnectorAccount) -> Optional[str]:
    """Decrypt an account's OAuth credentials, refreshing an expired access token.
    Returns a usable access token (or None). Used by interactive flows (Picker)."""
    import time
    if not account or not account.encrypted_credentials:
        return None
    try:
        creds = credstore.decrypt(account.tenant_id, account.encrypted_credentials)
    except Exception:
        return None
    if (creds.get("access_token") and creds.get("expires_at", 0) < time.time()
            and creds.get("refresh_token")):
        try:
            creds.update(oauth.refresh_tokens(account.connector_type, creds["refresh_token"]))
            account.encrypted_credentials = credstore.encrypt(account.tenant_id, creds)
            account.auth_status = "linked"
            db.commit()
        except Exception:
            account.auth_status = "needs-reauth"
            db.commit()
    return creds.get("access_token")


def existing_object_ids(db: Session, collection_id: str) -> set:
    """Object ids already backed up for a collection — used to skip re-importing
    items the user re-selects in a picker session."""
    rows = (db.query(SearchDocument.object_id)
            .filter(SearchDocument.collection_id == collection_id).distinct().all())
    return {r[0] for r in rows}


def run_backup(db: Session, collection: Collection, destinations: Optional[List[str]] = None,
               progress: Optional[Callable[[int, int, str], None]] = None
               ) -> SnapshotReceipt:
    """Pull from the source connector and ingest into protected storage.

    ``progress(processed, total, message)`` is called at milestones so a tracked
    job can show live status for long pulls (e.g. a full Gmail backup)."""
    account = (
        db.get(ConnectorAccount, collection.connector_account_id)
        if collection.connector_account_id
        else None
    )
    connector = get_connector(collection.source_type)
    if connector is None:
        raise ValueError(f"no connector for {collection.source_type}")

    # Default to the mapping's own destinations so scheduled/auto runs honour the
    # Data Map routing (e.g. an appliance) instead of falling back to the cloud.
    if destinations is None:
        destinations = collection.destinations or ["cv-cloud"]

    label = account.account_label if account else collection.name
    if progress:
        progress(0, 0, f"Fetching from {label}…")
    logger.debug("sync start: source=%s collection=%s label=%s",
                 collection.source_type, collection.id, label)
    try:
        config = _account_config(db, collection, account)
        # Fold the mapping's own settings (e.g. Gmail folder exclusions) into the
        # connector config alongside the credentials.
        if collection.config:
            config = {**config, **collection.config}
        caps = connector.capabilities()
        # Media-heavy sources (Facebook/Instagram photos, etc.) stream: ingest in
        # bounded batches so a large library can't materialize into memory and OOM.
        if caps.streaming:
            return _run_backup_streaming(db, collection, account, connector, config,
                                         destinations, caps, label, progress)
        # Incremental: pass the stored cursor; the connector returns a new cursor to
        # persist (full first backup, then deltas since the last sync).
        result = connector.fetch(label, cursor=(account.sync_cursor if account else None),
                                 config=config)
        objects = list(result.objects)
        logger.debug("sync fetched %d object(s): source=%s", len(objects), collection.source_type)

        # A delta run with nothing new: advance the cursor, don't create an empty
        # recovery point. (First-ever run still ingests to establish a baseline.)
        if not objects and account is not None:
            prior = (db.query(SnapshotReceipt)
                     .filter(SnapshotReceipt.collection_id == collection.id)
                     .order_by(SnapshotReceipt.created_at.desc()).first())
            if prior is not None:
                if result.cursor is not None:
                    account.sync_cursor = result.cursor
                _record_sync_success(db, account, 0)
                if progress:
                    progress(0, 0, "No new items")
                return prior

        if progress:
            progress(0, len(objects), f"Encrypting & storing {len(objects)} items…")
        receipt = ingest_objects(db, collection, objects, destinations,
                                 searchable_fields=caps.searchable_fields,
                                 facet_fields=caps.facet_fields, actor="sync-worker",
                                 progress=progress)
        if account:
            if result.cursor is not None:
                account.sync_cursor = result.cursor
            _record_sync_success(db, account, len(objects))
        return receipt
    except JobCancelled:
        raise  # operator cancellation — not a source failure
    except Exception as exc:
        _record_sync_error(db, account, collection, exc)
        raise


def _run_backup_streaming(db: Session, collection: Collection,
                          account: Optional[ConnectorAccount], connector, config: dict,
                          destinations: List[str], caps, label: str,
                          progress: Optional[Callable[[int, int, str], None]]
                          ) -> SnapshotReceipt:
    """Pull a content-heavy source lazily and ingest in bounded batches so memory
    stays flat regardless of library/mailbox size (each batch = one recovery
    point). ``state['cursor']`` (set by the connector) is persisted for deltas."""
    batch_bytes_cap = 64 * 1024 * 1024  # flush a batch at ~64 MiB of content
    batch_count_cap = 50
    batch: List = []
    batch_bytes = 0
    total = 0
    last_receipt: Optional[SnapshotReceipt] = None
    state: dict = {}
    if progress:
        progress(0, 0, f"Fetching from {label}…")

    def flush():
        nonlocal batch, batch_bytes, last_receipt
        if not batch:
            return
        last_receipt = ingest_objects(db, collection, batch, destinations,
                                      searchable_fields=caps.searchable_fields,
                                      facet_fields=caps.facet_fields, actor="sync-worker")
        batch = []
        batch_bytes = 0

    cursor = account.sync_cursor if account else None
    for obj in connector.fetch_stream(label, cursor=cursor, config=config, state=state):
        batch.append(obj)
        batch_bytes += len(getattr(obj, "content", b"") or b"")
        total += 1
        if len(batch) >= batch_count_cap or batch_bytes >= batch_bytes_cap:
            flush()
            if progress:
                progress(total, total, f"Encrypted & stored {total} items…")
    flush()
    new_cursor = state.get("cursor")

    if total == 0:
        prior = (db.query(SnapshotReceipt)
                 .filter(SnapshotReceipt.collection_id == collection.id)
                 .order_by(SnapshotReceipt.created_at.desc()).first())
        if prior is not None:
            if account:
                if new_cursor is not None:
                    account.sync_cursor = new_cursor
                _record_sync_success(db, account, 0)
            return prior
    if last_receipt is None:  # first run with nothing pulled — establish a baseline
        last_receipt = ingest_objects(db, collection, [], destinations,
                                      searchable_fields=caps.searchable_fields,
                                      facet_fields=caps.facet_fields, actor="sync-worker")
    if account:
        if new_cursor is not None:
            account.sync_cursor = new_cursor
        _record_sync_success(db, account, total)
    return last_receipt


def ingest_objects(db: Session, collection: Collection, source_objects,
                   destinations: Optional[List[str]] = None,
                   searchable_fields: Optional[List[str]] = None,
                   facet_fields: Optional[List[str]] = None,
                   actor: str = "ingest",
                   progress: Optional[Callable[[int, int, str], None]] = None) -> SnapshotReceipt:
    """Encrypt, snapshot, index, and store a set of normalized source objects.

    Shared by the cloud sync worker (connector pulls) and pushed ingest from the
    desktop agent.
    """
    vault = db.get(Vault, collection.vault_id)
    zero_knowledge = vault.key_ownership_model == "zero-knowledge"
    # Only the discrete metadata fields the connector declares are indexed or
    # shown in search — never the object's body/content. A per-source override
    # (collection.index_fields, set in the Data Map) wins when present; otherwise
    # facet_fields come first (most identifying), then extra searchable keys.
    override = list(collection.index_fields or [])
    display_keys: List[str] = []
    source_keys = override if override else [*(facet_fields or []), *(searchable_fields or [])]
    for k in source_keys:
        if k and k != "*" and k not in display_keys:
            display_keys.append(k)

    root_key = keybroker.release_vault_root_key(vault.id)
    hierarchy = EnvelopeKeyHierarchy(root_key)
    snapshot_id = str(uuid.uuid4())
    snapshot_key = hierarchy.snapshot_key(vault.id, collection.id, snapshot_id)
    chunk_size = get_settings().content_chunk_bytes

    storage_units: list = []  # envelopes to persist (single, or chunks + index)
    index_rows: List[SearchDocument] = []
    new_versions: List[ObjectVersion] = []
    total_bytes = 0

    src_list = list(source_objects)
    # Content-addressed versioning: look up the current version of each object so
    # identical re-collections are de-duplicated and only real changes create a
    # new version. This is pipeline-standard for every source.
    oids = list({s.object_id for s in src_list})
    current_versions: dict = {}
    if oids:
        for ov in (db.query(ObjectVersion)
                   .filter(ObjectVersion.tenant_id == collection.tenant_id,
                           ObjectVersion.source_type == collection.source_type,
                           ObjectVersion.object_id.in_(oids),
                           ObjectVersion.is_current.is_(True)).all()):
            current_versions[ov.object_id] = ov
    # Backfill baseline (v1) for objects that were first backed up before version
    # tracking existed: use their latest existing index row (which points at a real
    # stored snapshot). Without this, the first change to a legacy object would be
    # recorded as v1 and show no history.
    prior_docs: dict = {}
    missing = [o for o in oids if o not in current_versions]
    if missing:
        for d in (db.query(SearchDocument)
                  .filter(SearchDocument.tenant_id == collection.tenant_id,
                          SearchDocument.source_type == collection.source_type,
                          SearchDocument.object_id.in_(missing))
                  .order_by(SearchDocument.created_at.asc()).all()):
            prior_docs[d.object_id] = d  # asc → keeps the newest existing row

    stored = 0
    deduped = 0
    for idx, src in enumerate(src_list):
        # Prefer a client-supplied plaintext hash (agents encrypt with a fresh
        # nonce each run, so the ciphertext hash is never stable); otherwise hash
        # the content directly (connector plaintext is stable).
        content_hash = src.content_hash or hashlib.sha256(src.content or b"").hexdigest()
        prev = current_versions.get(src.object_id)
        if prev is None:
            pd = prior_docs.get(src.object_id)
            if pd is not None and pd.snapshot_id:
                prev = ObjectVersion(
                    tenant_id=collection.tenant_id, source_type=collection.source_type,
                    object_id=src.object_id, collection_id=collection.id, version=1,
                    content_hash=pd.content_hash or "", snapshot_id=pd.snapshot_id,
                    size_bytes=pd.size_bytes or 0, is_current=True,
                    created_at=pd.created_at,
                )
                new_versions.append(prev)
                current_versions[src.object_id] = prev
        if progress and (idx % 25 == 0):
            progress(idx, len(src_list), f"Processing {idx}/{len(src_list)}…")
        # Unchanged since the last version → de-duplicate (don't re-store or
        # re-index). The existing version and its bytes stand.
        if prev is not None and prev.content_hash == content_hash:
            deduped += 1
            # Backfill the corrected object timestamp onto the existing index rows
            # without re-storing content — repairs rows that recorded the ingest
            # time before per-source dates were parsed. Naive-UTC dates make this
            # a no-op once corrected, so it doesn't churn on every run.
            if src.modified_at is not None:
                db.query(SearchDocument).filter(
                    SearchDocument.tenant_id == collection.tenant_id,
                    SearchDocument.collection_id == collection.id,
                    SearchDocument.object_id == src.object_id,
                    SearchDocument.modified_at != src.modified_at,
                ).update({SearchDocument.modified_at: src.modified_at},
                         synchronize_session=False)
            continue

        # New object, or content changed → record a new immutable version.
        version = (prev.version + 1) if prev is not None else 1
        if prev is not None:
            prev.is_current = False
        new_versions.append(ObjectVersion(
            tenant_id=collection.tenant_id, source_type=collection.source_type,
            object_id=src.object_id, collection_id=collection.id, version=version,
            content_hash=content_hash, snapshot_id=snapshot_id,
            size_bytes=len(src.content or b""), is_current=True,
        ))
        # Large content is split into encrypted chunks at rest; small content is a
        # single envelope. Either way the item stays one logical object in search.
        storage_units.extend(
            _encrypt_content_units(snapshot_key, src.content, src.object_id, chunk_size))
        total_bytes += len(src.content)
        stored += 1
        # Index only discrete, connector-declared metadata — no body/content. The
        # preview is a composed "Field: value" summary of that metadata (empty for
        # zero-knowledge vaults, which index the title only).
        if zero_knowledge:
            discrete_meta: dict = {}
            preview = ""
            search_blob = ""
        else:
            discrete_meta = _discrete_metadata(src.meta, display_keys)
            preview = _compose_preview(discrete_meta)
            # Coerce every part to str — some connectors (e.g. Gmail) can surface
            # non-str header objects, which would break the join.
            search_blob = " ".join(
                str(x) for x in [src.title, *(src.labels or []), *_flatten_values(discrete_meta)]
                if x is not None
            ).strip()
        index_rows.append(
            SearchDocument(
                tenant_id=collection.tenant_id,
                vault_id=vault.id,
                collection_id=collection.id,
                snapshot_id=snapshot_id,
                object_id=src.object_id,
                source_type=collection.source_type,
                doc_type=src.doc_type,
                category=src.category,
                title=str(src.title) if src.title is not None else "",
                preview=preview,
                meta=discrete_meta,
                labels=[] if zero_knowledge else (src.labels or []),
                search_blob=search_blob,
                size_bytes=src.size_bytes,
                modified_at=src.modified_at,
                content_hash=content_hash,
                version=version,
            )
        )

    n_total = stored
    # Nothing changed since the last run — no new recovery point to create.
    if not storage_units:
        logger.info("ingest snapshot skipped: %d object(s) unchanged (deduped) for collection %s",
                    deduped, collection.id)
        prior = (db.query(SnapshotReceipt)
                 .filter(SnapshotReceipt.collection_id == collection.id)
                 .order_by(SnapshotReceipt.created_at.desc()).first())
        db.commit()  # persist any is_current flips (none here) — safe no-op
        return prior

    # Signed snapshot manifest (hybrid ML-DSA + Ed25519).
    manifest = build_snapshot_manifest(
        signer=fleet_signer(),
        snapshot_id=snapshot_id,
        vault_id=vault.id,
        collection_id=collection.id,
        objects=storage_units,
        retention_class="standard",
    )
    manifest_hash = manifest["signature"]["payloadHash"]

    dest_kinds = destinations or ["cv-cloud"]
    logger.info("ingest snapshot=%s collection=%s (%s) source=%s → destinations=%s",
                snapshot_id, collection.id, collection.name, collection.source_type, dest_kinds)
    tenant_prefix = _tenant_prefix(db, collection.tenant_id)
    last_receipt: Optional[SnapshotReceipt] = None
    succeeded: List[str] = []
    errors: dict = {}

    for kind in dest_kinds:
        recoverable = False
        receipt_appliance_id: Optional[str] = None
        # Each destination is attempted independently: one failing target (e.g. an
        # offline appliance) must not prevent the others (e.g. the cloud copy) from
        # landing. We only raise if *every* destination fails.
        try:
            if kind in ("cv-cloud", "customer-s3"):
                dest = build_destination(kind)
                for obj in storage_units:
                    # Store the full envelope (nonce + wrapped DEK + ciphertext) so
                    # the content can be decrypted on retrieval — not just the ct.
                    dest.put_object(tenant_prefix, f"{snapshot_id}/{obj['objectId']}",
                                    json.dumps(obj).encode())
                dest.put_manifest(tenant_prefix, snapshot_id, manifest)
                recoverable = True
            elif kind == "appliance" or kind.startswith("appliance:") or kind.startswith("store:"):
                # Hand the (already-encrypted) objects to the appliance via a
                # signed, sequenced OPEN_INGEST_WINDOW command. The appliance
                # commits them and returns a seal receipt (marking the snapshot
                # recoverable). Until then the receipt is not-yet-recoverable.
                appliance = _resolve_appliance(db, collection.tenant_id, kind)
                if appliance is None:
                    raise ValueError(
                        f"no linked appliance available for destination '{kind}' "
                        "(appliance offline, quarantined, or not sealed)")
                receipt_appliance_id = appliance.id
                fleet.issue_command(
                    db, appliance, "OPEN_INGEST_WINDOW", actor,
                    {
                        "snapshotId": snapshot_id,
                        "vaultId": vault.id,
                        "collectionId": collection.id,
                        "storageId": _storage_id(kind),
                        # Full envelopes (incl. nonce + wrapped DEK, and chunk
                        # indexes) so the appliance can store & later decrypt.
                        "objects": storage_units,
                        "objectCount": n_total,
                    },
                )
            else:
                raise ValueError(f"unknown destination '{kind}'")
        except Exception as exc:  # noqa: BLE001 - recorded per-destination
            logger.warning("destination %s failed for snapshot %s: %s",
                           kind, snapshot_id, exc)
            errors[kind] = str(exc)
            continue

        receipt = SnapshotReceipt(
            tenant_id=collection.tenant_id,
            vault_id=vault.id,
            collection_id=collection.id,
            snapshot_id=snapshot_id,
            destination=kind,
            appliance_id=receipt_appliance_id,
            object_count=n_total,
            total_bytes=total_bytes,
            manifest_hash=manifest_hash,
            recoverable=recoverable,
            receipt=manifest,
        )
        db.add(receipt)
        last_receipt = receipt
        succeeded.append(kind)

    if not succeeded:
        # Nothing landed anywhere — surface the failure to the caller.
        db.rollback()
        raise RuntimeError("backup failed for all destinations: "
                           + "; ".join(f"{k}: {v}" for k, v in errors.items()))

    for ov in new_versions:
        db.add(ov)
    for row in index_rows:
        db.add(row)
    db.commit()

    audit.record(
        db, actor=actor, action="backup.completed",
        tenant_id=collection.tenant_id, resource=collection.id,
        detail={"snapshotId": snapshot_id, "objects": n_total, "deduped": deduped,
                "bytes": total_bytes, "destinations": succeeded,
                "failed": errors or None},
    )
    if last_receipt:
        db.refresh(last_receipt)
    return last_receipt


def _tenant_prefix(db: Session, tenant_id: str) -> str:
    from ..models import Tenant

    tenant = db.get(Tenant, tenant_id)
    return tenant.storage_prefix if tenant else tenant_id


def _account_config(db: Session, collection: Collection,
                    account: Optional[ConnectorAccount]) -> dict:
    """Decrypt an account's credentials and refresh an expired OAuth token."""
    import time

    if not account or not account.encrypted_credentials:
        return {}
    try:
        creds = credstore.decrypt(collection.tenant_id, account.encrypted_credentials)
    except Exception:
        return {}
    # Credential access is security-relevant — record it in the audit ledger.
    audit.record(db, actor="sync-worker", action="connector.credentials_accessed",
                 tenant_id=collection.tenant_id, resource=account.id,
                 detail={"type": collection.source_type, "account": account.account_label})
    # Refresh the OAuth access token if it is expired and we have a refresh token.
    if creds.get("access_token") and creds.get("expires_at", 0) < time.time():
        if creds.get("refresh_token"):
            try:
                creds.update(oauth.refresh_tokens(collection.source_type,
                                                  creds["refresh_token"]))
                account.encrypted_credentials = credstore.encrypt(
                    collection.tenant_id, creds)
                account.auth_status = "linked"
                db.commit()
            except Exception:
                account.auth_status = "needs-reauth"
                db.commit()
    return creds
