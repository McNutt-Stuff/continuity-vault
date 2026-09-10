"""Public plan sign-up: guided account provisioning → region routing → optional
billing capture → login code → the in-portal setup wizard.

Flow: the customer picks a plan and enters their address; we resolve their region
from the address (US + Canada only for now — see geo.ACCEPTED_COUNTRIES), create
the tenant/user/vault, place the tenant on the least-full customer node in that
region's cluster, optionally store a payment method, then email a one-time login
code. On first sign-in the account is new (setup_completed_at is NULL) so the
portal shows the setup wizard.
"""

from __future__ import annotations

import logging
import secrets as _secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import audit, authcodes, geo, routing, security
from ..config import get_settings
from ..db import get_db
from ..emailer import send_email
from ..models import Region, Tenant, User

router = APIRouter(prefix="/signup", tags=["signup"])
settings = get_settings()
logger = logging.getLogger("cv.signup")

# Plans that get their own dedicated (organization) tenant; "personal" is a
# shared/pooled account with no org management.
_DEDICATED_PLANS = {"family", "business", "enterprise"}


def _plans(db: Session) -> list[dict]:
    from .billing import get_pricing
    p = get_pricing(db)
    out = []
    for pl in (p.license_plans or []):
        out.append({"id": pl.get("id"), "name": pl.get("name"),
                    "price_per_tb_month": pl.get("price_per_tb_month"),
                    "min_tb": pl.get("min_tb", 0)})
    return out


@router.get("/config")
def signup_config(db: Session = Depends(get_db)):
    """Everything the signup form needs: whether signups are open, the plans, and
    the accepted countries + which regions currently take signups."""
    regions = (db.query(Region)
               .filter(Region.signup_enabled.is_(True))
               .order_by(Region.sort_order).all())
    return {
        "enabled": bool(settings.public_signup_enabled),
        "accepted_countries": sorted(geo.ACCEPTED_COUNTRIES),
        "plans": _plans(db),
        "regions": [{"code": r.code, "name": r.name} for r in regions],
    }


class SignupBody(BaseModel):
    email: str
    first_name: str
    last_name: str = ""
    org_name: str = ""
    country: str
    subdivision: str = ""    # state / province code
    city: str = ""
    plan: str = "personal"
    licensed_tb: float = 0
    payment_method_token: str | None = None
    processor: str | None = None


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

    country = geo.normalize_country(body.country)
    if not geo.is_accepted(country):
        raise HTTPException(400, "sign-up isn't available in your country yet — "
                                 "we're expanding to more regions soon")
    region_code = geo.resolve_region(country, body.subdivision)
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
    licensed_bytes = int(max(0.0, body.licensed_tb) * (1000 ** 4))

    tenant = Tenant(
        name=org, plan=plan, tenant_type=tenant_type,
        key_ownership_model="customer-managed",
        storage_prefix=f"t-{_secrets.token_hex(4)}",
        protection_options=["cv-cloud"],
        licensed_bytes=licensed_bytes if dedicated else 0,
    )
    db.add(tenant)
    db.flush()

    # Geo-route the tenant to a region + the least-full customer node in its cluster.
    routing.route_new_tenant(db, tenant, country, body.subdivision)

    user = User(
        tenant_id=tenant.id, email=email, display_name=display,
        first_name=first, last_name=last,
        role="owner" if dedicated else "member",
        status="active", email_verified=False,
        protection_options=["cv-cloud"],
    )
    db.add(user)
    db.flush()

    provision_vault(db, tenant=tenant, owner_user_id=user.id, name="Primary Vault",
                    key_ownership_model="customer-managed")
    db.commit()

    audit.record(db, actor=email, action="auth.signup", tenant_id=tenant.id,
                 detail={"plan": plan, "region": region_code, "country": country})

    # Optional: capture a payment method now (best-effort — never fails the signup).
    if body.payment_method_token:
        try:
            _attach_signup_card(db, tenant, user, body)
        except Exception:  # noqa: BLE001
            logger.exception("signup card capture failed for %s", email)

    # Email a one-time login code so they can sign in and run the setup wizard.
    resp: dict = {"ok": True, "region": region_code, "plan": plan, "tenant_id": tenant.id}
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


def _attach_signup_card(db: Session, tenant: Tenant, user: User, body: SignupBody) -> None:
    """Store the customer's card + create an (inactive) billing profile. Charging
    is turned on separately (admin / portal), consistent with the platform's
    billing model — so a signup never surprises the customer with a charge."""
    from .. import payments, services
    from ..models import PaymentMethod
    from .billing import _ensure_billing_profile

    svc = services.tenant_payment_service(db, tenant.id)
    res = payments.attach_card_token(svc, token=body.payment_method_token or "",
                                     holder_name=user.display_name)
    if not res or not res.get("last4"):
        return
    pm = PaymentMethod(tenant_id=tenant.id, user_id=user.id, is_default=True, **res)
    db.add(pm)
    db.commit()
    db.refresh(pm)
    _ensure_billing_profile(db, tenant, user, pm)
