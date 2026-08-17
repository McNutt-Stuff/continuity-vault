"""Authentication: login, passkey registration, and passkey step-up unlock.

Real deployments use WebAuthn with a platform/hardware authenticator. To keep the
prototype runnable in any browser, a *simulated authenticator service* holds the
demo credential private keys server-side and signs challenges on request. The
security-critical server flow (challenge issuance, signature verification, sign
count, step-up gating) is identical to production; only the authenticator device
is simulated. The simulated endpoints are dev-only and gated on the environment.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from cv_crypto.provider import get_provider

from .. import audit, security
from ..config import get_settings
from ..db import get_db
from ..models import User

router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()

# Simulated-authenticator private-key store (dev only).
_AUTHN_STORE = Path("./cv_sim_authenticators.json")


def _load_authn() -> dict:
    return json.loads(_AUTHN_STORE.read_text()) if _AUTHN_STORE.exists() else {}


def _save_authn(data: dict) -> None:
    _AUTHN_STORE.write_text(json.dumps(data))


class LoginRequest(BaseModel):
    email: str


class LoginResponse(BaseModel):
    token: str
    user_id: str
    tenant_id: str
    role: str
    is_platform_admin: bool
    passkey_verified: bool
    has_passkey: bool


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    """Prototype primary-factor login. In production this is federated/OIDC;
    the passkey step-up below is the security-critical gate for data access."""
    user = db.query(User).filter(User.email == body.email.lower()).first()
    if not user:
        raise HTTPException(404, "unknown user")
    has_passkey = len(user.passkeys) > 0
    token = security.create_session_token(user, passkey_verified=False)
    audit.record(db, actor=user.email, action="auth.login", tenant_id=user.tenant_id)
    return LoginResponse(
        token=token,
        user_id=user.id,
        tenant_id=user.tenant_id,
        role=user.role,
        is_platform_admin=user.is_platform_admin,
        passkey_verified=False,
        has_passkey=has_passkey,
    )


class RegisterPasskeyRequest(BaseModel):
    credential_id: str
    public_key: str  # base64 Ed25519 public key (prototype authenticator)
    label: str = "Passkey"
    transport: str = "internal"


@router.post("/passkey/register")
def register_passkey(body: RegisterPasskeyRequest,
                     principal: security.Principal = Depends(security.get_principal),
                     db: Session = Depends(get_db)):
    user = db.get(User, principal.user_id)
    pk = security.register_passkey(
        db, user, body.credential_id, body.public_key, body.label, body.transport
    )
    audit.record(db, actor=user.email, action="passkey.registered",
                 tenant_id=user.tenant_id, resource=pk.id)
    return {"id": pk.id, "label": pk.label, "transport": pk.transport}


class ChallengeResponse(BaseModel):
    challenge: str


@router.post("/passkey/challenge", response_model=ChallengeResponse)
def passkey_challenge(principal: security.Principal = Depends(security.get_principal)):
    return ChallengeResponse(challenge=security.issue_challenge(principal.user_id))


class AssertRequest(BaseModel):
    credential_id: str
    challenge: str
    signature: str


@router.post("/passkey/verify", response_model=LoginResponse)
def passkey_verify(body: AssertRequest,
                   principal: security.Principal = Depends(security.get_principal),
                   db: Session = Depends(get_db)):
    stored = security.take_challenge(principal.user_id)
    if not stored or stored != body.challenge:
        raise HTTPException(400, "challenge expired or mismatched")
    user = db.get(User, principal.user_id)
    if not security.verify_passkey_assertion(db, user, body.credential_id,
                                             body.challenge, body.signature):
        raise HTTPException(403, "passkey verification failed")
    token = security.create_session_token(user, passkey_verified=True)
    audit.record(db, actor=user.email, action="passkey.unlock", tenant_id=user.tenant_id)
    return LoginResponse(
        token=token,
        user_id=user.id,
        tenant_id=user.tenant_id,
        role=user.role,
        is_platform_admin=user.is_platform_admin,
        passkey_verified=True,
        has_passkey=True,
    )


# --- Simulated authenticator (dev only) --------------------------------------


class SimRegisterRequest(BaseModel):
    label: str = "This device"
    transport: str = "internal"


@router.post("/passkey/register-simulated")
def register_simulated(body: SimRegisterRequest,
                       principal: security.Principal = Depends(security.get_principal),
                       db: Session = Depends(get_db)):
    """Enroll a passkey using the simulated authenticator (generates the key
    pair inside the 'device' and registers only the public key)."""
    if settings.environment != "development":
        raise HTTPException(403, "simulated authenticator disabled outside development")
    provider = get_provider()
    kp = provider.ed25519_keypair()
    credential_id = f"sim-{principal.user_id}-{len(_load_authn())}"
    store = _load_authn()
    store[credential_id] = base64.b64encode(kp.private_key).decode()
    _save_authn(store)

    user = db.get(User, principal.user_id)
    pk = security.register_passkey(
        db, user, credential_id, base64.b64encode(kp.public_key).decode(),
        body.label, body.transport)
    audit.record(db, actor=user.email, action="passkey.registered",
                 tenant_id=user.tenant_id, resource=pk.id)
    return {"id": pk.id, "credential_id": credential_id, "label": pk.label,
            "transport": pk.transport}


class SimSignRequest(BaseModel):
    credential_id: str
    challenge: str


@router.post("/passkey/sign-simulated")
def sign_simulated(body: SimSignRequest,
                   principal: security.Principal = Depends(security.get_principal)):
    """The simulated authenticator signs a challenge with its private key."""
    if settings.environment != "development":
        raise HTTPException(403, "simulated authenticator disabled outside development")
    store = _load_authn()
    priv_b64 = store.get(body.credential_id)
    if not priv_b64:
        raise HTTPException(404, "unknown simulated credential")
    provider = get_provider()
    sig = provider.ed25519_sign(base64.b64decode(priv_b64), body.challenge.encode())
    return {"signature": base64.b64encode(sig).decode()}


@router.get("/me")
def me(principal: security.Principal = Depends(security.get_principal),
       db: Session = Depends(get_db)):
    user = db.get(User, principal.user_id)
    return {
        "user_id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
        "tenant_id": user.tenant_id,
        "is_platform_admin": user.is_platform_admin,
        "passkey_verified": principal.passkey_verified,
        "passkeys": [{"id": p.id, "label": p.label, "transport": p.transport}
                     for p in user.passkeys],
    }
