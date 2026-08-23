"""Tenant-scoped encryption for connector credentials/tokens at rest."""

from __future__ import annotations

import base64
import json
import os
import struct

from cv_crypto.provider import get_provider

# Framed backup format: AES-GCM caps a single operation at 2**31-1 bytes, so a
# large bundle is encrypted in independent chunks. A magic prefix distinguishes
# it from the legacy single-shot (nonce||ct) format for backward compatibility.
_BACKUP_MAGIC = b"CVBK1"
_BACKUP_CHUNK = 64 * 1024 * 1024  # 64 MiB plaintext per frame


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
    KEK for ``scope``. Large inputs are framed into per-chunk AES-GCM segments
    (single-shot AES-GCM is capped at 2**31-1 bytes). Returns a magic-prefixed
    stream of ``[4-byte len][nonce||ct]`` frames — decryptable by any node/CP that
    shares CV_KEK_SECRET, so a backup stays restorable if the box is lost."""
    provider = get_provider()
    key = _kek(scope)
    out = bytearray(_BACKUP_MAGIC)
    for index, off in enumerate(range(0, max(len(data), 1), _BACKUP_CHUNK)):
        chunk = data[off:off + _BACKUP_CHUNK]
        # Frame index in the AAD binds ordering so frames can't be swapped.
        nonce, ct = provider.aes_encrypt(key, chunk, b"backup" + struct.pack(">I", index))
        frame = nonce + ct
        out += struct.pack(">I", len(frame)) + frame
        if not data:  # empty input → single empty frame, then stop
            break
    return bytes(out)


def decrypt_bytes(scope: str, blob: bytes) -> bytes:
    provider = get_provider()
    key = _kek(scope)
    if blob[:len(_BACKUP_MAGIC)] == _BACKUP_MAGIC:
        out = bytearray()
        pos = len(_BACKUP_MAGIC)
        index = 0
        while pos < len(blob):
            (flen,) = struct.unpack_from(">I", blob, pos)
            pos += 4
            frame = blob[pos:pos + flen]
            pos += flen
            nonce, ct = frame[:12], frame[12:]
            out += provider.aes_decrypt(key, nonce, ct, b"backup" + struct.pack(">I", index))
            index += 1
        return bytes(out)
    # Legacy single-shot format (nonce||ct).
    nonce, ct = blob[:12], blob[12:]
    return provider.aes_decrypt(key, nonce, ct, b"backup")
