"""
Client-side (endpoint) encryption for the desktop agent.

Item content is encrypted on the Mac before it ever leaves the machine, so the
cloud never sees plaintext secrets. The agent holds a local data key (macOS
Keychain, with a 0600 file fallback) and escrows a copy wrapped to the vault's
recovery public key so authorized recovery is possible if the Mac is lost.
"""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

from cv_crypto.provider import get_provider
from cv_crypto.envelope import wrap_key

_KEYCHAIN_SERVICE = "com.arkive.agent"
_KEYCHAIN_ACCOUNT = "data-key"


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _keychain_get() -> bytes | None:
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE,
             "-a", _KEYCHAIN_ACCOUNT, "-w"],
            capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return base64.b64decode(out.stdout.strip())
    except Exception:
        pass
    return None


def _keychain_set(key: bytes) -> bool:
    try:
        r = subprocess.run(
            ["security", "add-generic-password", "-U", "-s", _KEYCHAIN_SERVICE,
             "-a", _KEYCHAIN_ACCOUNT, "-w", _b64(key)],
            capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def load_or_create_key(data_dir: Path) -> bytes:
    key = _keychain_get()
    if key:
        return key
    f = data_dir / "agent.key"
    if f.exists():
        return base64.b64decode(f.read_text())
    key = get_provider().random_key(32)
    if not _keychain_set(key):
        f.write_text(_b64(key))
        f.chmod(0o600)
    return key


def encrypt_content(agent_key: bytes, content: bytes, object_id: str) -> bytes:
    """Return a serialized client envelope (AES-256-GCM DEK, wrapped to the
    agent key). The cloud stores this opaquely and cannot read the plaintext."""
    p = get_provider()
    dek = p.random_key(32)
    nonce, ct = p.aes_encrypt(dek, content, object_id.encode())
    wn, wdek = p.aes_encrypt(agent_key, dek, b"agent-dek")
    envelope = {
        "v": 1, "alg": "AES-256-GCM", "objectId": object_id,
        "nonce": _b64(nonce), "ct": _b64(ct),
        "wrappedDek": {"nonce": _b64(wn), "ct": _b64(wdek)},
    }
    return json.dumps(envelope).encode()


def wrap_for_recovery(agent_key: bytes, recovery_pub_b64: str, kem_alg: str) -> dict:
    return wrap_key(agent_key, base64.b64decode(recovery_pub_b64), kem_alg)
