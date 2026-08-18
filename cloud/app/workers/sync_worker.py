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

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from cv_crypto.command import build_snapshot_manifest
from cv_crypto.envelope import EnvelopeKeyHierarchy, encrypt_object
from cv_crypto.provider import hexdigest
from cv_crypto.signing import HybridSigner

from .. import audit, credstore, keybroker, taxonomy
from ..connectors import get_connector
from ..connectors import oauth
from ..models import (
    Collection,
    ConnectorAccount,
    SearchDocument,
    SnapshotReceipt,
    Vault,
)
from ..storage import build_destination

# The cloud manifest signer (distinct from the fleet command signer).
from ..fleet import fleet_signer


def run_backup(db: Session, collection: Collection, destinations: Optional[List[str]] = None
               ) -> SnapshotReceipt:
    """Pull from the source connector and ingest into protected storage."""
    account = (
        db.get(ConnectorAccount, collection.connector_account_id)
        if collection.connector_account_id
        else None
    )
    connector = get_connector(collection.source_type)
    if connector is None:
        raise ValueError(f"no connector for {collection.source_type}")

    label = account.account_label if account else collection.name
    config = _account_config(db, collection, account)
    caps = connector.capabilities()
    objects = list(connector.fetch(label, config=config).objects)

    receipt = ingest_objects(db, collection, objects, destinations,
                             searchable_fields=caps.searchable_fields, actor="sync-worker")
    if account:
        account.last_sync_at = datetime.now(timezone.utc)
        db.commit()
    return receipt


def ingest_objects(db: Session, collection: Collection, source_objects,
                   destinations: Optional[List[str]] = None,
                   searchable_fields: Optional[List[str]] = None,
                   actor: str = "ingest") -> SnapshotReceipt:
    """Encrypt, snapshot, index, and store a set of normalized source objects.

    Shared by the cloud sync worker (connector pulls) and pushed ingest from the
    desktop agent.
    """
    vault = db.get(Vault, collection.vault_id)
    zero_knowledge = vault.key_ownership_model == "zero-knowledge"
    searchable_fields = searchable_fields or ["*"]

    root_key = keybroker.release_vault_root_key(vault.id)
    hierarchy = EnvelopeKeyHierarchy(root_key)
    snapshot_id = str(uuid.uuid4())
    snapshot_key = hierarchy.snapshot_key(vault.id, collection.id, snapshot_id)

    encrypted_objects = []
    index_rows: List[SearchDocument] = []
    total_bytes = 0

    for src in source_objects:
        enc = encrypt_object(snapshot_key, src.content, src.object_id)
        enc["plaintextBytes"] = src.size_bytes
        encrypted_objects.append(enc)
        total_bytes += src.size_bytes
        # Restricted categories (credentials, identity) never index derived
        # content — only the title and non-secret metadata.
        allow_preview = not zero_knowledge and taxonomy.index_preview(src.category)
        search_blob = src.searchable_text(searchable_fields) if allow_preview \
            else ("" if zero_knowledge else src.title)
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
                title=src.title,
                preview="" if not allow_preview else src.preview,
                meta={} if zero_knowledge else src.meta,
                labels=[] if zero_knowledge else src.labels,
                search_blob=search_blob,
                size_bytes=src.size_bytes,
                modified_at=src.modified_at,
            )
        )

    # Signed snapshot manifest (hybrid ML-DSA + Ed25519).
    manifest = build_snapshot_manifest(
        signer=fleet_signer(),
        snapshot_id=snapshot_id,
        vault_id=vault.id,
        collection_id=collection.id,
        objects=encrypted_objects,
        retention_class="standard",
    )
    manifest_hash = manifest["signature"]["payloadHash"]

    dest_kinds = destinations or ["cv-cloud"]
    tenant_prefix = _tenant_prefix(db, collection.tenant_id)
    last_receipt: Optional[SnapshotReceipt] = None

    for kind in dest_kinds:
        recoverable = False
        if kind in ("cv-cloud", "customer-s3"):
            dest = build_destination(kind)
            for obj in encrypted_objects:
                dest.put_object(tenant_prefix, f"{snapshot_id}/{obj['objectId']}",
                                obj["ciphertext"].encode())
            dest.put_manifest(tenant_prefix, snapshot_id, manifest)
            recoverable = True

        receipt = SnapshotReceipt(
            tenant_id=collection.tenant_id,
            vault_id=vault.id,
            collection_id=collection.id,
            snapshot_id=snapshot_id,
            destination=kind,
            object_count=len(encrypted_objects),
            total_bytes=total_bytes,
            manifest_hash=manifest_hash,
            recoverable=recoverable,
            receipt=manifest,
        )
        db.add(receipt)
        last_receipt = receipt

    for row in index_rows:
        db.add(row)
    db.commit()

    audit.record(
        db, actor=actor, action="backup.completed",
        tenant_id=collection.tenant_id, resource=collection.id,
        detail={"snapshotId": snapshot_id, "objects": len(encrypted_objects),
                "bytes": total_bytes, "destinations": dest_kinds},
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
