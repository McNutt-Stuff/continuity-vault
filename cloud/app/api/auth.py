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

from .. import audit, authcodes, keybroker, security
from ..config import get_settings
from ..db import get_db
from ..emailer import send_email
from ..models import Passkey, Tenant, User, Vault

router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()

# Simulated-authenticator private-key store (dev only).
_AUTHN_STORE = Path("./cv_sim_authenticators.json")

# In-memory WebAuthn challenge store (key -> challenge bytes). Single-worker
# prototype; use a shared cache for multi-worker deployments.
_wa_challenges: dict[str, bytes] = {}


def _load_authn() -> dict:
    return json.loads(_AUTHN_STORE.read_text()) if _AUTHN_STORE.exists() else {}


def _save_authn(data: dict) -> None:
    _AUTHN_STORE.write_text(json.dumps(data))


class LoginResponse(BaseModel):
    token: str
    user_id: str
    tenant_id: str
    role: str
    is_platform_admin: bool
    passkey_verified: bool
    has_passkey: bool


def _session_response(user: User, passkey_verified: bool) -> LoginResponse:
    return LoginResponse(
        token=security.create_session_token(user, passkey_verified=passkey_verified),
        user_id=user.id,
        tenant_id=user.tenant_id,
        role=user.role,
        is_platform_admin=user.is_platform_admin,
        passkey_verified=passkey_verified,
        has_passkey=len(user.passkeys) > 0,
    )


# --- Login orchestration -----------------------------------------------------


class EmailBody(BaseModel):
    email: str


@router.post("/login/start")
def login_start(body: EmailBody, db: Session = Depends(get_db)):
    """Tell the client which factor to use for this address."""
    user = db.query(User).filter(User.email == body.email.strip().lower()).first()
    if not user or user.status != "active":
        return {"exists": False, "method": "signup"}
    return {
        "exists": True,
        "has_passkey": len(user.passkeys) > 0,
        # Passkey is the primary factor; email code bootstraps/recovers.
        "method": "passkey" if user.passkeys else "email",
    }


# --- Self-service sign-up ----------------------------------------------------


class SignupRequest(BaseModel):
    email: str
    display_name: str
    org_name: str


@router.post("/signup")
def signup(body: SignupRequest, db: Session = Depends(get_db)):
    import secrets as _secrets

    if not settings.allow_signup:
        raise HTTPException(403, "self-service sign-up is disabled")
    email = body.email.strip().lower()
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(409, "an account with this email already exists")

    tenant = Tenant(
        name=body.org_name.strip() or f"{body.display_name}'s Organization",
        plan="business",
        key_ownership_model="customer-managed",
        storage_prefix=f"t-{_secrets.token_hex(4)}",
    )
    db.add(tenant)
    db.flush()
    user = User(tenant_id=tenant.id, email=email, display_name=body.display_name.strip(),
                role="owner", email_verified=False, status="active")
    db.add(user)
    vault = Vault(tenant_id=tenant.id, name="Primary Vault",
                  key_ownership_model="customer-managed")
    db.add(vault)
    db.flush()
    keybroker.provision_vault_root_key(vault.id, vault.key_ownership_model)
    db.commit()

    code = authcodes.issue_code(email, "verify")
    delivery = send_email(email, "Verify your Arkive account",
                          f"Your Arkive verification code is: {code}\nIt expires shortly.")
    audit.record(db, actor=email, action="auth.signup", tenant_id=tenant.id)
    resp = {"sent": True, "delivery": delivery}
    if settings.environment == "development":
        resp["dev_code"] = code
    return resp


# --- Email verification code (bootstrap / recovery) --------------------------


class EmailCodeRequest(BaseModel):
    email: str
    purpose: str = "login"  # login | verify | recovery


@router.post("/email/request")
def email_request(body: EmailCodeRequest, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    purpose = body.purpose if body.purpose in ("login", "verify", "recovery") else "login"
    user = db.query(User).filter(User.email == email).first()
    resp: dict = {"sent": True}
    # Only actually send when the account exists (avoids account enumeration).
    if user and user.status == "active":
        try:
            code = authcodes.issue_code(email, purpose)
        except authcodes.RateLimited:
            return {"sent": True, "throttled": True}
        resp["delivery"] = send_email(
            email, "Your Arkive sign-in code",
            f"Your Arkive sign-in code is: {code}\nIt expires shortly.")
        if settings.environment == "development":
            resp["dev_code"] = code
    return resp


class EmailVerifyRequest(BaseModel):
    email: str
    code: str
    purpose: str = "login"


@router.post("/email/verify", response_model=LoginResponse)
def email_verify(body: EmailVerifyRequest, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    purpose = body.purpose if body.purpose in ("login", "verify", "recovery") else "login"
    user = db.query(User).filter(User.email == email).first()
    if not user or user.status != "active":
        raise HTTPException(400, "invalid code")
    if not authcodes.verify_code(email, body.code.strip(), purpose):
        raise HTTPException(403, "invalid or expired code")
    if not user.email_verified:
        user.email_verified = True
        db.commit()
    audit.record(db, actor=email, action=f"auth.email.{purpose}", tenant_id=user.tenant_id)
    # Email proves identity but not hardware possession: not passkey-verified.
    return _session_response(user, passkey_verified=False)


# --- Passwordless passkey login (primary factor) -----------------------------


class PasskeyLoginVerify(BaseModel):
    email: str
    credential: dict


@router.post("/login/passkey/options")
def login_passkey_options(body: EmailBody, db: Session = Depends(get_db)):
    import webauthn
    from webauthn.helpers import base64url_to_bytes
    from webauthn.helpers.structs import (
        PublicKeyCredentialDescriptor,
        UserVerificationRequirement,
    )

    user = db.query(User).filter(User.email == body.email.strip().lower()).first()
    if not user or not user.passkeys:
        raise HTTPException(404, "no passkey enrolled for this account")
    options = webauthn.generate_authentication_options(
        rp_id=settings.rp_id,
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(p.credential_id))
            for p in user.passkeys
        ],
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    _wa_challenges[f"login:{user.email}"] = options.challenge
    return json.loads(webauthn.options_to_json(options))


@router.post("/login/passkey/verify", response_model=LoginResponse)
def login_passkey_verify(body: PasskeyLoginVerify, db: Session = Depends(get_db)):
    import webauthn
    from webauthn.helpers import base64url_to_bytes

    user = db.query(User).filter(User.email == body.email.strip().lower()).first()
    if not user:
        raise HTTPException(404, "unknown account")
    challenge = _wa_challenges.pop(f"login:{user.email}", None)
    if not challenge:
        raise HTTPException(400, "no active login challenge")
    credential_id = body.credential.get("id")
    pk = (db.query(Passkey)
          .filter(Passkey.user_id == user.id, Passkey.credential_id == credential_id)
          .first())
    if not pk:
        raise HTTPException(404, "unknown credential")
    try:
        verification = webauthn.verify_authentication_response(
            credential=json.dumps(body.credential),
            expected_challenge=challenge,
            expected_rp_id=settings.rp_id,
            expected_origin=settings.rp_origin,
            credential_public_key=base64url_to_bytes(pk.public_key),
            credential_current_sign_count=pk.sign_count,
        )
    except Exception as exc:
        raise HTTPException(403, f"passkey login failed: {exc}")
    pk.sign_count = verification.new_sign_count
    db.commit()
    audit.record(db, actor=user.email, action="auth.login.passkey", tenant_id=user.tenant_id)
    return _session_response(user, passkey_verified=True)


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


# --- Real WebAuthn (platform authenticator / security key) -------------------


class RegisterVerifyRequest(BaseModel):
    credential: dict
    label: str = "This device"


class AuthVerifyRequest(BaseModel):
    credential: dict


@router.post("/webauthn/register/options")
def webauthn_register_options(principal: security.Principal = Depends(security.get_principal),
                              db: Session = Depends(get_db)):
    import webauthn
    from webauthn.helpers import base64url_to_bytes
    from webauthn.helpers.structs import (
        AuthenticatorSelectionCriteria,
        PublicKeyCredentialDescriptor,
        ResidentKeyRequirement,
        UserVerificationRequirement,
    )

    user = db.get(User, principal.user_id)
    options = webauthn.generate_registration_options(
        rp_id=settings.rp_id,
        rp_name=settings.rp_name,
        user_id=user.id.encode(),
        user_name=user.email,
        user_display_name=user.display_name,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(p.credential_id))
            for p in user.passkeys
        ],
    )
    _wa_challenges[user.id] = options.challenge
    return json.loads(webauthn.options_to_json(options))


@router.post("/webauthn/register/verify", response_model=LoginResponse)
def webauthn_register_verify(body: RegisterVerifyRequest,
                             principal: security.Principal = Depends(security.get_principal),
                             db: Session = Depends(get_db)):
    import webauthn
    from webauthn.helpers import bytes_to_base64url

    user = db.get(User, principal.user_id)
    challenge = _wa_challenges.pop(user.id, None)
    if not challenge:
        raise HTTPException(400, "no active registration challenge")
    try:
        verification = webauthn.verify_registration_response(
            credential=json.dumps(body.credential),
            expected_challenge=challenge,
            expected_rp_id=settings.rp_id,
            expected_origin=settings.rp_origin,
        )
    except Exception as exc:
        raise HTTPException(400, f"registration verification failed: {exc}")

    pk = Passkey(
        user_id=user.id,
        credential_id=bytes_to_base64url(verification.credential_id),
        public_key=bytes_to_base64url(verification.credential_public_key),
        sign_count=verification.sign_count,
        label=body.label,
        transport="platform",
    )
    db.add(pk)
    db.commit()
    audit.record(db, actor=user.email, action="passkey.registered",
                 tenant_id=user.tenant_id, resource=pk.id)

    # A successful attestation includes a user-verification gesture, so we can
    # issue a passkey-verified session immediately.
    token = security.create_session_token(user, passkey_verified=True)
    return LoginResponse(
        token=token, user_id=user.id, tenant_id=user.tenant_id, role=user.role,
        is_platform_admin=user.is_platform_admin, passkey_verified=True, has_passkey=True,
    )


@router.post("/webauthn/authenticate/options")
def webauthn_auth_options(principal: security.Principal = Depends(security.get_principal),
                          db: Session = Depends(get_db)):
    import webauthn
    from webauthn.helpers import base64url_to_bytes
    from webauthn.helpers.structs import (
        PublicKeyCredentialDescriptor,
        UserVerificationRequirement,
    )

    user = db.get(User, principal.user_id)
    if not user.passkeys:
        raise HTTPException(400, "no passkey enrolled")
    options = webauthn.generate_authentication_options(
        rp_id=settings.rp_id,
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(p.credential_id))
            for p in user.passkeys
        ],
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    _wa_challenges[user.id] = options.challenge
    return json.loads(webauthn.options_to_json(options))


@router.post("/webauthn/authenticate/verify", response_model=LoginResponse)
def webauthn_auth_verify(body: AuthVerifyRequest,
                         principal: security.Principal = Depends(security.get_principal),
                         db: Session = Depends(get_db)):
    import webauthn
    from webauthn.helpers import base64url_to_bytes

    user = db.get(User, principal.user_id)
    challenge = _wa_challenges.pop(user.id, None)
    if not challenge:
        raise HTTPException(400, "no active authentication challenge")

    credential_id = body.credential.get("id")
    pk = (db.query(Passkey)
          .filter(Passkey.user_id == user.id, Passkey.credential_id == credential_id)
          .first())
    if not pk:
        raise HTTPException(404, "unknown credential")
    try:
        verification = webauthn.verify_authentication_response(
            credential=json.dumps(body.credential),
            expected_challenge=challenge,
            expected_rp_id=settings.rp_id,
            expected_origin=settings.rp_origin,
            credential_public_key=base64url_to_bytes(pk.public_key),
            credential_current_sign_count=pk.sign_count,
        )
    except Exception as exc:
        raise HTTPException(403, f"authentication failed: {exc}")

    pk.sign_count = verification.new_sign_count
    db.commit()
    audit.record(db, actor=user.email, action="passkey.unlock", tenant_id=user.tenant_id)
    token = security.create_session_token(user, passkey_verified=True)
    return LoginResponse(
        token=token, user_id=user.id, tenant_id=user.tenant_id, role=user.role,
        is_platform_admin=user.is_platform_admin, passkey_verified=True, has_passkey=True,
    )


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
        "email_verified": user.email_verified,
        "passkey_verified": principal.passkey_verified,
        "passkeys": [{"id": p.id, "label": p.label, "transport": p.transport}
                     for p in user.passkeys],
    }
