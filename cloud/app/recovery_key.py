"""Vault Recovery Key — last-resort recovery credential (like a 1Password
Secret/Recovery Key).

A single high-entropy code the customer keeps offline. It wraps a copy of each
eligible vault's root key so that, if every passkey is lost or compromised, the
customer can prove identity with the code and restore access to their vault keys.
The plaintext code is shown ONCE at creation and is never stored — only a
verifier hash (to check the code) and the code-wrapped key copies live server
side.

ELIGIBILITY: split-control / customer-managed vaults only. Zero-knowledge vaults
are excluded — the server never holds unwrapped material for them, so it cannot
escrow a code-wrapped copy of the root key (there is nothing to recover to).
"""

from __future__ import annotations

import base64
import hmac
import logging
import secrets
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from cv_crypto.provider import get_provider, hexdigest

from . import keybroker
from .models import Tenant, User, Vault, VaultRecoveryKey

logger = logging.getLogger("cv.recoverykey")

# Crockford base32 without ambiguous characters (no I, L, O, U).
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_GROUPS = 6
_GROUP_LEN = 5  # 30 chars → ~150 bits of entropy
_PREFIX = "ARK1"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def generate_code() -> str:
    """A grouped, human-transcribable recovery code, e.g.
    ``ARK1-7QF3M-...`` (6 groups of 5)."""
    body = "".join(secrets.choice(_ALPHABET) for _ in range(_GROUPS * _GROUP_LEN))
    groups = [body[i:i + _GROUP_LEN] for i in range(0, len(body), _GROUP_LEN)]
    return f"{_PREFIX}-" + "-".join(groups)


def normalize_code(code: str) -> str:
    """Canonicalise user input: uppercase, drop separators/spaces, strip the
    prefix, and map look-alike characters onto the alphabet so a mis-typed
    O/0, I/1, L/1 or U/V still verifies."""
    raw = "".join(c for c in (code or "").upper() if c.isalnum())
    if raw.startswith(_PREFIX):
        raw = raw[len(_PREFIX):]
    return raw.translate(str.maketrans({"I": "1", "L": "1", "O": "0", "U": "V"}))


def _derive(code: str, salt: bytes) -> tuple[bytes, str]:
    """Derive (wrapping_key, verifier_hex) from the code + per-record salt."""
    provider = get_provider()
    ikm = normalize_code(code).encode() + salt
    kek = provider.hkdf(ikm, b"cv-recovery-code-kek", 32)
    verifier = hexdigest(provider.hkdf(ikm, b"cv-recovery-code-verify", 32))
    return kek, verifier


def eligible_vaults(db: Session, user: User, tenant: Tenant) -> list[Vault]:
    """The user's vaults that can be covered by a recovery key (not zero-knowledge)."""
    vaults = (db.query(Vault)
              .filter(Vault.tenant_id == tenant.id,
                      Vault.owner_user_id == user.id)
              .all())
    return [v for v in vaults if (v.key_ownership_model or "") != "zero-knowledge"]


def get_record(db: Session, user_id: str) -> VaultRecoveryKey | None:
    return (db.query(VaultRecoveryKey)
            .filter(VaultRecoveryKey.user_id == user_id).first())


def create(db: Session, user: User, tenant: Tenant) -> dict | None:
    """Generate a recovery key, wrap each eligible vault root key under it, and
    persist the record (replacing any existing one). Returns
    ``{code, hint, vault_count}`` with the plaintext code shown ONCE, or ``None``
    when there is no eligible vault to cover."""
    vaults = eligible_vaults(db, user, tenant)
    if not vaults:
        return None
    provider = get_provider()
    code = generate_code()
    salt = provider.random_key(16)
    kek, verifier = _derive(code, salt)
    wrapped: list[dict] = []
    for v in vaults:
        try:
            root = keybroker.release_vault_root_key(v.id)
        except Exception as exc:  # noqa: BLE001 — a missing key file just skips
            logger.warning("recovery key: skip vault %s (no root key: %s)", v.id, exc)
            continue
        nonce, ct = provider.aes_encrypt(kek, root, b"cv-recovery-root")
        wrapped.append({
            "vault_id": v.id,
            "nonce": base64.b64encode(nonce).decode(),
            "ct": base64.b64encode(ct).decode(),
        })
    if not wrapped:
        return None
    now = _now()
    hint = code[-4:]
    rec = get_record(db, user.id)
    if rec is not None:
        rec.salt = base64.b64encode(salt).decode()
        rec.verifier = verifier
        rec.hint = hint
        rec.wrapped_keys = wrapped
        rec.vault_count = len(wrapped)
        rec.rotated_at = now
        rec.last_used_at = None
    else:
        db.add(VaultRecoveryKey(
            user_id=user.id, tenant_id=tenant.id,
            salt=base64.b64encode(salt).decode(), verifier=verifier,
            hint=hint, wrapped_keys=wrapped, vault_count=len(wrapped),
        ))
    db.commit()
    logger.info("recovery key created for %s covering %d vault(s)",
                user.email or user.id, len(wrapped))
    return {"code": code, "hint": hint, "vault_count": len(wrapped)}


def verify(record: VaultRecoveryKey | None, code: str) -> bool:
    if not record:
        return False
    try:
        _, verifier = _derive(code, base64.b64decode(record.salt))
    except Exception:  # noqa: BLE001
        return False
    return hmac.compare_digest(verifier, record.verifier or "")


def redeem(db: Session, record: VaultRecoveryKey, code: str) -> list[str]:
    """Verify the code and restore each wrapped vault root key back under the
    fleet KEK (self-heal). Returns the restored vault_ids. The common case (all
    passkeys lost but the fleet-KEK key file intact) still succeeds — the rewrite
    is idempotent — and this also rebuilds the key file if it was truly lost."""
    if not verify(record, code):
        return []
    provider = get_provider()
    kek, _ = _derive(code, base64.b64decode(record.salt))
    restored: list[str] = []
    for w in (record.wrapped_keys or []):
        try:
            root = provider.aes_decrypt(
                kek, base64.b64decode(w["nonce"]), base64.b64decode(w["ct"]),
                b"cv-recovery-root")
        except Exception:  # noqa: BLE001
            continue
        vault = db.get(Vault, w.get("vault_id"))
        if vault is None:
            continue
        try:
            keybroker.import_root_key(
                vault.id, root, vault.key_ownership_model or "customer-managed")
        except Exception as exc:  # noqa: BLE001
            logger.warning("recovery redeem: could not restore vault %s: %s",
                           vault.id, exc)
        restored.append(w["vault_id"])
    record.last_used_at = _now()
    db.commit()
    logger.info("recovery key redeemed for user %s, restored %d vault(s)",
                record.user_id, len(restored))
    return restored


def status(db: Session, user: User, tenant: Tenant) -> dict:
    """Summary for the settings UI + the create-a-recovery-key prompt."""
    rec = get_record(db, user.id)
    elig = eligible_vaults(db, user, tenant)
    created = rec is not None
    return {
        "eligible": len(elig) > 0,
        "eligible_vaults": len(elig),
        "created": created,
        "created_at": rec.created_at.isoformat() if rec and rec.created_at else None,
        "rotated_at": rec.rotated_at.isoformat() if rec and rec.rotated_at else None,
        "last_used_at": rec.last_used_at.isoformat() if rec and rec.last_used_at else None,
        "hint": rec.hint if rec else "",
        "covered_vaults": rec.vault_count if rec else 0,
        # Prompt to create when eligible but none exists yet.
        "needs_prompt": len(elig) > 0 and not created,
        # New eligible vaults exist beyond what the current key covers.
        "stale": bool(rec and (rec.vault_count or 0) < len(elig)),
    }
