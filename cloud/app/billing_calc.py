"""Deterministic billing calculation — the single reproducible engine that turns a
tenant's commercial state (catalog plan version + add-ons + licensed quantities +
usage) into itemized charges. All money is integer minor-units (cents); every line
references the exact price version it came from so an invoice can be reproduced.

Model (one consistent rule everywhere — config, preview, invoice, reporting):
  billable quantity = max(0, licensed − included);   included + billable shown separately.
Seats/members are a plan component priced by the plan version; add-ons that GRANT
seats fold their quantity into the licensed count (never double-charged). Other
add-ons are their own line items priced from the catalog.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

_TB = 1024 ** 4


@dataclass
class Line:
    key: str
    label: str
    quantity: int
    unit_price_cents: int
    amount_cents: int
    included_qty: int = 0
    licensed_qty: int = 0
    kind: str = "recurring"          # recurring | one_time
    source: str = ""                 # e.g. "plan v3", "addon:m365"
    detail: dict = field(default_factory=dict)


@dataclass
class Calc:
    tenant_id: str
    plan: str
    currency: str
    price_version: int
    lines: list[Line] = field(default_factory=list)
    recurring_cents: int = 0
    one_time_cents: int = 0

    def as_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id, "plan": self.plan, "currency": self.currency,
            "price_version": self.price_version,
            "recurring_cents": self.recurring_cents, "one_time_cents": self.one_time_cents,
            "recurring_display": f"${self.recurring_cents / 100:.2f}",
            "lines": [{
                "key": l.key, "label": l.label, "quantity": l.quantity,
                "unit_price_cents": l.unit_price_cents, "amount_cents": l.amount_cents,
                "included_qty": l.included_qty, "licensed_qty": l.licensed_qty,
                "kind": l.kind, "source": l.source, "detail": l.detail,
            } for l in self.lines],
        }


def _tb(bytes_: int) -> int:
    return max(0, round((bytes_ or 0) / _TB))


def calculate(db: Session, tenant, *, overrides: dict | None = None) -> Calc:
    """Compute the recurring + one-time charges for a tenant's current commercial
    state. ``overrides`` (plan / licensed_tb / addons[{code,quantity}]) lets a
    preview evaluate a hypothetical change without persisting it."""
    from . import catalog, entitlements
    from .entitlements import addons as addon_svc
    from .entitlements.models import AddOn
    from .models import User

    ov = overrides or {}
    plan = str(ov.get("plan") or getattr(tenant, "plan", "") or "").lower()
    pricing = catalog.plan_pricing(db, plan)
    src = f"plan v{pricing.get('version', 0)}"
    owner = (db.query(User)
             .filter(User.tenant_id == tenant.id, User.role.in_(("owner", "admin"))).first())

    calc = Calc(tenant_id=tenant.id, plan=plan, currency=pricing.get("currency", "USD"),
                price_version=int(pricing.get("version", 0)))

    def add(**kw):
        line = Line(**kw)
        calc.lines.append(line)
        if line.kind == "one_time":
            calc.one_time_cents += line.amount_cents
        else:
            calc.recurring_cents += line.amount_cents

    # 1) Base subscription.
    base = int(pricing.get("base_price_cents", 0) or 0)
    if base:
        add(key="base", label="Base subscription", quantity=1,
            unit_price_cents=base, amount_cents=base, source=src)

    # 2) Protected data capacity (billable = licensed − included).
    lic_tb = ov.get("licensed_tb")
    licensed_tb = int(lic_tb) if lic_tb is not None else _tb(int(getattr(tenant, "licensed_bytes", 0) or 0))
    licensed_tb = max(licensed_tb, int(pricing.get("min_tb", 0) or 0))
    incl_tb = int(pricing.get("included_tb", 0) or 0)
    bill_tb = max(0, licensed_tb - incl_tb)
    rate_tb = int(pricing.get("protection_cents_per_tb", 0) or 0)
    if rate_tb and (bill_tb or incl_tb):
        add(key="protected_data", label="Protected data", quantity=bill_tb,
            unit_price_cents=rate_tb, amount_cents=bill_tb * rate_tb,
            included_qty=incl_tb, licensed_qty=licensed_tb, source=src)

    # Fold seat-granting add-ons into the licensed seat/member counts (priced by the
    # plan, not the add-on) so they're never double-charged.
    active = list(addon_svc.active_for_tenant(db, tenant.id))
    by_code = {a.code: a for a in db.query(AddOn).all()}
    _apply_addon_overrides(active, ov, by_code)
    seat_add = mem_add = 0
    for ta in active:
        a = by_code.get(ta.addon_code)
        if not a:
            continue
        ents = a.entitlements or {}
        if "protected_users" in ents:
            seat_add += int(ents["protected_users"]) * max(1, int(ta.quantity or 1))
        if "family_members" in ents:
            mem_add += int(ents["family_members"]) * max(1, int(ta.quantity or 1))

    # 3) Protected users (Business/Enterprise) — billable = licensed − included.
    incl_users = int(pricing.get("included_users", 0) or 0)
    licensed_users = incl_users + seat_add
    bill_users = max(0, licensed_users - incl_users)
    rate_user = int(pricing.get("per_user_cents", 0) or 0)
    if rate_user and licensed_users:
        add(key="protected_users", label="Protected users", quantity=bill_users,
            unit_price_cents=rate_user, amount_cents=bill_users * rate_user,
            included_qty=incl_users, licensed_qty=licensed_users, source=src)

    # 4) Family members — billable over the included allowance.
    incl_mem = int(pricing.get("included_members", 0) or 0)
    licensed_mem = incl_mem + mem_add
    bill_mem = max(0, licensed_mem - incl_mem)
    rate_mem = int(pricing.get("per_member_cents", 0) or 0)
    if rate_mem and licensed_mem:
        add(key="family_members", label="Family members", quantity=bill_mem,
            unit_price_cents=rate_mem, amount_cents=bill_mem * rate_mem,
            included_qty=incl_mem, licensed_qty=licensed_mem, source=src)

    # 5) Non-seat add-ons — priced from the catalog (per unit / per user / per TB / metered).
    from . import metering
    protected_users_used = entitlements.get_usage(db, tenant, "protected_users")
    cloud_tb_used = metering.current_or_live(
        db, tenant.id, "cloud_stored_tb",
        lambda: entitlements.get_usage(db, tenant, "protected_data_tb"))
    for ta in active:
        a = by_code.get(ta.addon_code)
        if not a:
            continue
        ents = a.entitlements or {}
        if "protected_users" in ents or "family_members" in ents:
            continue  # seat grant — already priced by the plan
        unit = int(ta.price_cents_snapshot or a.price_cents or 0)
        qty = max(1, int(ta.quantity or 1))
        model = a.pricing_model
        if model == "per_user":
            qty = protected_users_used or 1
        elif model in ("per_cloud_tb", "per_tb", "metered"):
            qty = cloud_tb_used  # usage-based (Phase 7 refines with real meters)
        add(key=f"addon:{a.code}", label=a.name or a.code, quantity=qty,
            unit_price_cents=unit, amount_cents=unit * qty,
            source=f"addon:{a.code} v{ta.addon_version}", detail={"pricing_model": model})

    # 6) Appliances (lease + one-time setup) from the plan version's tiers.
    tiers = {int(t.get("capacity_tb", 0)): t for t in (pricing_appliance(db, plan))}
    for sel in (getattr(tenant, "appliance_plan", None) or []):
        cap = int(sel.get("capacity_tb", 0) or 0)
        qty = int(sel.get("qty", 1) or 1)
        t = tiers.get(cap)
        if not t or qty <= 0:
            continue
        monthly = int(t.get("monthly_cents", 0) or 0)
        setup = int(t.get("setup_cents", 0) or 0)
        if monthly:
            add(key=f"appliance:{cap}", label=f"Appliance lease ({t.get('model') or f'{cap} TB'})",
                quantity=qty, unit_price_cents=monthly, amount_cents=monthly * qty, source=src)
        if setup:
            add(key=f"appliance_setup:{cap}", label=f"Appliance setup ({cap} TB)",
                quantity=qty, unit_price_cents=setup, amount_cents=setup * qty,
                kind="one_time", source=src)

    return calc


def pricing_appliance(db: Session, plan: str) -> list:
    """Appliance tiers (cents) for a plan — from the effective plan version, else the
    legacy PricingConfig (float → cents)."""
    from . import catalog
    v = catalog.effective_version(db, plan)
    if v is not None and v.appliance_tiers:
        return v.appliance_tiers
    from .api.billing import get_pricing
    p = get_pricing(db)
    return [{"capacity_tb": t.get("capacity_tb"), "model": t.get("model", ""),
             "monthly_cents": int(round(float(t.get("monthly", 0) or 0) * 100)),
             "setup_cents": int(round(float(t.get("setup", 0) or 0) * 100))}
            for t in (p.appliance_tiers or [])]


def _apply_addon_overrides(active: list, ov: dict, by_code: dict) -> None:
    """Fold hypothetical add-on changes into the active list for a preview."""
    from .entitlements.models import TenantAddOn
    changes = ov.get("addons")
    if not changes:
        return
    idx = {ta.addon_code: ta for ta in active}
    for ch in changes:
        code = str(ch.get("code") or "").lower()
        qty = int(ch.get("quantity", 0) or 0)
        if code not in by_code:
            continue
        if qty <= 0:
            active[:] = [ta for ta in active if ta.addon_code != code]
        elif code in idx:
            idx[code].quantity = qty
        else:
            a = by_code[code]
            active.append(TenantAddOn(tenant_id="", addon_code=code, quantity=qty,
                                      price_cents_snapshot=a.price_cents, addon_version=a.version))


def preview(db: Session, tenant, changes: dict) -> dict:
    """Current vs proposed recurring totals + the per-line delta for a change."""
    current = calculate(db, tenant)
    proposed = calculate(db, tenant, overrides=changes or {})
    return {
        "current": current.as_dict(), "proposed": proposed.as_dict(),
        "delta_cents": proposed.recurring_cents - current.recurring_cents,
        "one_time_cents": proposed.one_time_cents,
    }
