"""
Arkive shared cryptography package.

Implements the crypto-agile, hybrid (classical + post-quantum) provider model
described in the product specification (sections 9 and 17):

- AES-256-GCM authenticated content encryption
- Hybrid key establishment (X25519 + ML-KEM-768)
- Hybrid signatures (Ed25519 + ML-DSA-65)
- Layered envelope encryption (root -> vault -> collection -> snapshot -> object)
- A versioned CryptoProfile registry so algorithms can change without rewriting
  business logic (spec 2.4, 9.7).

Post-quantum primitives are provided through liboqs (python `oqs`) when present.
When liboqs is unavailable (developer laptops, CI), a clearly-flagged software
fallback is used so the end-to-end prototype still runs. The fallback NEVER
claims to be quantum-safe: `CryptoProvider.pq_available` reports the true state
and every artifact records the concrete algorithm actually used.
"""

from .profiles import CryptoProfile, PROFILE_REGISTRY, default_profile
from .provider import CryptoProvider, get_provider
from .envelope import (
    EnvelopeKeyHierarchy,
    encrypt_object,
    decrypt_object,
    wrap_key,
    unwrap_key,
)
from .signing import HybridSigner, HybridVerifier, sign_payload, verify_payload
from .errors import CryptoError, SignatureError, ProfileError

__all__ = [
    "CryptoProfile",
    "PROFILE_REGISTRY",
    "default_profile",
    "CryptoProvider",
    "get_provider",
    "EnvelopeKeyHierarchy",
    "encrypt_object",
    "decrypt_object",
    "wrap_key",
    "unwrap_key",
    "HybridSigner",
    "HybridVerifier",
    "sign_payload",
    "verify_payload",
    "CryptoError",
    "SignatureError",
    "ProfileError",
]

__version__ = "0.1.0"
