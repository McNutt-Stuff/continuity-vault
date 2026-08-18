"""
Appliance fleet manager (spec 5, 14).

Holds the cloud control-plane hybrid signing identity, issues signed/sequenced/
expiring command envelopes, verifies appliance attestation and signed receipts,
and enforces the separation between management-plane commands and content
access. The cloud signer public bundle is distributed to appliances at linking
time so they can verify every command locally (spec 5.1).
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from cv_crypto.command import build_command
from cv_crypto.provider import hexdigest
from cv_crypto.signing import HybridSigner, HybridVerifier, SigPolicy

from .models import Appliance, ApplianceCommand

_SIGNER_PATH = Path(os.environ.get("CV_FLEET_SIGNER", "./cv_fleet_signer.json"))


def _load_or_create_signer() -> HybridSigner:
    if _SIGNER_PATH.exists():
        data = json.loads(_SIGNER_PATH.read_text())
        from cv_crypto.profiles import get_profile

        return HybridSigner(
            profile=get_profile(data["profileId"]),
            classical_priv=base64.b64decode(data["classicalPriv"]),
            classical_pub=base64.b64decode(data["classicalPub"]),
            pq_alg=data["pqAlg"],
            pq_priv=base64.b64decode(data["pqPriv"]),
            pq_pub=base64.b64decode(data["pqPub"]),
            key_id=data["keyId"],
        )
    signer = HybridSigner.generate("cloud-control-plane")
    _SIGNER_PATH.write_text(
        json.dumps(
            {
                "keyId": signer.key_id,
                "profileId": signer.profile.profile_id,
                "classicalPriv": base64.b64encode(signer.classical_priv).decode(),
                "classicalPub": base64.b64encode(signer.classical_pub).decode(),
                "pqAlg": signer.pq_alg,
                "pqPriv": base64.b64encode(signer.pq_priv).decode(),
                "pqPub": base64.b64encode(signer.pq_pub).decode(),
            }
        )
    )
    return signer


_FLEET_SIGNER: Optional[HybridSigner] = None


def fleet_signer() -> HybridSigner:
    global _FLEET_SIGNER
    if _FLEET_SIGNER is None:
        _FLEET_SIGNER = _load_or_create_signer()
    return _FLEET_SIGNER


def cloud_public_bundle() -> dict:
    return fleet_signer().public_bundle()


def policy_hash(appliance: Appliance) -> str:
    """A deterministic hash of the local-policy view the command must match.

    The appliance independently re-derives this from its own local policy and
    rejects any command whose policyHash does not match (spec 5.2)."""
    policy = {
        "applianceId": appliance.id,
        "retentionFloorDays": 365,
        "immutability": True,
        "allowIngest": appliance.state in ("SEALED", "ONLINE_STAGING", "READY_TO_SEAL"),
    }
    return hexdigest(json.dumps(policy, sort_keys=True).encode())


def issue_command(db: Session, appliance: Appliance, command_type: str,
                  requested_by: str, parameters: dict,
                  approvals: Optional[list] = None) -> ApplianceCommand:
    appliance.command_sequence = (appliance.command_sequence or 0) + 1
    envelope = build_command(
        signer=fleet_signer(),
        appliance_id=appliance.id,
        command_type=command_type,
        sequence=appliance.command_sequence,
        requested_by=requested_by,
        parameters=parameters,
        policy_hash=policy_hash(appliance),
        approvals=approvals,
    )
    cmd = ApplianceCommand(
        tenant_id=appliance.tenant_id,
        appliance_id=appliance.id,
        command_type=command_type,
        sequence=appliance.command_sequence,
        envelope=envelope,
        requested_by=requested_by,
        status="pending",
    )
    # Key the row by the signed envelope's commandId so the appliance's
    # command-result (which echoes payload.commandId) resolves to this row.
    envelope_cmd_id = envelope.get("payload", {}).get("commandId")
    if envelope_cmd_id:
        cmd.id = envelope_cmd_id
    db.add(cmd)
    db.commit()
    db.refresh(cmd)
    return cmd


def verify_appliance_receipt(appliance: Appliance, payload: dict, signature: dict,
                             policy: SigPolicy = SigPolicy.REQUIRE_BOTH) -> bool:
    """Verify a signed seal/attestation receipt using the appliance's own
    identity bundle registered at linking time."""
    if not appliance.identity_bundle:
        return False
    return HybridVerifier.from_bundle(appliance.identity_bundle).verify(
        payload, signature, policy
    )
