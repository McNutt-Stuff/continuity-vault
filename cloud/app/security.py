"""
Authentication, session, tenant isolation, and passkey handling.

Passkeys (WebAuthn / hardware tokens) unlock portal interfaces and authorize
sensitive data-access operations (spec 15 + user requirement: keys/passkeys/
hardware tokens unlock the interfaces where users access data).

For the prototype the WebAuthn ceremony is simulated with a challenge/response
using an Ed25519 key held by the browser client, which exercises the same
server-side flow (challenge issuance, signature verification, sign-count) that a
production authenticator would drive. The API is structured so a real
`webauthn` library can replace the simulation without changing callers.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Header, HTTPException, status
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from cv_crypto.provider import get_provider

from .config import get_settings
from .db import get_db
from .models import Passkey, Tenant, User, Vault

settings = get_settings()
_serializer = URLSafeTimedSerializer(settings.session_secret, salt="cv-session")

# In-memory challenge store (prototype). Production: short-lived cache/redis.
_challenges: dict[str, tuple[str, float]] = {}


def issue_challenge(user_id: str) -> str:
    challenge = base64.urlsafe_b64encode(os.urandom(32)).decode()
    _challenges[user_id] = (challenge, time.time() + 300)
    return challenge


def take_challenge(user_id: str) -> Optional[str]:
    entry = _challenges.pop(user_id, None)
    if not entry:
        return None
    challenge, expiry = entry
    if time.time() > expiry:
        return None
    return challenge


def register_passkey(db: Session, user: User, credential_id: str, public_key_b64: str,
                     label: str, transport: str) -> Passkey:
    pk = Passkey(
        user_id=user.id,
        credential_id=credential_id,
        public_key=public_key_b64,
        label=label,
        transport=transport,
    )
    db.add(pk)
    db.commit()
    db.refresh(pk)
    return pk


def verify_passkey_assertion(db: Session, user: User, credential_id: str,
                             challenge: str, signature_b64: str) -> bool:
    pk = (
        db.query(Passkey)
        .filter(Passkey.user_id == user.id, Passkey.credential_id == credential_id)
        .first()
    )
    if not pk:
        return False
    provider = get_provider()
    ok = provider.ed25519_verify(
        base64.b64decode(pk.public_key),
        challenge.encode(),
        base64.b64decode(signature_b64),
    )
    if ok:
        pk.sign_count += 1
        db.commit()
    return ok


# -- Sessions ---------------------------------------------------------------


def create_session_token(user: User, passkey_verified: bool) -> str:
    return _serializer.dumps(
        {
            "uid": user.id,
            "tid": user.tenant_id,
            "role": user.role,
            "admin": user.is_platform_admin,
            "pk": passkey_verified,
        }
    )


@dataclass
class Principal:
    user_id: str
    tenant_id: str
    role: str
    is_platform_admin: bool
    passkey_verified: bool


def _decode(token: str) -> Principal:
    try:
        data = _serializer.loads(token, max_age=settings.session_ttl_seconds)
    except BadSignature:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid session")
    return Principal(
        user_id=data["uid"],
        tenant_id=data["tid"],
        role=data["role"],
        is_platform_admin=data["admin"],
        passkey_verified=data.get("pk", False),
    )


def get_principal(authorization: str = Header(default="")) -> Principal:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    return _decode(authorization.split(" ", 1)[1])


def require_passkey(principal: Principal = Depends(get_principal)) -> Principal:
    """Gate sensitive data-access interfaces behind a verified passkey."""
    if not principal.passkey_verified:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "passkey / hardware-token verification required for this interface",
        )
    return principal


def require_platform_admin(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.is_platform_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "platform admin required")
    return principal


def require_security_admin(principal: Principal = Depends(get_principal)) -> Principal:
    if not (is_org_admin(principal.role) or principal.is_platform_admin):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "security-admin role required")
    return principal


# -- Organization roles & data partitioning ---------------------------------
#
# Customer-facing roles: owner (full control + billing), admin (manage users,
# appliances, keys and see org-wide *aggregate* statistics), member (own data
# only). "security-admin" is the legacy name for an org admin.

ORG_ADMIN_ROLES = {"owner", "admin", "security-admin"}


def is_org_admin(role: str) -> bool:
    return role in ORG_ADMIN_ROLES


def is_owner(role: str) -> bool:
    return role == "owner"


def get_user(principal: Principal = Depends(get_principal),
             db: Session = Depends(get_db)) -> User:
    user = db.get(User, principal.user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    return user


def require_org_admin(principal: Principal = Depends(get_principal)) -> Principal:
    """Gate customer-facing Organization Admin functions (owner or admin)."""
    if not (is_org_admin(principal.role) or principal.is_platform_admin):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "organization admin required")
    return principal


def require_owner(principal: Principal = Depends(get_principal)) -> Principal:
    if not (is_owner(principal.role) or principal.is_platform_admin):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "organization owner required")
    return principal


def content_vault_ids(db: Session, principal: Principal) -> list[str]:
    """Vaults whose *content* the principal may read. Content is never shared
    across users — even org admins only see their own vaults' items. Aggregate
    statistics use ``scoped_vault_ids`` instead."""
    rows = db.query(Vault.id).filter(Vault.owner_user_id == principal.user_id).all()
    return [r[0] for r in rows]


def scoped_vault_ids(db: Session, principal: Principal, scope: str) -> tuple[list[str], str]:
    """Resolve the vault ids for an *aggregate* view and the effective scope.
    Org admins requesting ``org`` see every vault in the tenant; everyone else
    (and admins requesting ``me``) is limited to their own vaults."""
    if scope == "org" and (is_org_admin(principal.role) or principal.is_platform_admin):
        rows = db.query(Vault.id).filter(Vault.tenant_id == principal.tenant_id).all()
        return [r[0] for r in rows], "org"
    return content_vault_ids(db, principal), "me"


def get_tenant(principal: Principal = Depends(get_principal),
               db: Session = Depends(get_db)) -> Tenant:
    tenant = db.get(Tenant, principal.tenant_id)
    if not tenant:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "tenant not found")
    return tenant
