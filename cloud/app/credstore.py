"""Tenant-scoped encryption for connector credentials/tokens at rest."""

from __future__ import annotations

import base64
import json
import os

from cv_crypto.provider import get_provider


def _kek(tenant_id: str) -> bytes:
    return get_provider().hkdf(
        (os.environ.get("CV_KEK_SECRET", "dev-kek") + tenant_id).encode(),
        b"connector-cred", 32,
    )


def encrypt(tenant_id: str, data: dict) -> str:
    provider = get_provider()
    nonce, ct = provider.aes_encrypt(_kek(tenant_id), json.dumps(data).encode(), b"cred")
    return base64.b64encode(nonce + ct).decode()


def decrypt(tenant_id: str, blob: str) -> dict:
    provider = get_provider()
    raw = base64.b64decode(blob)
    nonce, ct = raw[:12], raw[12:]
    return json.loads(provider.aes_decrypt(_kek(tenant_id), nonce, ct, b"cred"))


def encrypt_bytes(scope: str, data: bytes) -> bytes:
    """Encrypt raw bytes (e.g. an infrastructure backup archive) under the fleet
    KEK for ``scope``. Returns nonce||ciphertext — decryptable by any node/CP that
    shares CV_KEK_SECRET, so a backup stays restorable if the box is lost."""
    provider = get_provider()
    nonce, ct = provider.aes_encrypt(_kek(scope), data, b"backup")
    return nonce + ct


def decrypt_bytes(scope: str, blob: bytes) -> bytes:
    provider = get_provider()
    nonce, ct = blob[:12], blob[12:]
    return provider.aes_decrypt(_kek(scope), nonce, ct, b"backup")
