"""Legacy → new-engine billing migration (Phase 12).

The safe, reversible cutover of a tenant from the legacy PricingConfig charge
(``_price_breakdown``) to the deterministic ``billing_calc`` total. Design:

  - **dry-run first** (``preview``): report legacy vs calc parity + exactly what a
    migration would change, WITHOUT writing anything,
  - **grandfather** when the calc differs from what the customer pays today: pin a
    per-tenant price override so the cutover is price-neutral (they keep paying the
    legacy amount) until an admin deliberately re-prices,
  - **compensating snapshot** on every apply (``BillingMigration``): the previous
    ``billing_source`` + profile amount, so a migration can be rolled back,
  - **bulk backfill** (``backfill_all``): materialize the persisted subscription for
    every tenant so the new model is populated before/independent of cutover.

Money is integer minor-units (cents). Control-plane only (billing is CP-authoritative).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .models import BillingMigration, BillingProfile, Tenant, User


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Per-tenant grandfathered price lock (a SystemSetting key). When present, it forces
# the tenant's charged amount to a fixed value regardless of legacy/calc — used to
# keep a cutover price-neutral for an existing customer.
def _lock_key(tenant_id: str) -> str:
    return f"billing_price_lock:{tenant_id}"


def price_lock(db: Session, tenant_id: str) -> int | None:
    from .models import SystemSetting
    row = db.get(SystemSetting, _lock_key(tenant_id))
    if row is None or not (row.value or "").strip():
        return None
    try:
        return int(row.value)
    except ValueError:
        return None


def _set_price_lock(db: Session, tenant_id: str, cents: int | None) -> None:
    from .models import SystemSetting
    row = db.get(SystemSetting, _lock_key(tenant_id))
    if cents is None:
        if row is not None:
            db.delete(row)
    elif row is None:
        db.add(SystemSetting(key=_lock_key(tenant_id), value=str(int(cents))))
    else:
        row.value = str(int(cents))


def _owner(db: Session, tenant_id: str) -> User | None:
    return (db.query(User).filter(User.tenant_id == tenant_id)
            .order_by(User.created_at.asc()).first())


def _amounts(db: Session, tenant: Tenant) -> tuple[int, int]:
    """(legacy_cents, calc_cents) for a tenant, computed independently of the
    tenant's current billing_source."""
    from .api.billing import _legacy_amount_cents, _calc_amount_cents
    owner = _owner(db, tenant.id)
    try:
        legacy = int(_legacy_amount_cents(db, owner, tenant)[0]) if owner else 0
    except Exception:  # noqa: BLE001
        legacy = 0
    try:
        calc = int(_calc_amount_cents(db, tenant)[0])
    except Exception:  # noqa: BLE001
        calc = 0
    return legacy, calc


def preview(db: Session, tenant: Tenant) -> dict:
    """What a migration would do — legacy vs calc, delta, current state. No writes."""
    from .api.billing import _billing_source
    from . import subscriptions
    legacy, calc = _amounts(db, tenant)
    prof = db.query(BillingProfile).filter(BillingProfile.tenant_id == tenant.id).first()
    sub = subscriptions.get_subscription(db, tenant.id)
    match = legacy == calc
    return {
        "tenant_id": tenant.id, "plan": tenant.plan,
        "current_source": _billing_source(db, tenant),
        "legacy_cents": legacy, "calc_cents": calc, "delta_cents": calc - legacy,
        "match": match,
        "price_lock_cents": price_lock(db, tenant.id),
        "profile_amount_cents": (int(prof.amount_cents or 0) if prof else None),
        "profile_active": (bool(prof.active) if prof else None),
        "subscription_materialized": sub is not None,
        "recommendation": ("cutover" if match else "grandfather"),
        "note": ("The new engine reproduces today's charge — a cutover is price-neutral."
                 if match else
                 "The new engine differs from today's charge — grandfather to keep the "
                 "price, or cut over to re-price to the calc total."),
    }


def apply(db: Session, tenant: Tenant, *, mode: str = "calc", actor: str | None = None,
          note: str = "") -> dict:
    """Cut a tenant over to billing_source=calc. ``mode``:
      - ``calc``: charge the calc total going forward (re-prices if it differs),
      - ``grandfather``: cut over but PIN the current legacy amount (price-neutral).
    Materializes the subscription, refreshes the profile amount, and records a
    compensating BillingMigration for rollback."""
    from .api.billing import _billing_source, _set_billing_source
    from . import subscriptions
    legacy, calc = _amounts(db, tenant)
    prev_source = _billing_source(db, tenant)
    prof = db.query(BillingProfile).filter(BillingProfile.tenant_id == tenant.id).first()
    prev_amount = int(prof.amount_cents or 0) if prof else 0

    # Grandfather: lock the price to today's legacy amount so the cutover is neutral.
    if mode == "grandfather":
        _set_price_lock(db, tenant.id, legacy)
        new_amount = legacy
    else:
        _set_price_lock(db, tenant.id, None)
        new_amount = calc

    _set_billing_source(db, tenant.id, "calc")
    # Materialize the persisted subscription from the calc.
    subscriptions.sync_from_calc(db, tenant)
    # Bring the billing profile's charged amount in line immediately.
    if prof is not None:
        prof.amount_cents = new_amount
        prof.plan_id = tenant.plan or prof.plan_id
    rec = BillingMigration(
        tenant_id=tenant.id, mode=mode, status="applied",
        prev_source=prev_source, new_source="calc",
        legacy_cents=legacy, calc_cents=calc,
        prev_amount_cents=prev_amount, new_amount_cents=new_amount,
        actor=actor, note=note)
    db.add(rec)
    db.commit()
    return {"ok": True, "migration_id": rec.id, "mode": mode,
            "prev_amount_cents": prev_amount, "new_amount_cents": new_amount,
            **preview(db, tenant)}


def rollback(db: Session, tenant: Tenant, *, actor: str | None = None) -> dict:
    """Revert the tenant's most recent applied migration — restore the previous
    billing_source, clear any grandfather lock, and restore the profile amount."""
    from .api.billing import _set_billing_source
    rec = (db.query(BillingMigration)
           .filter(BillingMigration.tenant_id == tenant.id, BillingMigration.status == "applied")
           .order_by(BillingMigration.created_at.desc()).first())
    if rec is None:
        return {"ok": False, "error": "no applied migration to roll back"}
    # Restore billing_source (empty prev means: clear the per-tenant override → inherit).
    _set_billing_source(db, tenant.id, rec.prev_source or None)
    _set_price_lock(db, tenant.id, None)
    prof = db.query(BillingProfile).filter(BillingProfile.tenant_id == tenant.id).first()
    if prof is not None:
        prof.amount_cents = int(rec.prev_amount_cents or 0)
    rec.status = "rolled_back"
    rec.rolled_back_at = _now()
    db.commit()
    return {"ok": True, "migration_id": rec.id, "restored_amount_cents": int(rec.prev_amount_cents or 0)}


def backfill_all(db: Session, *, limit: int | None = None) -> dict:
    """Materialize the persisted subscription for every tenant (idempotent). Safe to
    run repeatedly; does NOT change billing_source or charge anything."""
    from . import subscriptions
    q = db.query(Tenant).order_by(Tenant.created_at.asc())
    if limit:
        q = q.limit(limit)
    ok = failed = 0
    for t in q.all():
        try:
            subscriptions.sync_from_calc(db, t)
            ok += 1
        except Exception:  # noqa: BLE001 — one tenant must not stop the backfill
            db.rollback()
            failed += 1
    return {"ok": True, "materialized": ok, "failed": failed}


def history(db: Session, tenant_id: str) -> list[dict]:
    rows = (db.query(BillingMigration)
            .filter(BillingMigration.tenant_id == tenant_id)
            .order_by(BillingMigration.created_at.desc()).all())
    return [{"id": r.id, "mode": r.mode, "status": r.status,
             "prev_source": r.prev_source, "new_source": r.new_source,
             "legacy_cents": r.legacy_cents, "calc_cents": r.calc_cents,
             "prev_amount_cents": r.prev_amount_cents, "new_amount_cents": r.new_amount_cents,
             "actor": r.actor, "note": r.note,
             "created_at": r.created_at.isoformat() if r.created_at else None,
             "rolled_back_at": r.rolled_back_at.isoformat() if r.rolled_back_at else None}
            for r in rows]
