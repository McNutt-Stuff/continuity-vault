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

from . import audit, keybroker
from .connectors import get_connector
from .models import (
    Collection,
    ConnectorAccount,
    SearchDocument,
    SnapshotReceipt,
    Vault,
)
from .storage import build_destination

# The cloud manifest signer (distinct from the fleet command signer).
from .fleet import fleet_signer


def run_backup(db: Session, collection: Collection, destinations: Optional[List[str]] = None
               ) -> SnapshotReceipt:
    vault = db.get(Vault, collection.vault_id)
    account = (
        db.get(ConnectorAccount, collection.connector_account_id)
        if collection.connector_account_id
        else None
    )
    connector = get_connector(collection.source_type)
    if connector is None:
        raise ValueError(f"no connector for {collection.source_type}")

    label = account.account_label if account else collection.name
    zero_knowledge = vault.key_ownership_model == "zero-knowledge"

    # Release the vault root key for encryption (or derive on endpoint in ZK).
    root_key = keybroker.release_vault_root_key(vault.id)
    hierarchy = EnvelopeKeyHierarchy(root_key)
    snapshot_id = str(uuid.uuid4())
    snapshot_key = hierarchy.snapshot_key(vault.id, collection.id, snapshot_id)

    encrypted_objects = []
    index_rows: List[SearchDocument] = []
    total_bytes = 0

    caps = connector.capabilities()
    for src in connector.fetch(label).objects:
        enc = encrypt_object(snapshot_key, src.content, src.object_id)
        enc["plaintextBytes"] = src.size_bytes
        encrypted_objects.append(enc)
        total_bytes += src.size_bytes
        # Build the searchable blob from title + preview + connector-declared
        # searchable metadata fields; suppressed entirely for zero-knowledge.
        search_blob = "" if zero_knowledge else src.searchable_text(caps.searchable_fields)
        index_rows.append(
            SearchDocument(
                tenant_id=collection.tenant_id,
                vault_id=vault.id,
                collection_id=collection.id,
                snapshot_id=snapshot_id,
                object_id=src.object_id,
                source_type=collection.source_type,
                doc_type=src.doc_type,
                title=src.title,
                preview="" if zero_knowledge else src.preview,
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
        # Appliance destinations are handled by the appliance agent via signed
        # commands; here we record the pending receipt and the fleet manager
        # opens an ingest window. Cloud/customer-s3 write immediately.
        recoverable = False
        if kind in ("cv-cloud", "customer-s3"):
            dest = build_destination(kind)
            for obj in encrypted_objects:
                dest.put_object(tenant_prefix, f"{snapshot_id}/{obj['objectId']}",
                                obj["ciphertext"].encode())
            dest.put_manifest(tenant_prefix, snapshot_id, manifest)
            recoverable = True  # destination confirmed commit

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

    account and setattr(account, "last_sync_at", datetime.now(timezone.utc))
    db.commit()

    audit.record(
        db, actor="sync-worker", action="backup.completed",
        tenant_id=collection.tenant_id, resource=collection.id,
        detail={"snapshotId": snapshot_id, "objects": len(encrypted_objects),
                "bytes": total_bytes, "destinations": dest_kinds},
    )
    if last_receipt:
        db.refresh(last_receipt)
    return last_receipt


def _tenant_prefix(db: Session, tenant_id: str) -> str:
    from .models import Tenant

    tenant = db.get(Tenant, tenant_id)
    return tenant.storage_prefix if tenant else tenant_id
