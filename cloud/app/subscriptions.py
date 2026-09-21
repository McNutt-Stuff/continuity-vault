"""Subscriptions & subscription items — the durable snapshot of a tenant's
commercial state (Phase 4).

The billing calc (``billing_calc.calculate``) is the deterministic *engine*; this
is its *persisted* shape. A ``Subscription`` records the plan + version a tenant is
on; ``SubscriptionItem`` rows are the individual priced components (base, capacity,
seats, members, add-ons, appliances) materialized from the calc so we have a stable
record to bill, show history against, and later map to payment-provider items.

Design:
  - CP-authoritative: rows are created on the control plane and replicate CP→node
    (``node_replication._PULL_ORDER``) so a node can read a tenant's subscription
    offline. Nodes never author these.
  - Reproducible: each item pins ``price_version`` + ``unit_price_cents`` at sync
    time, so a later catalog price change doesn't rewrite the current record.
  - Adapter, not a replacement: this sits *over* the existing ``BillingProfile`` /
    ``billing_engine`` — it doesn't move money. ``provider_*`` columns are where a
    later phase pins Stripe subscription/line ids.
  - All money is integer minor-units (cents); never float.

All NEW tables (``create_all`` provisions them); imported from ``db.init_db``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Session

from .db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Subscription(Base):
    """A tenant's active commercial subscription — plan + version + status, with the
    materialized recurring/one-time totals. One active row per tenant."""

    __tablename__ = "subscriptions"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    plan_code = Column(String, default="", index=True)      # -> catalog Plan.code / Tenant.plan
    plan_version = Column(Integer, default=0)               # catalog PlanVersion.version in force
    status = Column(String, default="active", index=True)   # active|trialing|past_due|canceled
    currency = Column(String, default="USD")
    billing_interval = Column(String, default="month")      # month|quarter|year
    recurring_cents = Column(Integer, default=0)            # sum of recurring items (snapshot)
    one_time_cents = Column(Integer, default=0)             # sum of one-time items (snapshot)
    provider = Column(String, default="")                   # stripe|paypal|""
    provider_subscription_id = Column(String, default="")   # pinned by a later phase
    synced_at = Column(DateTime, nullable=True)             # last materialize from the calc
    started_at = Column(DateTime, default=_now)
    canceled_at = Column(DateTime, nullable=True)
    meta = Column(JSON, default=dict)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class SubscriptionItem(Base):
    """One priced component of a subscription (base / capacity / seats / members /
    add-on / appliance). Mirrors a ``billing_calc.Line``; pins its price + version
    so the persisted subscription stays reproducible."""

    __tablename__ = "subscription_items"
    id = Column(String, primary_key=True, default=_uuid)
    subscription_id = Column(String, ForeignKey("subscriptions.id"), nullable=False, index=True)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    item_key = Column(String, default="", index=True)       # calc Line.key (base, protected_data, …)
    label = Column(String, default="")
    kind = Column(String, default="recurring")              # recurring|one_time
    quantity = Column(Integer, default=0)                   # billable quantity
    included_qty = Column(Integer, default=0)
    licensed_qty = Column(Integer, default=0)
    unit_price_cents = Column(Integer, default=0)
    amount_cents = Column(Integer, default=0)
    price_version = Column(Integer, default=0)              # catalog version this price came from
    addon_code = Column(String, default="")                 # set for add-on items
    provider_item_id = Column(String, default="")           # pinned by a later phase
    detail = Column(JSON, default=dict)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


# --------------------------------------------------------------------------- service


def get_subscription(db: Session, tenant_id: str) -> Subscription | None:
    """The tenant's current (non-canceled) subscription, if any."""
    return (db.query(Subscription)
            .filter(Subscription.tenant_id == tenant_id, Subscription.status != "canceled")
            .order_by(Subscription.created_at.desc()).first())


def sync_from_calc(db: Session, tenant) -> Subscription:
    """Materialize (create or refresh) the tenant's subscription + items from the
    deterministic billing calc. Idempotent: re-running replaces the item rows with
    the current calc so the persisted record always matches the engine."""
    from . import billing_calc

    calc = billing_calc.calculate(db, tenant)
    sub = get_subscription(db, tenant.id)
    if sub is None:
        sub = Subscription(tenant_id=tenant.id, started_at=_now())
        db.add(sub)
        db.flush()

    sub.plan_code = calc.plan
    sub.plan_version = calc.price_version
    sub.currency = calc.currency
    sub.recurring_cents = calc.recurring_cents
    sub.one_time_cents = calc.one_time_cents
    if getattr(tenant, "billing_status", None) == "trial":
        sub.status = "trialing"
    elif sub.status in ("", "canceled"):
        sub.status = "active"
    sub.synced_at = _now()

    # Replace items with the current calc (reproducible snapshot).
    db.query(SubscriptionItem).filter(SubscriptionItem.subscription_id == sub.id).delete()
    for l in calc.lines:
        db.add(SubscriptionItem(
            subscription_id=sub.id, tenant_id=tenant.id, item_key=l.key, label=l.label,
            kind=l.kind, quantity=l.quantity, included_qty=l.included_qty,
            licensed_qty=l.licensed_qty, unit_price_cents=l.unit_price_cents,
            amount_cents=l.amount_cents, price_version=calc.price_version,
            addon_code=(l.key.split(":", 1)[1] if l.key.startswith("addon:") else ""),
            detail=l.detail or {}))
    db.commit()
    return sub


def cancel(db: Session, tenant_id: str) -> None:
    """Mark the tenant's current subscription canceled (keeps the record + items)."""
    sub = get_subscription(db, tenant_id)
    if sub is not None:
        sub.status = "canceled"
        sub.canceled_at = _now()
        db.commit()


def view(db: Session, tenant) -> dict:
    """A serializable view of the tenant's subscription + its items (admin/customer)."""
    sub = get_subscription(db, tenant.id)
    if sub is None:
        return {"tenant_id": tenant.id, "subscription": None, "items": []}
    items = (db.query(SubscriptionItem)
             .filter(SubscriptionItem.subscription_id == sub.id)
             .order_by(SubscriptionItem.kind, SubscriptionItem.item_key).all())
    return {
        "tenant_id": tenant.id,
        "subscription": {
            "id": sub.id, "plan_code": sub.plan_code, "plan_version": sub.plan_version,
            "status": sub.status, "currency": sub.currency,
            "billing_interval": sub.billing_interval,
            "recurring_cents": sub.recurring_cents, "one_time_cents": sub.one_time_cents,
            "recurring_display": f"${sub.recurring_cents / 100:.2f}",
            "provider": sub.provider, "provider_subscription_id": sub.provider_subscription_id,
            "synced_at": sub.synced_at.isoformat() if sub.synced_at else None,
            "started_at": sub.started_at.isoformat() if sub.started_at else None,
        },
        "items": [{
            "key": i.item_key, "label": i.label, "kind": i.kind, "quantity": i.quantity,
            "included_qty": i.included_qty, "licensed_qty": i.licensed_qty,
            "unit_price_cents": i.unit_price_cents, "amount_cents": i.amount_cents,
            "price_version": i.price_version, "addon_code": i.addon_code, "detail": i.detail,
        } for i in items],
    }
