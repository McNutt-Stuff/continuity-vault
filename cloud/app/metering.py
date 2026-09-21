"""Usage metering — idempotent usage meters that feed the billing calc (Phase 7).

The billing calc needs a defensible, reproducible *quantity* for usage-based
charges (metered add-ons, capacity peaks). Reading a live counter at charge time
is neither reproducible (it drifts) nor safe under retries. This module records
usage into period buckets with an **idempotency key** so the same observation is
never double-counted, and exposes an aggregate the calc consumes.

Two ways in:
  - ``record(...)``  — append an external usage *event* (idempotent on its key);
    aggregated by the meter's rule (sum/max/last). Use for discrete events.
  - ``observe(...)`` — snapshot a *level* (e.g. current peak TB) into the period;
    idempotent per (meter, period, source) and monotonic for ``max`` meters, so a
    periodic worker can call it every tick without inflating the number.

Meters are defined in code (``_DEFAULT_METERS``) and seeded to the DB. Usage is
control-plane billing state; the calc falls back to a live count when a meter has
no data yet, so this is safe to roll out incrementally.

All money stays in the calc; this only produces quantities. NEW tables
(``create_all``); imported from ``db.init_db``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Session

from .db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def current_period(at: datetime | None = None) -> str:
    """The billing period bucket a timestamp falls in (monthly, ``YYYY-MM``)."""
    d = at or _now()
    return f"{d.year:04d}-{d.month:02d}"


class UsageMeter(Base):
    """A meter definition — what is measured, in what unit, and how a period's many
    records collapse to one billable quantity."""

    __tablename__ = "usage_meters"
    key = Column(String, primary_key=True)               # immutable meter id
    name = Column(String, default="")
    description = Column(Text, default="")
    unit = Column(String, default="unit")                # tb | seat | unit | request | …
    aggregation = Column(String, default="max")          # max (peak) | sum | last
    entitlement_key = Column(String, default="")         # -> registry entitlement it evidences
    active = Column(String, default="active")            # active | retired
    meta = Column(JSON, default=dict)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class UsageRecord(Base):
    """One usage observation for a tenant + meter + period. ``idempotency_key`` is
    globally unique so a retried event / repeated snapshot never double-counts."""

    __tablename__ = "usage_records"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    meter_key = Column(String, nullable=False, index=True)   # -> UsageMeter.key
    period = Column(String, nullable=False, index=True)      # YYYY-MM bucket
    quantity = Column(Integer, default=0)                    # in the meter's unit
    idempotency_key = Column(String, nullable=False, unique=True)
    source = Column(String, default="")                     # snapshot | connector | api | …
    at = Column(DateTime, default=_now)
    meta = Column(JSON, default=dict)
    created_at = Column(DateTime, default=_now)


# --------------------------------------------------------------------------- meters

_DEFAULT_METERS = [
    {"key": "protected_data_tb", "name": "Protected data (TB)", "unit": "tb",
     "aggregation": "max", "entitlement_key": "protected_data_tb",
     "description": "Peak protected data in the period."},
    {"key": "protected_users", "name": "Protected users", "unit": "seat",
     "aggregation": "max", "entitlement_key": "protected_users",
     "description": "Peak licensed users in the period."},
    {"key": "cloud_stored_tb", "name": "Arkive Cloud stored (TB)", "unit": "tb",
     "aggregation": "max", "entitlement_key": "arkive_cloud_access",
     "description": "Peak data stored in Arkive Cloud in the period."},
]


def ensure_defaults(db: Session) -> None:
    """Seed the built-in meters (id-stable; safe to call on every startup)."""
    existing = {m.key for m in db.query(UsageMeter.key).all()}
    changed = False
    for spec in _DEFAULT_METERS:
        if spec["key"] in existing:
            continue
        db.add(UsageMeter(**spec))
        changed = True
    if changed:
        db.commit()


# --------------------------------------------------------------------------- service


def _meter(db: Session, meter_key: str) -> UsageMeter | None:
    return db.get(UsageMeter, meter_key)


def record(db: Session, tenant_id: str, meter_key: str, quantity: int, *,
           idempotency_key: str, source: str = "api", period: str | None = None,
           at: datetime | None = None, meta: dict | None = None) -> UsageRecord:
    """Append a usage event, idempotent on ``idempotency_key`` — a repeat is a no-op
    that returns the already-stored record (never double-counts)."""
    existing = (db.query(UsageRecord)
                .filter(UsageRecord.idempotency_key == idempotency_key).first())
    if existing is not None:
        return existing
    rec = UsageRecord(tenant_id=tenant_id, meter_key=meter_key,
                      period=period or current_period(at), quantity=int(quantity or 0),
                      idempotency_key=idempotency_key, source=source,
                      at=at or _now(), meta=meta or {})
    db.add(rec)
    db.commit()
    return rec


def observe(db: Session, tenant_id: str, meter_key: str, quantity: int, *,
            source: str = "snapshot", period: str | None = None,
            at: datetime | None = None) -> UsageRecord:
    """Snapshot a level into the period. Idempotent per (meter, period, source); for
    ``max`` meters the stored value only ever rises (monotonic peak), so a periodic
    worker can call this every tick without inflating usage."""
    per = period or current_period(at)
    key = f"snap:{meter_key}:{per}:{source}"
    rec = (db.query(UsageRecord)
           .filter(UsageRecord.idempotency_key == key).first())
    q = int(quantity or 0)
    if rec is None:
        rec = UsageRecord(tenant_id=tenant_id, meter_key=meter_key, period=per,
                          quantity=q, idempotency_key=key, source=source, at=at or _now())
        db.add(rec)
    else:
        agg = (m.aggregation if (m := _meter(db, meter_key)) else "max")
        if agg == "max":
            rec.quantity = max(int(rec.quantity or 0), q)
        else:                       # last / sum-snapshot → the latest level wins
            rec.quantity = q
        rec.at = at or _now()
    db.commit()
    return rec


def current(db: Session, tenant_id: str, meter_key: str,
            period: str | None = None) -> int | None:
    """The billable quantity for a meter in a period per its aggregation, or ``None``
    when nothing has been recorded yet (so callers can fall back to a live count)."""
    per = period or current_period()
    m = _meter(db, meter_key)
    agg = m.aggregation if m else "max"
    base = (db.query(UsageRecord)
            .filter(UsageRecord.tenant_id == tenant_id,
                    UsageRecord.meter_key == meter_key, UsageRecord.period == per))
    if base.first() is None:
        return None
    if agg == "sum":
        return int(base.with_entities(func.coalesce(func.sum(UsageRecord.quantity), 0)).scalar() or 0)
    if agg == "last":
        last = base.order_by(UsageRecord.at.desc()).first()
        return int(last.quantity or 0) if last else None
    return int(base.with_entities(func.coalesce(func.max(UsageRecord.quantity), 0)).scalar() or 0)


def current_or_live(db: Session, tenant_id: str, meter_key: str, live) -> int:
    """The metered quantity if any is recorded this period, else a live fallback.
    Keeps the calc stable during rollout: no meter data → identical to before."""
    val = current(db, tenant_id, meter_key)
    return int(val) if val is not None else int(live() or 0)


def snapshot_tenant(db: Session, tenant) -> None:
    """Observe the current usage levels for a tenant into this period (idempotent)."""
    from . import entitlements
    users = entitlements.get_usage(db, tenant, "protected_users")
    data_tb = entitlements.get_usage(db, tenant, "protected_data_tb")
    observe(db, tenant.id, "protected_users", users)
    observe(db, tenant.id, "protected_data_tb", data_tb)
    # Arkive Cloud stored TB — proxied by protected data until a cloud-specific
    # counter exists; keeps the calc's metered add-on basis unchanged.
    observe(db, tenant.id, "cloud_stored_tb", data_tb)


def snapshot_all(db: Session) -> int:
    """Snapshot usage for every tenant — driven by the hourly billing worker."""
    from .models import Tenant
    ensure_defaults(db)
    n = 0
    for t in db.query(Tenant).all():
        try:
            snapshot_tenant(db, t)
            n += 1
        except Exception:  # noqa: BLE001 — one tenant must not stop the sweep
            db.rollback()
    return n


def tenant_view(db: Session, tenant) -> dict:
    """Current-period metered usage for a tenant (admin/customer visibility)."""
    per = current_period()
    out = []
    for m in db.query(UsageMeter).all():
        out.append({"key": m.key, "name": m.name, "unit": m.unit,
                    "aggregation": m.aggregation, "period": per,
                    "quantity": current(db, tenant.id, m.key) or 0})
    return {"tenant_id": tenant.id, "period": per, "meters": out}
