"""
Key broker (spec 14) and audit ledger.

The key broker manages per-tenant/per-vault root key material according to the
selected ownership model (spec 10). Production deployments back this with an HSM
or the customer's cloud key manager; the prototype persists wrapped root keys in
a local key store and exposes only the operations the control plane legitimately
needs. Operators never receive standing plaintext access (spec 3.1).
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Dict

from cv_crypto.provider import get_provider, hexdigest

_KEY_STORE = Path(os.environ.get("CV_KEY_STORE", "./cv_keystore"))
_KEY_STORE.mkdir(exist_ok=True)


def _path(vault_id: str) -> Path:
    return _KEY_STORE / f"{vault_id}.json"


def provision_vault_root_key(vault_id: str, ownership_model: str) -> Dict[str, object]:
    """Create and store a vault root key. For customer-managed / zero-knowledge
    models the plaintext is never returned to services after provisioning."""
    provider = get_provider()
    root = provider.random_key(32)
    # Wrap under a broker master key derived from environment secret (stand-in
    # for HSM-protected wrapping).
    master = provider.hkdf(
        (os.environ.get("CV_KEK_SECRET", "dev-kek") + vault_id).encode(),
        b"cv-broker-master",
        32,
    )
    nonce, ct = provider.aes_encrypt(master, root, b"vault-root")
    record = {
        "vaultId": vault_id,
        "ownershipModel": ownership_model,
        "wrapped": {"nonce": base64.b64encode(nonce).decode(),
                    "ct": base64.b64encode(ct).decode()},
        "rootKeyHash": hexdigest(root),
    }
    _path(vault_id).write_text(json.dumps(record))
    # Only zero-knowledge/customer-managed differ operationally; for demo we
    # return the plaintext once so the caller can hand it to the client. In
    # zero-knowledge mode this would happen entirely on the customer endpoint.
    return {"root_key": root, "record": record}


def release_vault_root_key(vault_id: str) -> bytes:
    """Release (unwrap) the vault root key for an authorized operation only.

    Zero-knowledge vaults would refuse this in production; here it powers the
    prototype restore/search flows for other ownership models.
    """
    record = json.loads(_path(vault_id).read_text())
    provider = get_provider()
    master = provider.hkdf(
        (os.environ.get("CV_KEK_SECRET", "dev-kek") + vault_id).encode(),
        b"cv-broker-master",
        32,
    )
    w = record["wrapped"]
    return provider.aes_decrypt(
        master, base64.b64decode(w["nonce"]), base64.b64decode(w["ct"]), b"vault-root"
    )


def _recovery_path(vault_id: str) -> Path:
    return _KEY_STORE / f"{vault_id}.recovery.json"


def provision_recovery_keypair(vault_id: str) -> Dict[str, str]:
    """Provision a KEM recovery keypair for endpoint (client-side) encryption.

    Agents wrap their local data key to this public key so escrowed content can
    be recovered by an authorized party. The private key is broker-wrapped (in a
    zero-knowledge deployment it would be customer-held instead)."""
    path = _recovery_path(vault_id)
    if path.exists():
        d = json.loads(path.read_text())
        return {"public_key": d["publicKey"], "kem_alg": d["kemAlg"]}
    provider = get_provider()
    kp = provider.kem_keypair("ML-KEM-768")
    master = provider.hkdf(
        (os.environ.get("CV_KEK_SECRET", "dev-kek") + vault_id).encode(),
        b"cv-recovery-master", 32)
    nonce, ct = provider.aes_encrypt(master, kp.private_key, b"recovery-priv")
    path.write_text(json.dumps({
        "vaultId": vault_id, "publicKey": base64.b64encode(kp.public_key).decode(),
        "kemAlg": kp.algorithm,
        "wrappedPriv": {"nonce": base64.b64encode(nonce).decode(),
                        "ct": base64.b64encode(ct).decode()},
    }))
    return {"public_key": base64.b64encode(kp.public_key).decode(), "kem_alg": kp.algorithm}


def release_recovery_private(vault_id: str) -> bytes:
    record = json.loads(_recovery_path(vault_id).read_text())
    provider = get_provider()
    master = provider.hkdf(
        (os.environ.get("CV_KEK_SECRET", "dev-kek") + vault_id).encode(),
        b"cv-recovery-master", 32)
    w = record["wrappedPriv"]
    return provider.aes_decrypt(
        master, base64.b64decode(w["nonce"]), base64.b64decode(w["ct"]), b"recovery-priv")
