"""
Hybrid signing and verification (spec 9.3).

Critical artifacts (manifests, seal receipts, appliance command envelopes,
software-update metadata) carry BOTH a classical (Ed25519) and a post-quantum
(ML-DSA) signature. Validation policy is explicit:

- ``REQUIRE_BOTH``     both signatures must validate (default for privileged ops)
- ``REQUIRE_PQ``       the post-quantum signature must validate
- ``REQUIRE_ANY``      at least one validates (explicitly-managed migration only)

Fail-open behaviour is prohibited for current privileged commands
(spec 9.3): a privileged verify with a missing signature is a hard failure.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from enum import Enum
from typing import List

from .errors import SignatureError
from .profiles import CryptoProfile, default_profile
from .provider import CryptoProvider, get_provider, hexdigest


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _unb64(s: str) -> bytes:
    return base64.b64decode(s)


class SigPolicy(str, Enum):
    REQUIRE_BOTH = "require-both"
    REQUIRE_PQ = "require-pq"
    REQUIRE_ANY = "require-any"


@dataclass
class HybridSigner:
    """Holds a classical + post-quantum signing key for one identity."""

    profile: CryptoProfile
    classical_priv: bytes
    classical_pub: bytes
    pq_alg: str
    pq_priv: bytes
    pq_pub: bytes
    key_id: str
    provider: CryptoProvider = None  # type: ignore

    def __post_init__(self) -> None:
        self.provider = self.provider or get_provider()

    @classmethod
    def generate(cls, key_id: str, profile: CryptoProfile = None) -> "HybridSigner":
        profile = profile or default_profile("signature")
        p = get_provider()
        ck = p.ed25519_keypair()
        pk = p.pq_sig_keypair(profile.sig_pq)
        return cls(
            profile=profile,
            classical_priv=ck.private_key,
            classical_pub=ck.public_key,
            pq_alg=pk.algorithm,
            pq_priv=pk.private_key,
            pq_pub=pk.public_key,
            key_id=key_id,
        )

    def public_bundle(self) -> dict:
        return {
            "keyId": self.key_id,
            "profileId": self.profile.profile_id,
            "classicalAlg": "Ed25519",
            "classicalPub": _b64(self.classical_pub),
            "pqAlg": self.pq_alg,
            "pqPub": _b64(self.pq_pub),
        }

    def sign(self, payload: dict) -> dict:
        message, payload_hash = _canonical(payload, self.profile.hash_algo)
        c_sig = self.provider.ed25519_sign(self.classical_priv, message)
        p_sig = self.provider.pq_sign(self.pq_alg, self.pq_priv, message)
        return {
            "payloadHash": payload_hash,
            "hashAlg": self.profile.hash_algo,
            "profileId": self.profile.profile_id,
            "signatures": [
                {"algorithm": "Ed25519", "keyId": self.key_id, "signature": _b64(c_sig)},
                {"algorithm": self.pq_alg, "keyId": self.key_id, "signature": _b64(p_sig)},
            ],
        }


@dataclass
class HybridVerifier:
    classical_pub: bytes
    pq_alg: str
    pq_pub: bytes
    hash_algo: str = "SHA-384"
    provider: CryptoProvider = None  # type: ignore

    def __post_init__(self) -> None:
        self.provider = self.provider or get_provider()

    @classmethod
    def from_bundle(cls, bundle: dict) -> "HybridVerifier":
        return cls(
            classical_pub=_unb64(bundle["classicalPub"]),
            pq_alg=bundle["pqAlg"],
            pq_pub=_unb64(bundle["pqPub"]),
        )

    def verify(self, payload: dict, envelope: dict, policy: SigPolicy = SigPolicy.REQUIRE_BOTH) -> bool:
        message, payload_hash = _canonical(payload, envelope.get("hashAlg", self.hash_algo))
        if payload_hash != envelope.get("payloadHash"):
            raise SignatureError("payload hash mismatch")

        sigs = {s["algorithm"]: _unb64(s["signature"]) for s in envelope.get("signatures", [])}
        classical_ok = False
        pq_ok = False
        for alg, sig in sigs.items():
            if alg == "Ed25519":
                classical_ok = self.provider.ed25519_verify(self.classical_pub, message, sig)
            elif alg == self.pq_alg:
                pq_ok = self.provider.pq_verify(self.pq_alg, self.pq_pub, message, sig)

        if policy == SigPolicy.REQUIRE_BOTH:
            if not (classical_ok and pq_ok):
                raise SignatureError("hybrid policy require-both not satisfied")
            return True
        if policy == SigPolicy.REQUIRE_PQ:
            if not pq_ok:
                raise SignatureError("post-quantum signature required but invalid")
            return True
        if not (classical_ok or pq_ok):
            raise SignatureError("no valid signature present")
        return True


def _canonical(payload: dict, hash_algo: str):
    message = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return message, hexdigest(message, hash_algo)


def sign_payload(signer: HybridSigner, payload: dict) -> dict:
    return signer.sign(payload)


def verify_payload(bundle: dict, payload: dict, envelope: dict, policy: SigPolicy = SigPolicy.REQUIRE_BOTH) -> bool:
    return HybridVerifier.from_bundle(bundle).verify(payload, envelope, policy)
