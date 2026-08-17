"""
CryptoProvider: the single abstraction layer for all cryptographic primitives
(spec 9.7 / LLM build instruction 11 & 12 — never implement primitives manually).

Classical primitives use the well-reviewed `cryptography` library. Post-quantum
primitives (ML-KEM, ML-DSA, SLH-DSA) use liboqs via the `oqs` python bindings
when available. If liboqs is not installed, the provider degrades to a clearly
flagged software fallback (HKDF-based KEM stand-in and HMAC "signatures") so the
prototype remains runnable end to end. `pq_available` reports the honest state
and callers/telemetry surface it — the fallback is NEVER presented as quantum
safe.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional, Tuple

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes, serialization

from .errors import CryptoError

try:  # pragma: no cover - depends on host
    import oqs  # type: ignore

    _OQS_AVAILABLE = True
except Exception:  # pragma: no cover
    oqs = None  # type: ignore
    _OQS_AVAILABLE = False


HASHES = {
    "SHA-384": hashes.SHA384,
    "SHA-512": hashes.SHA512,
    "SHA-256": hashes.SHA256,
}


def digest(data: bytes, algo: str = "SHA-384") -> bytes:
    h = hashlib.new(_hashlib_name(algo))
    h.update(data)
    return h.digest()


def hexdigest(data: bytes, algo: str = "SHA-384") -> str:
    return digest(data, algo).hex()


def _hashlib_name(algo: str) -> str:
    return {"SHA-384": "sha384", "SHA-512": "sha512", "SHA-256": "sha256"}[algo]


@dataclass
class KeyPair:
    algorithm: str
    public_key: bytes
    private_key: bytes
    pq: bool


class CryptoProvider:
    """Facade over classical and post-quantum primitives."""

    def __init__(self) -> None:
        self.pq_available = _OQS_AVAILABLE

    # -- Symmetric content encryption (AES-256-GCM) -----------------------

    def aes_encrypt(
        self, key: bytes, plaintext: bytes, aad: Optional[bytes] = None
    ) -> Tuple[bytes, bytes]:
        if len(key) != 32:
            raise CryptoError("AES-256-GCM requires a 32-byte key")
        nonce = os.urandom(12)
        ct = AESGCM(key).encrypt(nonce, plaintext, aad)
        return nonce, ct

    def aes_decrypt(
        self, key: bytes, nonce: bytes, ciphertext: bytes, aad: Optional[bytes] = None
    ) -> bytes:
        return AESGCM(key).decrypt(nonce, ciphertext, aad)

    def random_key(self, size: int = 32) -> bytes:
        return os.urandom(size)

    def hkdf(self, ikm: bytes, info: bytes, length: int = 32, salt: bytes = b"") -> bytes:
        return HKDF(
            algorithm=hashes.SHA384(),
            length=length,
            salt=salt or None,
            info=info,
        ).derive(ikm)

    # -- Classical key establishment (X25519) -----------------------------

    def x25519_keypair(self) -> KeyPair:
        sk = X25519PrivateKey.generate()
        return KeyPair(
            algorithm="X25519",
            public_key=sk.public_key().public_bytes_raw(),
            private_key=sk.private_bytes_raw(),
            pq=False,
        )

    def x25519_shared(self, private_key: bytes, peer_public: bytes) -> bytes:
        sk = X25519PrivateKey.from_private_bytes(private_key)
        return sk.exchange(X25519PublicKey.from_public_bytes(peer_public))

    # -- Post-quantum KEM (ML-KEM-768) ------------------------------------

    def kem_keypair(self, alg: str = "ML-KEM-768") -> KeyPair:
        if self.pq_available:
            oqs_name = _oqs_kem_name(alg)
            with oqs.KeyEncapsulation(oqs_name) as kem:  # type: ignore
                pk = kem.generate_keypair()
                sk = kem.export_secret_key()
            return KeyPair(algorithm=alg, public_key=pk, private_key=sk, pq=True)
        # Fallback: X25519 stand-in, clearly non-PQ.
        kp = self.x25519_keypair()
        return KeyPair(
            algorithm=f"{alg}!fallback-x25519", public_key=kp.public_key,
            private_key=kp.private_key, pq=False,
        )

    def kem_encapsulate(self, alg: str, public_key: bytes) -> Tuple[bytes, bytes]:
        """Return (ciphertext, shared_secret)."""
        if self.pq_available and "!fallback" not in alg:
            oqs_name = _oqs_kem_name(alg)
            with oqs.KeyEncapsulation(oqs_name) as kem:  # type: ignore
                ct, ss = kem.encap_secret(public_key)
            return ct, ss
        # Fallback ephemeral X25519 encapsulation.
        eph = self.x25519_keypair()
        shared = self.x25519_shared(eph.private_key, public_key)
        ss = self.hkdf(shared, b"cv-kem-fallback", 32)
        return eph.public_key, ss

    def kem_decapsulate(self, alg: str, private_key: bytes, ciphertext: bytes) -> bytes:
        if self.pq_available and "!fallback" not in alg:
            oqs_name = _oqs_kem_name(alg)
            with oqs.KeyEncapsulation(oqs_name, secret_key=private_key) as kem:  # type: ignore
                return kem.decap_secret(ciphertext)
        shared = self.x25519_shared(private_key, ciphertext)
        return self.hkdf(shared, b"cv-kem-fallback", 32)

    # -- Classical signatures (Ed25519) -----------------------------------

    def ed25519_keypair(self) -> KeyPair:
        sk = Ed25519PrivateKey.generate()
        return KeyPair(
            algorithm="Ed25519",
            public_key=sk.public_key().public_bytes_raw(),
            private_key=sk.private_bytes_raw(),
            pq=False,
        )

    def ed25519_sign(self, private_key: bytes, message: bytes) -> bytes:
        return Ed25519PrivateKey.from_private_bytes(private_key).sign(message)

    def ed25519_verify(self, public_key: bytes, message: bytes, signature: bytes) -> bool:
        try:
            Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
            return True
        except Exception:
            return False

    # -- Post-quantum signatures (ML-DSA / SLH-DSA) -----------------------

    def pq_sig_keypair(self, alg: str = "ML-DSA-65") -> KeyPair:
        if self.pq_available:
            oqs_name = _oqs_sig_name(alg)
            with oqs.Signature(oqs_name) as sig:  # type: ignore
                pk = sig.generate_keypair()
                sk = sig.export_secret_key()
            return KeyPair(algorithm=alg, public_key=pk, private_key=sk, pq=True)
        # Fallback: HMAC keypair (symmetric) flagged as non-PQ.
        seed = os.urandom(32)
        return KeyPair(
            algorithm=f"{alg}!fallback-hmac", public_key=seed, private_key=seed, pq=False
        )

    def pq_sign(self, alg: str, private_key: bytes, message: bytes) -> bytes:
        if self.pq_available and "!fallback" not in alg:
            oqs_name = _oqs_sig_name(alg)
            with oqs.Signature(oqs_name, secret_key=private_key) as sig:  # type: ignore
                return sig.sign(message)
        return hmac.new(private_key, message, hashlib.sha384).digest()

    def pq_verify(self, alg: str, public_key: bytes, message: bytes, signature: bytes) -> bool:
        if self.pq_available and "!fallback" not in alg:
            oqs_name = _oqs_sig_name(alg)
            with oqs.Signature(oqs_name) as sig:  # type: ignore
                try:
                    return bool(sig.verify(message, signature, public_key))
                except Exception:
                    return False
        expected = hmac.new(public_key, message, hashlib.sha384).digest()
        return hmac.compare_digest(expected, signature)


def _oqs_kem_name(alg: str) -> str:
    return {
        "ML-KEM-768": "ML-KEM-768",
        "ML-KEM-1024": "ML-KEM-1024",
    }.get(alg, alg)


def _oqs_sig_name(alg: str) -> str:
    return {
        "ML-DSA-65": "ML-DSA-65",
        "ML-DSA-87": "ML-DSA-87",
        "SLH-DSA-SHA2-128s": "SPHINCS+-SHA2-128s-simple",
    }.get(alg, alg)


@lru_cache(maxsize=1)
def get_provider() -> CryptoProvider:
    return CryptoProvider()
