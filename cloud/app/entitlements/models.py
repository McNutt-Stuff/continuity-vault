"""Entitlement data model — per-tenant overrides only.

Entitlement *definitions* live in ``registry.py`` (code) and *grants* are derived
deterministically at read time (``engine.derive``) from the plan + pricing +
flags, so they can never drift from the commercial state. The one piece of
durable state is an admin **override**: a documented, time-boxed exception that
raises/lowers an entitlement for a single tenant (a support grant, a comp, a
temporary boost) — always separate from the billed subscription.

All NEW tables, so ``create_all`` provisions them (no migration); imported from
``db.init_db`` before ``create_all``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text

from ..db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class EntitlementOverride(Base):
    """A documented, time-boxed admin override of one entitlement for one tenant.
    Overrides adjust ACCESS, never the billed subscription — they're labelled and
    audited so a comp/support grant is never mistaken for a commercial change."""

    __tablename__ = "entitlement_overrides"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    key = Column(String, nullable=False, index=True)     # -> registry.ENTITLEMENTS
    # For bool entitlements: "true"/"false"; for quantity/capacity: an integer string.
    value = Column(String, default="")
    reason = Column(Text, default="")
    created_by = Column(String, nullable=True)
    active = Column(Boolean, default=True)
    expires_at = Column(DateTime, nullable=True)
    meta = Column(JSON, default=dict)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class AddOn(Base):
    """A purchasable add-on in the admin catalog — the internal source of truth for
    an optional capability's pricing + the entitlements/flags it grants. Money is
    integer minor-units (cents). ``code`` is the immutable internal key; display
    names are never durable identifiers."""

    __tablename__ = "addons"
    code = Column(String, primary_key=True)              # immutable internal key
    name = Column(String, default="")
    description = Column(Text, default="")
    status = Column(String, default="active")            # draft|active|grandfathered|retired
    version = Column(Integer, default=1)
    # flat | per_user | per_member | per_tb | per_cloud_tb | per_appliance | metered | tiered | included
    pricing_model = Column(String, default="flat")
    price_cents = Column(Integer, default=0)             # unit price (minor units)
    currency = Column(String, default="USD")
    billing_interval = Column(String, default="month")   # month | quarter | year
    eligible_plans = Column(JSON, default=list)          # ["business","enterprise"] (empty = all)
    entitlements = Column(JSON, default=dict)            # {ent_key: per-unit value}
    feature_flags = Column(JSON, default=list)           # flags enabled while active
    meter_key = Column(String, default="")               # for metered pricing
    min_qty = Column(Integer, default=1)
    max_qty = Column(Integer, nullable=True)
    self_service = Column(Boolean, default=True)         # customer can buy without an admin
    requires_approval = Column(Boolean, default=False)
    customer_visible = Column(Boolean, default=True)
    provider_mappings = Column(JSON, default=dict)       # {stripe_product, stripe_price, ...}
    meta = Column(JSON, default=dict)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class TenantAddOn(Base):
    """An add-on a tenant has (a lightweight subscription item). Pins the price +
    version at assignment so past invoices stay reproducible even if the catalog
    price later changes."""

    __tablename__ = "tenant_addons"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    addon_code = Column(String, nullable=False, index=True)   # -> AddOn.code
    quantity = Column(Integer, default=1)
    status = Column(String, default="active", index=True)    # active | canceled
    price_cents_snapshot = Column(Integer, default=0)        # price pinned at assignment
    addon_version = Column(Integer, default=1)               # version pinned at assignment
    effective_at = Column(DateTime, default=_now)
    ends_at = Column(DateTime, nullable=True)
    created_by = Column(String, nullable=True)
    meta = Column(JSON, default=dict)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)
