"""Catalog service — seeding, effective-version resolution, publishing, and the
dual-read pricing adapter the billing calc will consume."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .models import Plan, PlanVersion


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _cents(dollars) -> int:
    try:
        return int(round(float(dollars or 0) * 100))
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- #
# Seed the catalog from the legacy PricingConfig (dual-read rollout).         #
# --------------------------------------------------------------------------- #
def ensure_seeded(db: Session) -> None:
    """Mirror the legacy PricingConfig license tiers into Plan + PlanVersion v1
    once, so the catalog is populated without a destructive migration."""
    if db.query(Plan.code).first() is not None:
        return
    from ..api.billing import get_pricing
    p = get_pricing(db)
    appliance = [{"capacity_tb": t.get("capacity_tb"), "model": t.get("model", ""),
                  "monthly_cents": _cents(t.get("monthly")), "setup_cents": _cents(t.get("setup"))}
                 for t in (p.appliance_tiers or [])]
    cloud_cents = _cents(p.cloud_price_per_tb_month)
    now = _now()
    for tier in (p.license_plans or []):
        code = str(tier.get("id") or "").strip().lower()
        if not code:
            continue
        db.add(Plan(code=code, family=code, name=tier.get("name") or code.title(),
                    status="active", customer_visible=True))
        db.add(PlanVersion(
            plan_code=code, version=1, status="active", currency=p.currency or "USD",
            billing_interval="month", effective_from=now,
            base_price_cents=_cents(tier.get("base_price")),
            protection_cents_per_tb=_cents(tier.get("price_per_tb_month")),
            cloud_cents_per_tb=cloud_cents,
            cloud_plus_cents_per_tb=_cents(tier.get("cloud_plus_price_per_tb_month")),
            per_user_cents=_cents(tier.get("price_per_user_month")),
            per_member_cents=_cents(tier.get("price_per_additional_member")),
            included_users=int(tier.get("included_users") or 0),
            included_members=int(tier.get("included_members") or 0),
            included_tb=int(tier.get("min_tb") or 0),
            min_tb=int(tier.get("min_tb") or 0),
            appliance_tiers=appliance, notes="Seeded from PricingConfig (dual-read)."))
    db.commit()


# --------------------------------------------------------------------------- #
# Resolution + publishing.                                                    #
# --------------------------------------------------------------------------- #
def effective_version(db: Session, plan_code: str, at: datetime | None = None) -> PlanVersion | None:
    """The plan version in force at ``at`` (default now)."""
    at = at or _now()
    q = (db.query(PlanVersion)
         .filter(PlanVersion.plan_code == plan_code, PlanVersion.status == "active")
         .order_by(PlanVersion.version.desc()).all())
    for v in q:
        ef = v.effective_from or v.created_at
        if ef and ef <= at and (v.effective_to is None or v.effective_to > at):
            return v
    return q[0] if q else None


_INT_PRICE_FIELDS = (
    "base_price_cents", "protection_cents_per_tb", "cloud_cents_per_tb",
    "cloud_plus_cents_per_tb", "per_user_cents", "per_member_cents",
    "included_users", "included_members", "included_tb", "min_tb")


def publish_version(db: Session, plan_code: str, fields: dict, actor: str | None = None) -> PlanVersion:
    """Publish a NEW immutable version (closes the current one). Validates prices."""
    plan = db.get(Plan, plan_code)
    if plan is None:
        raise ValueError(f"unknown plan '{plan_code}'")
    for k in _INT_PRICE_FIELDS:
        if k in fields and fields[k] is not None and int(fields[k]) < 0:
            raise ValueError(f"{k} cannot be negative")
    now = _now()
    current = effective_version(db, plan_code, now)
    next_ver = (db.query(PlanVersion.version)
                .filter(PlanVersion.plan_code == plan_code)
                .order_by(PlanVersion.version.desc()).first())
    ver = (int(next_ver[0]) + 1) if next_ver else 1
    # Close the current version (kept as immutable history for reproducible invoices).
    if current is not None:
        current.effective_to = now
        current.status = "retired"
    clean = {k: v for k, v in (fields or {}).items()
             if k not in ("id", "plan_code", "version", "status", "effective_from", "effective_to")}
    nv = PlanVersion(plan_code=plan_code, version=ver, status="active",
                     effective_from=now, created_by=actor, **clean)
    db.add(nv)
    db.commit()
    return nv


def version_view(v: PlanVersion) -> dict:
    return {
        "id": v.id, "version": v.version, "status": v.status, "currency": v.currency,
        "billing_interval": v.billing_interval,
        "effective_from": v.effective_from.isoformat() if v.effective_from else None,
        "effective_to": v.effective_to.isoformat() if v.effective_to else None,
        "base_price_cents": v.base_price_cents,
        "protection_cents_per_tb": v.protection_cents_per_tb,
        "cloud_cents_per_tb": v.cloud_cents_per_tb,
        "cloud_plus_cents_per_tb": v.cloud_plus_cents_per_tb,
        "per_user_cents": v.per_user_cents, "per_member_cents": v.per_member_cents,
        "included_users": v.included_users, "included_members": v.included_members,
        "included_tb": v.included_tb, "min_tb": v.min_tb, "max_users": v.max_users,
        "features": v.features or [], "entitlements": v.entitlements or {},
        "compatible_addons": v.compatible_addons or [],
        "appliance_tiers": v.appliance_tiers or [], "provider_mappings": v.provider_mappings or {},
        "trial_days": v.trial_days, "notes": v.notes,
    }


def plan_view(db: Session, plan: Plan) -> dict:
    versions = (db.query(PlanVersion)
                .filter(PlanVersion.plan_code == plan.code)
                .order_by(PlanVersion.version.desc()).all())
    eff = effective_version(db, plan.code)
    return {
        "code": plan.code, "family": plan.family, "name": plan.name,
        "description": plan.description, "status": plan.status,
        "customer_visible": bool(plan.customer_visible),
        "effective_version": version_view(eff) if eff else None,
        "versions": [version_view(v) for v in versions],
    }


def catalog(db: Session) -> list[dict]:
    ensure_seeded(db)
    return [plan_view(db, p) for p in db.query(Plan).order_by(Plan.code.asc()).all()]


# --------------------------------------------------------------------------- #
# Dual-read pricing adapter — normalized cents for the billing calc (Phase 5).#
# Reads the effective PlanVersion; falls back to the legacy PricingConfig tier #
# so existing customers keep pricing while the catalog rolls out.             #
# --------------------------------------------------------------------------- #
def plan_pricing(db: Session, plan_code: str) -> dict:
    ensure_seeded(db)
    v = effective_version(db, plan_code)
    if v is not None:
        return {
            "source": "catalog", "plan_code": plan_code, "version": v.version,
            "currency": v.currency, "billing_interval": v.billing_interval,
            "base_price_cents": v.base_price_cents,
            "protection_cents_per_tb": v.protection_cents_per_tb,
            "cloud_cents_per_tb": v.cloud_cents_per_tb,
            "cloud_plus_cents_per_tb": v.cloud_plus_cents_per_tb,
            "per_user_cents": v.per_user_cents, "per_member_cents": v.per_member_cents,
            "included_users": v.included_users, "included_members": v.included_members,
            "included_tb": v.included_tb, "min_tb": v.min_tb, "max_users": v.max_users,
        }
    # Fallback: legacy PricingConfig tier (float → cents).
    from ..api.billing import get_pricing, effective_plan
    p = get_pricing(db)
    tier = effective_plan(p, plan_code) or {}
    return {
        "source": "legacy", "plan_code": plan_code, "version": 0,
        "currency": p.currency or "USD", "billing_interval": "month",
        "base_price_cents": _cents(tier.get("base_price")),
        "protection_cents_per_tb": _cents(tier.get("price_per_tb_month")),
        "cloud_cents_per_tb": _cents(p.cloud_price_per_tb_month),
        "cloud_plus_cents_per_tb": _cents(tier.get("cloud_plus_price_per_tb_month")),
        "per_user_cents": _cents(tier.get("price_per_user_month")),
        "per_member_cents": _cents(tier.get("price_per_additional_member")),
        "included_users": int(tier.get("included_users") or 0),
        "included_members": int(tier.get("included_members") or 0),
        "included_tb": int(tier.get("min_tb") or 0),
        "min_tb": int(tier.get("min_tb") or 0), "max_users": None,
    }
