"""Entitlement engine — the centralized service every feature check flows through.

``derive`` computes a tenant's effective entitlements deterministically from:
    plan defaults (registry) → PricingConfig included quantities → committed
    capacity (Tenant.licensed_bytes) → feature flags → admin overrides
and the ``has_entitlement`` / ``get_limit`` / ``get_usage`` / ``can_consume`` /
``require_entitlement`` helpers wrap it so call-sites never hard-code plan names.

Phase 1 is READ + VISIBILITY + OPTIONAL enforcement: derivation and the admin
view are always on; hard *enforcement* (e.g. rejecting a seat over the limit) is
gated by the ``entitlements_enforced`` flag (OFF by default) so existing
customers are never surprised. Later phases feed subscription items / add-ons /
Enterprise contracts into ``derive`` without touching enforcement call-sites.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import registry
from .models import EntitlementOverride

_TB = 1024 ** 4

# Which member statuses consume a licensed seat. A removed/deleted member frees a
# seat; everything else (active, invited, suspended, service) still consumes one.
_NON_CONSUMING_STATUSES = {"removed", "deleted", "deactivated"}

# Feature flag that turns HARD enforcement on. OFF by default (visibility only).
ENFORCE_FLAG = "entitlements_enforced"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class Entitlement:
    key: str
    type: str                       # bool | quantity | capacity
    value: object                   # bool for bool; int for quantity/capacity
    unit: str = ""
    title: str = ""
    source: str = "plan"            # plan | flag | override
    detail: dict = field(default_factory=dict)


def _coerce(etype: str, raw: str):
    if etype == "bool":
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return 0


def _active_overrides(db: Session, tenant_id: str) -> dict[str, EntitlementOverride]:
    now = _now()
    out: dict[str, EntitlementOverride] = {}
    for o in (db.query(EntitlementOverride)
              .filter(EntitlementOverride.tenant_id == tenant_id,
                      EntitlementOverride.active.is_(True)).all()):
        if o.expires_at is not None and o.expires_at < now:
            continue
        out[o.key] = o  # latest wins (one active override per key expected)
    return out


def _plan_included(db: Session, plan: str) -> dict:
    """Included user/member quantities for a plan, read from the PricingConfig tier
    (data-driven) so admins tune them without code. Falls back to registry defaults."""
    inc: dict = {}
    try:
        from ..api.billing import get_pricing, effective_plan
        p = get_pricing(db)
        tier = effective_plan(p, plan)
        if isinstance(tier, dict):
            if tier.get("included_users") is not None:
                inc["protected_users"] = int(tier["included_users"])
            if tier.get("included_members") is not None:
                inc["family_members"] = int(tier["included_members"])
    except Exception:  # noqa: BLE001 — pricing optional; registry defaults apply
        pass
    return inc


def derive(db: Session, tenant, user=None) -> dict[str, Entitlement]:
    """The tenant's effective entitlements. Deterministic + reproducible."""
    from .. import features
    plan = (getattr(tenant, "plan", "") or "").lower()
    grants = registry.plan_grants(plan)
    included = _plan_included(db, plan)
    grants.update({k: v for k, v in included.items() if v is not None})

    # Committed capacity from licensed_bytes (authoritative when set).
    lb = int(getattr(tenant, "licensed_bytes", 0) or 0)
    if lb > 0:
        grants["protected_data_tb"] = max(int(grants.get("protected_data_tb", 0) or 0),
                                          max(1, round(lb / _TB)))

    # The catalog plan version (admin-edited via the plan editor) is authoritative
    # over the code-registry defaults for the entitlements it specifies.
    try:
        from .. import catalog
        v = catalog.effective_version(db, plan)
        if v is not None and v.entitlements:
            for k, val in v.entitlements.items():
                grants[k] = val
    except Exception:  # noqa: BLE001 — catalog optional; registry defaults apply
        pass

    # Add-ons the tenant has purchased add to the plan grants (quantity entitlements
    # increment; boolean entitlements enable). Applied BEFORE overrides.
    try:
        from . import addons
        qty_inc, add_bools, _flags = addons.grants_for_tenant(db, tenant.id)
        for k, inc in qty_inc.items():
            grants[k] = int(grants.get(k, 0) or 0) + int(inc)
        for k, on in add_bools.items():
            if on:
                grants[k] = True
    except Exception:  # noqa: BLE001 — add-ons optional; plan grants still apply
        pass

    overrides = _active_overrides(db, tenant.id)
    out: dict[str, Entitlement] = {}
    for key, spec in registry.ENTITLEMENTS.items():
        etype = spec["type"]
        source = "plan"
        if etype == "bool" and spec.get("feature"):
            # The flag resolver is now the single source of truth for a flag-backed
            # capability: it layers plan/add-on grant under per-user/tenant overrides
            # (and legal-hold disable). No inverted "flag OR grant" here.
            value: object = features.resolve(user, tenant, spec["feature"], db=db)
        else:
            value = grants.get(key)
        if value is None:
            value = False if etype == "bool" else 0
        if key in overrides:
            value = _coerce(etype, overrides[key].value)
            source = "override"
        out[key] = Entitlement(
            key=key, type=etype, value=value, unit=spec.get("unit", ""),
            title=spec.get("title", key), source=source,
            detail={"expires_at": overrides[key].expires_at.isoformat()
                    if key in overrides and overrides[key].expires_at else None,
                    "reason": overrides[key].reason if key in overrides else ""})
    return out


# --------------------------------------------------------------------------- #
# Usage (consumption) — measured from live platform state.                    #
# --------------------------------------------------------------------------- #
def get_usage(db: Session, tenant, key: str) -> int:
    """Consumed quantity for a quantity/capacity entitlement (0 for booleans)."""
    from ..models import User, SearchDocument
    from sqlalchemy import func
    if key in ("protected_users", "family_members"):
        rows = (db.query(User.status)
                .filter(User.tenant_id == tenant.id).all())
        return sum(1 for (st,) in rows if (st or "active") not in _NON_CONSUMING_STATUSES)
    if key == "protected_data_tb":
        used = int(db.query(func.coalesce(func.sum(SearchDocument.size_bytes), 0))
                   .filter(SearchDocument.tenant_id == tenant.id,
                           SearchDocument.is_current.is_(True)).scalar() or 0)
        return max(0, round(used / _TB))
    return 0


# --------------------------------------------------------------------------- #
# Service API — the ONLY sanctioned way to check entitlements.                #
# --------------------------------------------------------------------------- #
def has_entitlement(db: Session, tenant, key: str, user=None) -> bool:
    e = derive(db, tenant, user).get(key)
    if e is None:
        return False
    return bool(e.value) if e.type == "bool" else int(e.value or 0) > 0


def get_limit(db: Session, tenant, key: str, user=None) -> int | None:
    """The granted quantity/capacity limit (None for booleans / unlimited=0-means-none)."""
    e = derive(db, tenant, user).get(key)
    if e is None or e.type == "bool":
        return None
    return int(e.value or 0)


def can_consume(db: Session, tenant, key: str, quantity: int = 1, user=None) -> bool:
    limit = get_limit(db, tenant, key, user)
    if limit is None or limit <= 0:
        return True  # boolean or unmetered (0 = not seat-limited) → allowed
    return (get_usage(db, tenant, key) + max(0, quantity)) <= limit


def enforcement_on(db: Session, tenant, user=None) -> bool:
    from .. import features
    return features.resolve(user, tenant, ENFORCE_FLAG)


def require_seat(db: Session, tenant, user=None, quantity: int = 1) -> None:
    """Raise 403 if adding ``quantity`` members would exceed the licensed seats AND
    enforcement is enabled. No-op when enforcement is off (visibility-only rollout)."""
    from fastapi import HTTPException
    if not enforcement_on(db, tenant, user):
        return
    key = "family_members" if (getattr(tenant, "plan", "") or "").lower() == "family" else "protected_users"
    if not can_consume(db, tenant, key, quantity, user):
        limit = get_limit(db, tenant, key, user) or 0
        used = get_usage(db, tenant, key)
        raise HTTPException(402, f"Your plan includes {limit} licensed {key.replace('_', ' ')} "
                                 f"and {used} are in use. Add licenses to invite more.")


def view(db: Session, tenant, user=None) -> dict:
    """Admin/customer view: every entitlement with granted, used, remaining + source."""
    ents = derive(db, tenant, user)
    out = []
    for key, e in ents.items():
        used = get_usage(db, tenant, key) if e.type != "bool" else None
        limit = None if e.type == "bool" else int(e.value or 0)
        out.append({
            "key": key, "title": e.title, "type": e.type, "unit": e.unit,
            "value": e.value, "granted": limit, "used": used,
            "remaining": (None if limit is None else max(0, limit - (used or 0))),
            "source": e.source, **e.detail,
        })
    return {"entitlements": out}
