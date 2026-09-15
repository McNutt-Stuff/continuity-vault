"""Catalog & price-book — versioned plans with immutable price history.

The forward source of truth for what Arkive sells and how it's priced. Unlike the
legacy single-row float ``PricingConfig``, this is:
  - **versioned**: a ``PlanVersion`` is immutable once published; a price change
    publishes a NEW version (prior invoices stay reproducible),
  - **minor-unit money**: all prices are integer cents (never float),
  - **immutable codes**: ``Plan.code`` is the durable identifier, not the name,
  - **effective-dated**: ``effective_from`` / ``effective_to`` select the version
    in force at any instant,
  - **provider-mapped**: ``provider_mappings`` pins Stripe product/price ids.

Rollout is dual-read: the catalog is seeded from ``PricingConfig`` and the
``service.plan_pricing`` adapter falls back to it, so existing billing keeps
working while later phases (subscription items, billing calc) read the catalog.
All NEW tables (``create_all``); imported from ``db.init_db``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Integer, JSON, String, Text, UniqueConstraint

from ..db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Plan(Base):
    """A commercial plan offering. ``code`` is the immutable durable identifier
    (matches ``Tenant.plan`` for the built-in families)."""

    __tablename__ = "catalog_plans"
    code = Column(String, primary_key=True)              # immutable, e.g. "business"
    family = Column(String, default="", index=True)      # personal|consumer|family|business|enterprise
    name = Column(String, default="")
    description = Column(Text, default="")
    status = Column(String, default="active")            # draft|active|grandfathered|retired
    customer_visible = Column(Boolean, default=True)
    meta = Column(JSON, default=dict)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class PlanVersion(Base):
    """An immutable, effective-dated priced configuration of a plan. Once
    published (status=active) it is never edited — a change publishes a new
    version and closes this one's ``effective_to``. All money is integer cents."""

    __tablename__ = "catalog_plan_versions"
    __table_args__ = (UniqueConstraint("plan_code", "version", name="uq_plan_version"),)
    id = Column(String, primary_key=True, default=_uuid)
    plan_code = Column(String, nullable=False, index=True)   # -> Plan.code
    version = Column(Integer, default=1)
    status = Column(String, default="draft", index=True)     # draft|active|retired
    currency = Column(String, default="USD")
    billing_interval = Column(String, default="month")       # month|quarter|year
    effective_from = Column(DateTime, nullable=True, index=True)
    effective_to = Column(DateTime, nullable=True)

    # Prices — integer minor-units (cents).
    base_price_cents = Column(Integer, default=0)            # recurring base subscription
    protection_cents_per_tb = Column(Integer, default=0)     # protected-data rate
    cloud_cents_per_tb = Column(Integer, default=0)          # Arkive Cloud
    cloud_plus_cents_per_tb = Column(Integer, default=0)     # Arkive Cloud Plus
    per_user_cents = Column(Integer, default=0)              # per licensed user (Business+)
    per_member_cents = Column(Integer, default=0)            # per family member over the allowance

    # Included allowances + limits.
    included_users = Column(Integer, default=0)
    included_members = Column(Integer, default=0)
    included_tb = Column(Integer, default=0)
    min_tb = Column(Integer, default=0)
    max_users = Column(Integer, nullable=True)

    features = Column(JSON, default=list)                   # feature flags included
    entitlements = Column(JSON, default=dict)               # base entitlements granted
    compatible_addons = Column(JSON, default=list)          # add-on codes eligible
    appliance_tiers = Column(JSON, default=list)            # [{capacity_tb, monthly_cents, setup_cents, model}]
    provider_mappings = Column(JSON, default=dict)          # {stripe_product, stripe_price}
    proration = Column(String, default="prorate")           # prorate|none
    trial_days = Column(Integer, default=0)
    notes = Column(Text, default="")
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)
