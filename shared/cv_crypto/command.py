"""
Signed appliance command envelope (spec 5.2) and snapshot / seal receipts
(spec 6.1). Shared by the cloud control plane (issuer) and the appliance agent
(verifier / enforcer).

The appliance MUST reject commands that are expired, duplicated, out of
sequence, incorrectly signed, issued to another appliance, inconsistent with
local policy, missing approvals, broader than requested, or issued while
quarantined. This module provides the structure + signing; policy enforcement
lives on the appliance.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from .provider import hexdigest
from .signing import HybridSigner, HybridVerifier, SigPolicy


def _now() -> datetime:
    return datetime.now(timezone.utc)


def rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


COMMAND_TYPES = {
    "OPEN_INGEST_WINDOW",
    "OPEN_RECOVERY_WINDOW",
    "REQUEST_VERIFICATION",
    "SCHEDULE_BACKUP",
    "STAGE_UPDATE",
    "APPLY_UPDATE",
    "ROTATE_IDENTITY",
    "COLLECT_DIAGNOSTICS",
    "QUARANTINE",
    "SEAL",
}


def build_command(
    signer: HybridSigner,
    appliance_id: str,
    command_type: str,
    sequence: int,
    requested_by: str,
    parameters: dict,
    policy_hash: str,
    approvals: Optional[List[dict]] = None,
    ttl_seconds: int = 900,
) -> dict:
    if command_type not in COMMAND_TYPES:
        raise ValueError(f"unknown command type {command_type}")
    issued = _now()
    payload = {
        "commandId": str(uuid.uuid4()),
        "applianceId": appliance_id,
        "commandType": command_type,
        "issuedAt": rfc3339(issued),
        "notBefore": rfc3339(issued),
        "expiresAt": rfc3339(issued + timedelta(seconds=ttl_seconds)),
        "sequence": sequence,
        "requestedBy": requested_by,
        "approvalSet": approvals or [],
        "parameters": parameters,
        "policyHash": policy_hash,
    }
    envelope = signer.sign(payload)
    return {"payload": payload, "signature": envelope}


def verify_command(bundle: dict, command: dict, policy: SigPolicy = SigPolicy.REQUIRE_BOTH) -> bool:
    return HybridVerifier.from_bundle(bundle).verify(
        command["payload"], command["signature"], policy
    )


def build_snapshot_manifest(
    signer: HybridSigner,
    snapshot_id: str,
    vault_id: str,
    collection_id: str,
    objects: List[dict],
    retention_class: str,
) -> dict:
    total_bytes = sum(int(o.get("plaintextBytes", 0)) for o in objects)
    object_hashes = [
        {"objectId": o["objectId"], "ciphertextHash": hexdigest(str(o).encode())}
        for o in objects
    ]
    payload = {
        "snapshotId": snapshot_id,
        "vaultId": vault_id,
        "collectionId": collection_id,
        "objectCount": len(objects),
        "totalBytes": total_bytes,
        "retentionClass": retention_class,
        "objectHashes": object_hashes,
        "createdAt": rfc3339(_now()),
    }
    return {"payload": payload, "signature": signer.sign(payload)}


def build_seal_receipt(
    signer: HybridSigner,
    appliance_id: str,
    snapshot_id: str,
    manifest_hash: str,
    object_count: int,
    total_bytes: int,
    isolation_state: str,
    integrity_result: str,
) -> dict:
    payload = {
        "applianceId": appliance_id,
        "snapshotId": snapshot_id,
        "objectCount": object_count,
        "totalBytes": total_bytes,
        "manifestHash": manifest_hash,
        "commitTimestamp": rfc3339(_now()),
        "isolationState": isolation_state,
        "integrityResult": integrity_result,
    }
    return {"payload": payload, "signature": signer.sign(payload)}
