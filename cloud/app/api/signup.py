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

import html as _html
import logging
import re
import secrets as _secrets
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import audit, authcodes, emailer, geo, routing, security  # noqa: F401
from ..config import get_settings
from ..db import get_db
from ..emailer import send_email
from ..models import Region, Tenant, User

router = APIRouter(prefix="/signup", tags=["signup"])
settings = get_settings()
logger = logging.getLogger("cv.signup")

TRIAL_DAYS = 7
# Fee charged per appliance if a customer cancels and doesn't return the hardware.
APPLIANCE_NONRETURN_FEE = 1999
# Family + Business get their own dedicated (organization) tenant. Enterprise is a
# sales-assisted process — never offered through self-service signup.
_DEDICATED_PLANS = {"family", "business"}
_HIDDEN_PLANS = {"enterprise"}
_VALID_OPTIONS = {"cv-cloud", "appliance", "customer-cloud"}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _plans(db: Session) -> list[dict]:
    from .billing import get_pricing
    p = get_pricing(db)
    return [{"id": pl.get("id"), "name": pl.get("name"),
             "price_per_tb_month": pl.get("price_per_tb_month"),
             "min_tb": pl.get("min_tb", 0)}
            for pl in (p.license_plans or []) if pl.get("id") not in _HIDDEN_PLANS]


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
        "appliance_nonreturn_fee": APPLIANCE_NONRETURN_FEE,
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


# --- Email verification interstitial (runs after the Account step) -----------
# The wizard sends the customer a verification link and polls /verify/status,
# continuing automatically once they click it. Single-use tokens live in memory
# (no account exists yet at this point); a shared cache would back multi-worker.
_VERIFY_TTL_SECONDS = 30 * 60
_verify_store: dict[str, dict] = {}


def _prune_verify(now: float) -> None:
    for tok in [t for t, v in _verify_store.items()
                if now - v.get("created", 0) > _VERIFY_TTL_SECONDS]:
        _verify_store.pop(tok, None)


class VerifyStartBody(BaseModel):
    email: str
    first_name: str = ""


@router.post("/verify/start")
def signup_verify_start(body: VerifyStartBody):
    """Issue a single-use email-verification token and email the customer a link.
    The signup wizard polls /signup/verify/status and continues automatically once
    the customer clicks the link (which hits /signup/verify/confirm)."""
    if not settings.public_signup_enabled:
        raise HTTPException(403, "sign-up is not available right now")
    now = time.time()
    _prune_verify(now)
    email = body.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(400, "please enter a valid email address")
    # Reuse an unexpired, unverified token for the same email so a resend doesn't
    # orphan the tab that's already polling the earlier token.
    token = next((t for t, v in _verify_store.items()
                  if v["email"] == email and not v["verified"]), None)
    if not token:
        token = _secrets.token_urlsafe(24)
        _verify_store[token] = {"email": email, "verified": False, "created": now}
    link = f"{_portal_url()}/signup?verify={token}"
    resp: dict = {"ok": True, "token": token}
    try:
        first = (body.first_name or "").strip()
        greeting = _html.escape(first) if first else "there"
        html = emailer.render(
            "Verify your email",
            f'<p style="margin:0 0 12px;">Hi {greeting}, please confirm this is your '
            f'email address to continue setting up your Arkive account.</p>'
            f'<p style="margin:0 0 12px;">Click the button below, then return to your '
            f'signup — it will continue automatically.</p>',
            cta={"label": "Verify my email", "url": link},
            preheader="Confirm your email to continue your Arkive signup",
            footer_note="If you didn't start an Arkive signup, you can ignore this email.")
        emailer.send(email, "Verify your email to continue — Arkive",
                     html=html, text=f"Verify your email to continue your Arkive signup: {link}",
                     category="signin")
        resp["sent"] = True
    except Exception:  # noqa: BLE001 — never fail the flow on a delivery hiccup
        logger.exception("signup verify email failed for %s", email)
        resp["sent"] = False
    if settings.environment == "development":
        resp["dev_link"] = link
    return resp


@router.get("/verify/status")
def signup_verify_status(token: str):
    """Poll target for the wizard — true once the customer clicks their link."""
    _prune_verify(time.time())
    v = _verify_store.get(token)
    if not v:
        return {"verified": False, "expired": True}
    return {"verified": bool(v["verified"]), "email": v["email"]}


@router.post("/verify/confirm")
def signup_verify_confirm(token: str):
    """Hit by the email link's landing page to mark the token verified."""
    v = _verify_store.get(token)
    if not v or time.time() - v.get("created", 0) > _VERIFY_TTL_SECONDS:
        raise HTTPException(404, "this verification link has expired — go back to your "
                                 "signup and resend the email")
    v["verified"] = True
    logger.info("signup email verified for %s", v["email"])
    return {"ok": True, "email": v["email"]}


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
    if not _EMAIL_RE.match(email):
        raise HTTPException(400, "please enter a valid email address")
    if not body.first_name.strip():
        raise HTTPException(400, "your first name is required")
    if not body.last_name.strip():
        raise HTTPException(400, "your last name is required")
    if body.phone.strip() and len(re.sub(r"\D", "", body.phone)) < 7:
        raise HTTPException(400, "please enter a valid phone number")

    a = body.address
    country = geo.normalize_country(a.country)
    if not geo.is_accepted(country):
        raise HTTPException(400, "sign-up isn't available in your country yet — "
                                 "we're expanding to more regions soon")
    if not a.line1.strip():
        raise HTTPException(400, "your street address is required")
    if not a.city.strip():
        raise HTTPException(400, "your city is required")
    if not geo.normalize_subdivision(a.subdivision):
        raise HTTPException(400, "please choose your state or province")
    postal = a.postal_code.strip()
    if country == "US" and not re.match(r"^\d{5}(-\d{4})?$", postal):
        raise HTTPException(400, "please enter a valid US ZIP code")
    if country == "CA" and not re.match(r"^[A-Za-z]\d[A-Za-z] ?\d[A-Za-z]\d$", postal):
        raise HTTPException(400, "please enter a valid Canadian postal code")

    region_code = geo.resolve_region(country, a.subdivision)
    region = db.query(Region).filter(Region.code == region_code).first()
    if region is None or not region.signup_enabled:
        raise HTTPException(400, "sign-up isn't available for your region yet")

    if db.query(User).filter(func.lower(User.email) == email).first():
        raise HTTPException(409, "an account with this email already exists — try signing in")

    plans = {p["id"] for p in _plans(db)}
    plan = body.plan if body.plan in plans else "personal"
    dedicated = plan in _DEDICATED_PLANS
    tenant_type = "dedicated" if dedicated else "shared"
    if dedicated and not body.org_name.strip():
        raise HTTPException(400, "please name your organization")

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

    # Alert platform admins to the new signup (best-effort — never blocks signup).
    try:
        from .. import admin_notifications
        admin_notifications.emit(
            db, "new_signup",
            subject=f"[Arkive] New signup — {org}",
            title="New customer signup",
            intro=f"{display} just signed up for the {plan.title()} plan.",
            rows=[{"icon": "user", "name": org, "detail": f"{plan.title()} plan"},
                  {"icon": "email", "name": email, "detail": display},
                  {"icon": "activity", "name": "Region", "detail": f"{region_code} · {country}"}],
            severity="info", cta={"label": "Open admin", "url": _portal_url() + "/admin"})
    except Exception:  # noqa: BLE001
        logger.exception("admin new-signup alert failed for %s", email)


    trial_started = False
    if body.payment_method_token:
        try:
            trial_started = _attach_card_and_trial(db, tenant, user, body)
        except Exception:  # noqa: BLE001 — never fail the signup on billing issues
            logger.exception("signup billing failed for %s", email)

    # A summary/welcome email (separate from the sign-in code) so the customer has
    # a record of their plan, protection, trial terms and the hardware fine print.
    try:
        _send_signup_summary(db, user, plan, options, appliance_plan)
    except Exception:  # noqa: BLE001
        logger.exception("signup summary email failed for %s", email)

    resp: dict = {"ok": True, "region": region_code, "plan": plan,
                  "tenant_id": tenant.id, "trial": trial_started, "trial_days": TRIAL_DAYS,
                  "vault_id": vault.id}
    try:
        code = authcodes.issue_code(email, "login")
        resp["delivery"] = send_email(
            email, "Verify your email — your Arkive sign-in code",
            f"Welcome to Arkive!\n\nVerify your email and finish setting up your account "
            f"by entering this code on the sign-in screen: {code}\nIt expires shortly.",
            category="signin")
        resp["sent"] = True
        if settings.environment == "development":
            resp["dev_code"] = code
    except authcodes.RateLimited:
        resp["sent"] = False
        resp["throttled"] = True
    return resp


def _send_signup_summary(db: Session, user: User, plan: str, options: list[str],
                         appliance_plan: list[dict]) -> None:
    """Email the new customer a summary of their plan, protection, trial and the
    hardware terms (ship-after-trial + non-return fee)."""
    from datetime import datetime, timedelta, timezone
    from .. import emailer

    labels = {"cv-cloud": "Arkive Cloud", "appliance": "Secure appliance",
              "customer-cloud": "Your own cloud storage"}
    trial_end = (datetime.now(timezone.utc) + timedelta(days=TRIAL_DAYS)).strftime("%B %-d, %Y")
    has_hw = "appliance" in options or bool(appliance_plan)
    n_appliances = sum(int(s.get("qty") or 0) for s in (appliance_plan or []))

    parts = [f'<p style="margin:0 0 12px;">Welcome to Arkive, {user.first_name or "there"}! '
             f'Here\'s a summary of your new account.</p>']
    rows = [{"icon": "sparkle", "name": "Plan", "detail": plan.title()},
            {"icon": "shield", "name": "Protection", "detail": ", ".join(labels.get(o, o) for o in options)},
            {"icon": "clock", "name": "Free trial", "detail": f"{TRIAL_DAYS} days — through {trial_end}"}]
    parts.append(emailer_rows(rows))
    parts.append(f'<p style="margin:14px 0 0;">Your {TRIAL_DAYS}-day free trial is active. '
                 f'We\'ll start billing your {plan.title()} plan on {trial_end} — cancel anytime before then '
                 f'and you won\'t be charged.</p>')
    if has_hw:
        parts.append(f'<p style="margin:12px 0 0;"><b>Hardware:</b> your appliance'
                     f'{"s" if n_appliances != 1 else ""} will ship after your {TRIAL_DAYS}-day trial completes.</p>')
    footer = (f"Fine print: if you cancel your plan and do not return your appliance(s), a fee of "
              f"${APPLIANCE_NONRETURN_FEE:,} per appliance applies." if has_hw else
              "You're receiving this because you created an Arkive account.")
    html = emailer.render("Your Arkive account is ready", "".join(parts),
                          cta={"label": "Sign in to Arkive", "url": _portal_url() + "/"},
                          preheader="Your plan, protection and trial details",
                          footer_note=footer)
    emailer.send(user.email, "Your Arkive account — plan & trial summary",
                 html=html, text="Welcome to Arkive. Your account is ready; sign in with the code we emailed.",
                 category="signup")


def emailer_rows(rows: list[dict]) -> str:
    from .. import notifications
    return notifications._rows(rows)


def _portal_url() -> str:
    return (getattr(settings, "rp_origin", "") or "https://vault.arkive.life").rstrip("/")


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
