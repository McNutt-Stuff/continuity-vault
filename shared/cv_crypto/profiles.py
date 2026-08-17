"""
Crypto-profile registry (spec 9.7).

Every encrypted object, signature, key wrap, and manifest references a
CryptoProfile by id. The profile carries the algorithm identifiers, parameter
sets, lifecycle status, and migration metadata required for crypto-agility.

Business logic must reference profiles by id and MUST NOT hard-code algorithm
names, so the platform can migrate algorithms without rewriting application
code or losing access to historical backups.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date
from enum import Enum
from typing import Dict, Optional

from .errors import ProfileError


class AlgorithmStatus(str, Enum):
    EXPERIMENTAL = "experimental"
    APPROVED = "approved"
    PREFERRED = "preferred"
    LEGACY_VERIFY_ONLY = "legacy-verify-only"
    DEPRECATED = "deprecated"
    PROHIBITED = "prohibited"


@dataclass(frozen=True)
class CryptoProfile:
    """A versioned, self-describing cryptographic profile."""

    profile_id: str
    purpose: str  # transport | content | signature | keywrap
    # Content encryption
    content_algo: str = "AES-256-GCM"
    # Key establishment / encapsulation (hybrid)
    kem_classical: str = "X25519"
    kem_pq: str = "ML-KEM-768"
    # Signatures (hybrid)
    sig_classical: str = "Ed25519"
    sig_pq: str = "ML-DSA-65"
    # Integrity
    hash_algo: str = "SHA-384"
    # Password derivation
    kdf: str = "Argon2id"
    status: AlgorithmStatus = AlgorithmStatus.PREFERRED
    introduced_at: str = "2026-01-01"
    deprecated_at: Optional[str] = None
    disallowed_at: Optional[str] = None
    minimum_verification_date: Optional[str] = None
    migration_policy: str = "hybrid-required"
    library_provider: str = "liboqs+cryptography"
    hardware_support: bool = False

    def as_dict(self) -> Dict[str, object]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    def assert_usable_for_new_data(self) -> None:
        if self.status in (
            AlgorithmStatus.LEGACY_VERIFY_ONLY,
            AlgorithmStatus.DEPRECATED,
            AlgorithmStatus.PROHIBITED,
        ):
            raise ProfileError(
                f"Profile {self.profile_id} has status {self.status.value} and "
                "cannot be used to protect new data."
            )


# --- Registry ---------------------------------------------------------------

PROFILE_REGISTRY: Dict[str, CryptoProfile] = {}


def register_profile(profile: CryptoProfile) -> CryptoProfile:
    PROFILE_REGISTRY[profile.profile_id] = profile
    return profile


# Default operational profiles (spec 9.2 defaults + higher-assurance).
register_profile(
    CryptoProfile(
        profile_id="cvp-hybrid-2026a",
        purpose="content",
        content_algo="AES-256-GCM",
        kem_classical="X25519",
        kem_pq="ML-KEM-768",
        sig_classical="Ed25519",
        sig_pq="ML-DSA-65",
        hash_algo="SHA-384",
        status=AlgorithmStatus.PREFERRED,
    )
)

register_profile(
    CryptoProfile(
        profile_id="cvp-hybrid-high-2026a",
        purpose="content",
        content_algo="AES-256-GCM",
        kem_classical="X448",
        kem_pq="ML-KEM-1024",
        sig_classical="Ed25519",
        sig_pq="ML-DSA-87",
        hash_algo="SHA-512",
        status=AlgorithmStatus.APPROVED,
        migration_policy="hybrid-required",
    )
)

register_profile(
    CryptoProfile(
        profile_id="cvp-transport-2026a",
        purpose="transport",
        kem_classical="X25519",
        kem_pq="ML-KEM-768",
        sig_classical="Ed25519",
        sig_pq="ML-DSA-65",
        hash_algo="SHA-384",
        status=AlgorithmStatus.PREFERRED,
    )
)

# Long-lived archival signature profile with an optional stateless-hash signature
register_profile(
    CryptoProfile(
        profile_id="cvp-archival-sig-2026a",
        purpose="signature",
        sig_classical="Ed25519",
        sig_pq="SLH-DSA-SHA2-128s",
        hash_algo="SHA-512",
        status=AlgorithmStatus.APPROVED,
        migration_policy="archival-longlived",
    )
)


def get_profile(profile_id: str) -> CryptoProfile:
    try:
        return PROFILE_REGISTRY[profile_id]
    except KeyError as exc:
        raise ProfileError(f"Unknown crypto profile: {profile_id}") from exc


def default_profile(purpose: str = "content") -> CryptoProfile:
    if purpose == "transport":
        return PROFILE_REGISTRY["cvp-transport-2026a"]
    if purpose == "signature":
        return PROFILE_REGISTRY["cvp-archival-sig-2026a"]
    return PROFILE_REGISTRY["cvp-hybrid-2026a"]
