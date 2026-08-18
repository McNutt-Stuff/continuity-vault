"""
Short-lived, single-use email verification codes (bootstrap / recovery factor).

Codes are 6-digit, hashed at rest in memory, expire quickly, are single-use,
rate-limited per address, and locked out after too many wrong attempts. Single
worker in-memory store is sufficient for the prototype; back with a shared cache
for multi-worker deployments.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from .config import get_settings

settings = get_settings()

_RESEND_INTERVAL = 20
_MAX_ATTEMPTS = 5


class RateLimited(Exception):
    pass


@dataclass
class _Entry:
    code_hash: str
    purpose: str
    expires_at: float
    last_sent: float
    attempts: int = 0


_store: Dict[str, _Entry] = {}


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def issue_code(email: str, purpose: str) -> str:
    now = time.time()
    existing = _store.get(email)
    if existing and now - existing.last_sent < _RESEND_INTERVAL:
        raise RateLimited("please wait before requesting another code")
    code = f"{secrets.randbelow(1_000_000):06d}"
    _store[email] = _Entry(
        code_hash=_hash(code),
        purpose=purpose,
        expires_at=now + settings.email_code_ttl_seconds,
        last_sent=now,
    )
    return code


def verify_code(email: str, code: str, purpose: str) -> bool:
    entry = _store.get(email)
    if not entry or entry.purpose != purpose or time.time() > entry.expires_at:
        return False
    entry.attempts += 1
    if entry.attempts > _MAX_ATTEMPTS:
        _store.pop(email, None)
        return False
    if hmac.compare_digest(entry.code_hash, _hash(code)):
        _store.pop(email, None)
        return True
    return False
