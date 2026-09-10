"""Public plan sign-up: a full guided flow — plan → account → address →
protection setup → billing (with a 7-day free trial) → login → setup wizard.

The customer picks a plan, enters their details + full address (US + Canada only
for now — see geo.ACCEPTED_COUNTRIES), configures protection destinations, and
adds a card that starts a 7-day free trial (auto-bills at trial end). We create
the tenant/user/vault, place the tenant on the least-full customer node in the
region's cluster, store the address + payment method, start the trial, then email
a one-time login code. First sign-in shows the in-portal setup wizard.
"""

from __future__ import annotations

import logging
import secrets as _secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import audit, authcodes, geo, routing, security  # noqa: F401
from ..config import get_settings
from ..db import get_db
from ..emailer import send_email
from ..models import Region, Tenant, User

router = APIRouter(prefix="/signup", tags=["signup"])
settings = get_settings()
logger = logging.getLogger("cv.signup")

TRIAL_DAYS = 7
_DEDICATED_PLANS = {"family", "business", "enterprise"}
_VALID_OPTIONS = {"cv-cloud", "appliance", "customer-cloud"}


def _plans(db: Session) -> list[dict]:
    from .billing import get_pricing
    p = get_pricing(db)
    return [{"id": pl.get("id"), "name": pl.get("name"),
             "price_per_tb_month": pl.get("price_per_tb_month"),
             "min_tb": pl.get("min_tb", 0)} for pl in (p.license_plans or [])]


@router.get("/config")
def signup_config(db: Session = Depends(get_db)):
    """Everything the signup form needs: whether signups are open, the plans +
    full pricing (so the protection step mirrors the in-app onboarding), the
    accepted countries, signup-enabled regions, and the trial length."""
    from .billing import get_pricing, pricing_public
    regions = (db.query(Region)
               .filter(Region.signup_enabled.is_(True))
               .order_by(Region.sort_order).all())
    return {
        "enabled": bool(settings.public_signup_enabled),
        "trial_days": TRIAL_DAYS,
        "accepted_countries": sorted(geo.ACCEPTED_COUNTRIES),
        "plans": _plans(db),
        "pricing": pricing_public(get_pricing(db)),
        "regions": [{"code": r.code, "name": r.name} for r in regions],
    }


@router.get("/payment-config")
def signup_payment_config():
    """The processor + public key the signup billing step needs. Resolves the
    control plane's assigned payment service (single-cluster today); never returns
    a secret. `configured: false` → the UI offers a no-card trial."""
    from .. import services
    svc = services.self_payment_service()
    if not svc:
        return {"configured": False, "processor": None}
    kind = svc.get("kind", "")
    cfg = svc.get("config", {}) or {}
    processor = kind.replace("payment-", "") or None
    out = {"configured": True, "processor": processor, "name": svc.get("name"),
           "currency": (cfg.get("currency") or "USD").upper()}
    if processor == "stripe":
        out["publishable_key"] = cfg.get("publishable_key") or ""
    elif processor == "paypal":
        out["client_id"] = cfg.get("client_id") or ""
        out["environment"] = cfg.get("environment") or "live"
    return out


class Address(BaseModel):
    line1: str = ""
    line2: str = ""
    city: str = ""
    subdivision: str = ""   # state / province code
    postal_code: str = ""
    country: str = "US"


class SignupBody(BaseModel):
    email: str
    first_name: str
    last_name: str = ""
    phone: str = ""
    org_name: str = ""
    plan: str = "personal"
    address: Address
    # Protection setup (mirrors the in-app onboarding).
    options: list[str] = ["cv-cloud"]
    licensed_tb: float = 1
    appliance_plan: list[dict] = []
    # Billing / trial.
    payment_method_token: str | None = None
    trial: bool = True


@router.post("")
def signup(body: SignupBody, db: Session = Depends(get_db)):
    from .tenant import provision_vault

    if not settings.public_signup_enabled:
        raise HTTPException(403, "sign-up is not available right now")

    email = body.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "a valid email is required")
    if not body.first_name.strip():
        raise HTTPException(400, "your name is required")

    country = geo.normalize_country(body.address.country)
    if not geo.is_accepted(country):
        raise HTTPException(400, "sign-up isn't available in your country yet — "
                                 "we're expanding to more regions soon")
    region_code = geo.resolve_region(country, body.address.subdivision)
    region = db.query(Region).filter(Region.code == region_code).first()
    if region is None or not region.signup_enabled:
        raise HTTPException(400, "sign-up isn't available for your region yet")

    if db.query(User).filter(func.lower(User.email) == email).first():
        raise HTTPException(409, "an account with this email already exists — try signing in")

    plans = {p["id"] for p in _plans(db)}
    plan = body.plan if body.plan in plans else "personal"
    dedicated = plan in _DEDICATED_PLANS
    tenant_type = "dedicated" if dedicated else "shared"

    first = body.first_name.strip()
    last = body.last_name.strip()
    display = (f"{first} {last}".strip()) or email
    org = body.org_name.strip() or (f"{first}'s Organization" if dedicated else f"{first}'s Account")
    options = [o for o in (body.options or []) if o in _VALID_OPTIONS] or ["cv-cloud"]
    licensed_bytes = int(max(0.0, body.licensed_tb) * (1000 ** 4))
    appliance_plan = [{"capacity_tb": s.get("capacity_tb"), "qty": int(s.get("qty") or 0)}
                      for s in (body.appliance_plan or []) if int(s.get("qty") or 0) > 0]

    tenant = Tenant(
        name=org, plan=plan, tenant_type=tenant_type,
        key_ownership_model="customer-managed",
        storage_prefix=f"t-{_secrets.token_hex(4)}",
        protection_options=options,
        licensed_bytes=licensed_bytes if dedicated else 0,
        appliance_plan=appliance_plan,
    )
    db.add(tenant)
    db.flush()

    # Geo-route the tenant to a region + the least-full customer node in its cluster.
    routing.route_new_tenant(db, tenant, country, body.address.subdivision)

    user = User(
        tenant_id=tenant.id, email=email, display_name=display,
        first_name=first, last_name=last, phone=body.phone.strip(),
        role="owner" if dedicated else "member",
        status="active", email_verified=False,
        protection_options=options,
    )
    db.add(user)
    db.flush()

    vault = provision_vault(db, tenant=tenant, owner_user_id=user.id, name="Primary Vault",
                            key_ownership_model="customer-managed")
    _save_billing_address(db, tenant, user, body)
    db.commit()

    audit.record(db, actor=email, action="auth.signup", tenant_id=tenant.id,
                 detail={"plan": plan, "region": region_code, "country": country,
                         "trial": bool(body.trial)})

    trial_started = False
    if body.payment_method_token:
        try:
            trial_started = _attach_card_and_trial(db, tenant, user, body)
        except Exception:  # noqa: BLE001 — never fail the signup on billing issues
            logger.exception("signup billing failed for %s", email)

    resp: dict = {"ok": True, "region": region_code, "plan": plan,
                  "tenant_id": tenant.id, "trial": trial_started, "trial_days": TRIAL_DAYS,
                  "vault_id": vault.id}
    try:
        code = authcodes.issue_code(email, "login")
        resp["delivery"] = send_email(
            email, "Welcome to Arkive — your sign-in code",
            f"Welcome to Arkive!\n\nYour sign-in code is: {code}\nIt expires shortly.\n\n"
            "Enter it on the sign-in screen to finish setting up your account.",
            category="signin")
        resp["sent"] = True
        if settings.environment == "development":
            resp["dev_code"] = code
    except authcodes.RateLimited:
        resp["sent"] = False
        resp["throttled"] = True
    return resp


def _save_billing_address(db: Session, tenant: Tenant, user: User, body: SignupBody) -> None:
    from ..models import UserAddress
    a = body.address
    if not (a.line1 or a.city or a.postal_code):
        return
    db.add(UserAddress(
        tenant_id=tenant.id, user_id=user.id, kind="billing", name=user.display_name,
        line1=a.line1.strip(), line2=a.line2.strip(), city=a.city.strip(),
        region=geo.normalize_subdivision(a.subdivision), postal_code=a.postal_code.strip(),
        country=geo.normalize_country(a.country), phone=body.phone.strip(), is_default=True))


def _attach_card_and_trial(db: Session, tenant: Tenant, user: User, body: SignupBody) -> bool:
    """Store the card, create a billing profile, and start a 7-day free trial that
    auto-bills at the end. Returns True if a trial was started."""
    from .. import billing_engine, payments, services
    from ..models import PaymentMethod
    from .billing import _ensure_billing_profile

    svc = services.tenant_payment_service(db, tenant.id) or services.self_payment_service()
    res = payments.attach_card_token(svc, token=body.payment_method_token or "",
                                     holder_name=user.display_name)
    if not res or not res.get("last4"):
        return False
    pm = PaymentMethod(tenant_id=tenant.id, user_id=user.id, is_default=True, **res)
    db.add(pm)
    db.commit()
    db.refresh(pm)
    prof = _ensure_billing_profile(db, tenant, user, pm)
    if body.trial:
        billing_engine.start_trial(db, prof, days=TRIAL_DAYS)
    else:
        billing_engine.activate(db, prof)  # charge the first month now
    db.commit()
    return bool(body.trial)
