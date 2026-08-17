"""
Layered envelope encryption (spec 9.4).

Key hierarchy:

    Customer Root Key
          -> Vault Key
             -> Collection Key
                -> Snapshot Key
                   -> Object / Chunk Data-Encryption Key (DEK)
                      -> AES-256-GCM encrypted content

Long-lived keys (vault / collection) can be additionally wrapped for multiple
recovery recipients using ML-KEM encapsulation (spec 9.4 "multiple recovery
recipients", 10.x key-ownership models). Every wrapped key and ciphertext
records the crypto profile and algorithm actually used, enabling key rewrapping
without bulk re-encryption (spec 9.7).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .errors import KeyWrapError
from .profiles import CryptoProfile, default_profile
from .provider import CryptoProvider, get_provider


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _unb64(s: str) -> bytes:
    return base64.b64decode(s)


@dataclass
class EnvelopeKeyHierarchy:
    """Derives the deterministic per-layer keys for a vault (prototype model).

    In production the root key lives in an HSM / customer key manager and only
    wrapped material transits services. For the prototype we derive layer keys
    from the root via HKDF with domain separation, which preserves the layered
    structure and lets us demonstrate rewrapping and multi-recipient recovery.
    """

    root_key: bytes
    provider: CryptoProvider = field(default=None)  # type: ignore

    def __post_init__(self) -> None:
        self.provider = self.provider or get_provider()

    def vault_key(self, vault_id: str) -> bytes:
        return self.provider.hkdf(self.root_key, f"vault:{vault_id}".encode(), 32)

    def collection_key(self, vault_id: str, collection_id: str) -> bytes:
        return self.provider.hkdf(
            self.vault_key(vault_id), f"collection:{collection_id}".encode(), 32
        )

    def snapshot_key(self, vault_id: str, collection_id: str, snapshot_id: str) -> bytes:
        return self.provider.hkdf(
            self.collection_key(vault_id, collection_id),
            f"snapshot:{snapshot_id}".encode(),
            32,
        )


def encrypt_object(
    snapshot_key: bytes,
    plaintext: bytes,
    object_id: str,
    profile: Optional[CryptoProfile] = None,
) -> Dict[str, object]:
    """Encrypt one object under a fresh DEK wrapped by the snapshot key."""
    profile = profile or default_profile("content")
    profile.assert_usable_for_new_data()
    p = get_provider()

    dek = p.random_key(32)
    aad = object_id.encode()
    nonce, ct = p.aes_encrypt(dek, plaintext, aad)

    # Wrap the DEK under the snapshot key with AES-GCM key wrap.
    wnonce, wdek = p.aes_encrypt(snapshot_key, dek, b"dek:" + aad)

    return {
        "objectId": object_id,
        "profileId": profile.profile_id,
        "contentAlgo": profile.content_algo,
        "nonce": _b64(nonce),
        "ciphertext": _b64(ct),
        "wrappedDek": {"nonce": _b64(wnonce), "ct": _b64(wdek)},
        "plaintextBytes": len(plaintext),
    }


def decrypt_object(snapshot_key: bytes, obj: Dict[str, object]) -> bytes:
    p = get_provider()
    object_id = obj["objectId"]  # type: ignore
    aad = str(object_id).encode()
    wrapped = obj["wrappedDek"]  # type: ignore
    dek = p.aes_decrypt(
        snapshot_key, _unb64(wrapped["nonce"]), _unb64(wrapped["ct"]), b"dek:" + aad
    )
    return p.aes_decrypt(dek, _unb64(obj["nonce"]), _unb64(obj["ciphertext"]), aad)  # type: ignore


def wrap_key(
    key: bytes,
    recipient_kem_pub: bytes,
    kem_alg: str = "ML-KEM-768",
    profile: Optional[CryptoProfile] = None,
) -> Dict[str, object]:
    """Wrap a long-lived key for a recovery recipient using ML-KEM encapsulation.

    ML-KEM is used only to establish a symmetric wrapping key (spec 9.2 —
    do not use ML-KEM directly for bulk encryption).
    """
    profile = profile or default_profile("content")
    p = get_provider()
    kem_ct, shared = p.kem_encapsulate(kem_alg, recipient_kem_pub)
    wrap_kek = p.hkdf(shared, b"cv-recipient-wrap", 32)
    nonce, ct = p.aes_encrypt(wrap_kek, key, b"recipient-key-wrap")
    return {
        "kemAlg": kem_alg,
        "profileId": profile.profile_id,
        "kemCiphertext": _b64(kem_ct),
        "nonce": _b64(nonce),
        "ct": _b64(ct),
    }


def unwrap_key(wrapped: Dict[str, object], recipient_kem_priv: bytes) -> bytes:
    p = get_provider()
    shared = p.kem_decapsulate(
        str(wrapped["kemAlg"]), recipient_kem_priv, _unb64(wrapped["kemCiphertext"])  # type: ignore
    )
    wrap_kek = p.hkdf(shared, b"cv-recipient-wrap", 32)
    try:
        return p.aes_decrypt(
            wrap_kek, _unb64(wrapped["nonce"]), _unb64(wrapped["ct"]), b"recipient-key-wrap"  # type: ignore
        )
    except Exception as exc:
        raise KeyWrapError("failed to unwrap recipient key") from exc
