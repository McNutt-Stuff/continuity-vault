"""Add-ons — the purchasable-capability catalog and per-tenant assignments.

Add-ons are the second source (after the base plan) that grants entitlements:
    plan defaults + ADD-ONS + contract/overrides  →  entitlements  →  features/limits
An add-on's ``entitlements`` map is applied per unit of ``quantity`` in
``engine.derive`` (quantity entitlements increment; booleans enable). Money is
integer minor-units (cents); ``code`` is the immutable durable identifier.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .models import AddOn, TenantAddOn


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Seeded on first read (like PricingConfig). Real, wired defaults — not demo data.
#
# Add-ons are ONLY for capabilities a plan does NOT already include (e.g. a lower
# plan buying M365 / Cloud Plus / Compliance that the Business plan includes), or a
# genuine metered extra. Buying MORE seats / members / TB is plan OVERAGE, priced by
# the plan version's per_user/per_member/per_tb rate over the included allowance —
# never a duplicate "extra seats" add-on. (The legacy extra_users / extra_members
# add-ons were exactly that duplication and are retired below.)
_DEFAULT_ADDONS: list[dict] = [
    {"code": "m365_managed_integration", "name": "Microsoft 365 Managed Integration",
     "description": "Admin-governed Microsoft 365 discovery + collection, billed per protected user.",
     "pricing_model": "per_user", "price_cents": 500, "billing_interval": "month",
     "eligible_plans": ["business", "enterprise"],
     "entitlements": {"m365_managed_integration": True},
     "feature_flags": ["m365_managed_integration"]},
    {"code": "arkive_cloud_plus", "name": "Arkive Cloud Plus",
     "description": "Higher-tier managed cloud storage, billed per consumed TB.",
     "pricing_model": "per_cloud_tb", "price_cents": 1500, "billing_interval": "month",
     "eligible_plans": ["business", "enterprise", "family"],
     "entitlements": {"arkive_cloud_plus_access": True}, "meter_key": "arkive_cloud_plus_tb"},
    {"code": "compliance", "name": "Compliance engine",
     "description": "Framework posture scoring (NIST/CIS/HIPAA/ISO/SOC2/GDPR).",
     "pricing_model": "flat", "price_cents": 9900, "billing_interval": "month",
     "eligible_plans": ["business", "enterprise"],
     "entitlements": {"compliance": True}, "feature_flags": ["compliance_enabled"]},
]

# Legacy seeded add-ons that duplicated plan overage — retired (not deleted, so any
# historical assignment stays reproducible) when no tenant is actively using them.
_RETIRED_DEFAULTS = ["extra_users", "extra_members"]


def ensure_defaults(db: Session) -> None:
    """Seed the default add-on catalog once (idempotent) and retire the deprecated
    overage-duplicating defaults when they're unused."""
    have = {c for (c,) in db.query(AddOn.code).all()}
    changed = False
    for spec in _DEFAULT_ADDONS:
        if spec["code"] in have:
            continue
        db.add(AddOn(status="active", version=1, currency="USD",
                     self_service=True, customer_visible=True, **spec))
        changed = True
    # Retire the deprecated duplicates if present and not actively assigned.
    for code in _RETIRED_DEFAULTS:
        a = db.get(AddOn, code)
        if a is None or a.status == "retired":
            continue
        in_use = (db.query(TenantAddOn)
                  .filter(TenantAddOn.addon_code == code, TenantAddOn.status == "active").first())
        if in_use is None:
            a.status = "retired"
            changed = True
    if changed:
        db.commit()


def catalog(db: Session, include_retired: bool = False) -> list[AddOn]:
    ensure_defaults(db)
    q = db.query(AddOn)
    if not include_retired:
        q = q.filter(AddOn.status != "retired")
    return q.order_by(AddOn.name.asc()).all()


def eligible_for_plan(db: Session, plan: str) -> list[AddOn]:
    plan = (plan or "").lower()
    out = []
    for a in catalog(db):
        if a.status not in ("active", "grandfathered"):
            continue
        elig = a.eligible_plans or []
        if not elig or plan in [str(p).lower() for p in elig]:
            out.append(a)
    return out


def active_for_tenant(db: Session, tenant_id: str) -> list[TenantAddOn]:
    now = _now()
    rows = (db.query(TenantAddOn)
            .filter(TenantAddOn.tenant_id == tenant_id,
                    TenantAddOn.status == "active").all())
    return [r for r in rows if not (r.ends_at and r.ends_at < now)]


def grants_for_tenant(db: Session, tenant_id: str) -> tuple[dict, dict, list[str]]:
    """Fold a tenant's active add-ons into (quantity_increments, bool_enables,
    feature_flags) applied to the base plan grants in ``engine.derive``."""
    qty_inc: dict[str, int] = {}
    bools: dict[str, bool] = {}
    flags: list[str] = []
    by_code = {a.code: a for a in db.query(AddOn).all()}
    for ta in active_for_tenant(db, tenant_id):
        a = by_code.get(ta.addon_code)
        if a is None:
            continue
        units = max(1, int(ta.quantity or 1))
        for key, per_unit in (a.entitlements or {}).items():
            if isinstance(per_unit, bool):
                bools[key] = bools.get(key, False) or per_unit
            else:
                try:
                    qty_inc[key] = qty_inc.get(key, 0) + int(per_unit) * units
                except (TypeError, ValueError):
                    continue
        flags.extend(a.feature_flags or [])
    return qty_inc, bools, flags


def public_view(a: AddOn) -> dict:
    return {
        "code": a.code, "name": a.name, "description": a.description, "status": a.status,
        "version": a.version, "pricing_model": a.pricing_model, "price_cents": a.price_cents,
        "currency": a.currency, "billing_interval": a.billing_interval,
        "eligible_plans": a.eligible_plans or [], "entitlements": a.entitlements or {},
        "feature_flags": a.feature_flags or [], "meter_key": a.meter_key,
        "min_qty": a.min_qty, "max_qty": a.max_qty, "self_service": bool(a.self_service),
        "requires_approval": bool(a.requires_approval), "customer_visible": bool(a.customer_visible),
    }
