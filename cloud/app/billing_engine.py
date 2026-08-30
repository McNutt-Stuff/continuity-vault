"""
Recurring billing engine.

Arkive runs its own subscription cadence (rather than a processor subscription) so
it controls proration, the fiscal-year revenue reporting, and a single scheduled
sweep. A profile that an admin turns ``active`` charges the tenant's default card
for the plan amount on the monthly anniversary of activation; the sweep
(``run_due_charges``) is called by the background scheduler.
"""

from __future__ import annotations

import calendar
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from .models import BillingCharge, BillingProfile, PaymentMethod

logger = logging.getLogger("cv.billing_engine")

PERIOD_DAYS = 30
MAX_DUNNING_ATTEMPTS = 4     # failed charges tolerated before the subscription is canceled
DUNNING_RETRY_DAYS = 3       # days between retry attempts while past_due


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def add_month(dt: datetime) -> datetime:
    """One month later, clamping to the last valid day (e.g. Jan 31 → Feb 28)."""
    y = dt.year + (dt.month // 12)
    m = dt.month % 12 + 1
    d = min(dt.day, calendar.monthrange(y, m)[1])
    return dt.replace(year=y, month=m, day=d)


def activate(db: Session, prof: BillingProfile, now: datetime | None = None,
             charge_now: bool = True) -> BillingCharge | None:
    """Turn recurring charges on. The first month is billed upfront at activation;
    subsequent charges fall on the monthly anniversary. Resuming a paused profile
    does not re-charge — it keeps the existing schedule. Returns the activation
    charge (or None)."""
    now = now or _now()
    first_time = prof.activated_at is None
    prof.active = True
    prof.status = "active"
    if not first_time:
        # Resuming after a pause/cancel — clear dunning, keep/repair the schedule.
        prof.dunning_attempts = 0
        if not prof.next_charge_at or prof.next_charge_at < now:
            prof.next_charge_at = add_month(now)
        return None
    prof.activated_at = now
    if not charge_now:
        prof.next_charge_at = add_month(now)
        return None
    charge = charge_profile(db, prof, kind="recurring", now=now)
    if charge.status == "succeeded":
        prof.dunning_attempts = 0
        prof.next_charge_at = add_month(now)
        prof.status = "active"
    else:
        _handle_failure(db, prof, now)
    return charge


def _default_pm(db: Session, prof: BillingProfile) -> PaymentMethod | None:
    if prof.payment_method_id:
        pm = db.get(PaymentMethod, prof.payment_method_id)
        if pm is not None:
            return pm
    return (db.query(PaymentMethod).filter(PaymentMethod.tenant_id == prof.tenant_id)
            .order_by(PaymentMethod.is_default.desc(), PaymentMethod.created_at.desc()).first())


def charge_profile(db: Session, prof: BillingProfile, *, kind: str = "recurring",
                   amount_cents: int | None = None, now: datetime | None = None) -> BillingCharge:
    """Charge the tenant's default card and record the attempt. Caller commits."""
    from . import payments, services
    now = now or _now()
    amt = int(amount_cents if amount_cents is not None else prof.amount_cents)
    pm = _default_pm(db, prof)
    attempt = (db.query(BillingCharge).filter(BillingCharge.profile_id == prof.id).count()) + 1
    if pm is None or amt <= 0:
        res = {"charge_id": "", "status": "failed",
               "error": "no payment method on file" if pm is None else "zero amount"}
    else:
        svc = services.tenant_payment_service(db, prof.tenant_id)
        res = payments.charge_once(
            svc, customer=prof.processor_customer, token=pm.processor_token,
            amount_cents=amt, currency=prof.currency,
            description=f"{prof.plan_name or 'Arkive'} — {kind}")
    charge = BillingCharge(
        tenant_id=prof.tenant_id, profile_id=prof.id, amount_cents=amt,
        currency=prof.currency, status=res["status"], attempt=attempt, kind=kind,
        processor_charge_id=res.get("charge_id", ""), error=res.get("error", ""))
    db.add(charge)
    prof.last_charge_at = now
    prof.last_status = res["status"]
    return charge


def run_due_charges(db: Session, now: datetime | None = None) -> int:
    """Charge every active profile whose next_charge_at has arrived, then advance
    it to the next anniversary. Failures mark past_due and retry the next day."""
    now = now or _now()
    due = (db.query(BillingProfile)
           .filter(BillingProfile.active.is_(True),
                   BillingProfile.next_charge_at.isnot(None),
                   BillingProfile.next_charge_at <= now).all())
    charged = 0
    for prof in due:
        try:
            charge = charge_profile(db, prof, kind="recurring", now=now)
            if charge.status == "succeeded":
                prof.dunning_attempts = 0
                nxt = add_month(prof.next_charge_at or now)
                while nxt <= now:               # catch up if cycles were missed
                    nxt = add_month(nxt)
                prof.next_charge_at = nxt
                prof.status = "active"
            else:
                _handle_failure(db, prof, now)
            db.commit()
            charged += 1
        except Exception:  # noqa: BLE001 — one bad profile never stops the sweep
            db.rollback()
            logger.exception("recurring charge failed for profile %s", prof.id)
    if charged:
        logger.info("billing sweep charged %d profile(s)", charged)
    return charged


def _handle_failure(db: Session, prof: BillingProfile, now: datetime) -> None:
    """A charge failed: escalate dunning, retry a few times, then cancel."""
    prof.dunning_attempts = (prof.dunning_attempts or 0) + 1
    if prof.dunning_attempts >= MAX_DUNNING_ATTEMPTS:
        prof.status = "canceled"
        prof.active = False
        prof.next_charge_at = None
        _email_dunning(db, prof, final=True)
    else:
        prof.status = "past_due"
        prof.next_charge_at = now + timedelta(days=DUNNING_RETRY_DAYS)
        _email_dunning(db, prof, final=False)


def _billing_contact(db: Session, prof: BillingProfile):
    """The user to notify about billing — the default card's owner, else the
    tenant's owner."""
    from .models import User
    if prof.payment_method_id:
        pm = db.get(PaymentMethod, prof.payment_method_id)
        if pm is not None and pm.user_id:
            u = db.get(User, pm.user_id)
            if u is not None:
                return u
    return (db.query(User).filter(User.tenant_id == prof.tenant_id, User.role == "owner")
            .order_by(User.created_at.asc()).first()
            or db.query(User).filter(User.tenant_id == prof.tenant_id)
            .order_by(User.created_at.asc()).first())


def _email_dunning(db: Session, prof: BillingProfile, *, final: bool) -> None:
    from . import emailer
    from .config import get_settings
    user = _billing_contact(db, prof)
    if not user or not user.email:
        return
    link = f"https://{getattr(get_settings(), 'domain', 'vault.arkive.life')}/settings"
    try:
        if final:
            subject = "Your Arkive subscription was canceled — payment failed"
            body = (f"Hi {user.display_name or ''},\n\n"
                    "We tried several times but couldn't charge your card, so your Arkive subscription has "
                    "been canceled. Update your payment method and re-activate to keep your protected data:\n"
                    f"{link}\n\n— Arkive")
        else:
            left = MAX_DUNNING_ATTEMPTS - (prof.dunning_attempts or 0)
            subject = "Action needed: your Arkive payment failed"
            body = (f"Hi {user.display_name or ''},\n\n"
                    "We couldn't process your latest Arkive payment. Please update your card to avoid "
                    f"interruption — we'll retry in {DUNNING_RETRY_DAYS} days ({left} attempt(s) remaining):\n"
                    f"{link}\n\n— Arkive")
        emailer.send_email(user.email, subject, body, category="notification")
    except Exception as exc:  # noqa: BLE001
        logger.warning("dunning email to %s failed: %s", user.email, exc)


def proration_cents(old_amount_cents: int, new_amount_cents: int,
                    next_charge_at: datetime | None, now: datetime | None = None) -> int:
    """Delta owed for the remaining days of the current cycle when the plan amount
    changes mid-period. Positive = charge now (upgrade); <=0 = no immediate charge."""
    now = now or _now()
    if not next_charge_at or next_charge_at <= now:
        return 0
    remaining = max(0, min((next_charge_at - now).days, PERIOD_DAYS))
    delta = int(new_amount_cents) - int(old_amount_cents)
    return int(round(delta * remaining / PERIOD_DAYS))
