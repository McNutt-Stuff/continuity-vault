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

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, JSON, String, Text

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
