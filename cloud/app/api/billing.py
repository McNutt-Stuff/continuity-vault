"""Protection Setup / billing.

The customer's Protection Setup page reads its pricing from ``GET /billing/pricing``
(admin-managed platform pricing) and its current plan + computed monthly cost +
data-value estimate from ``GET /billing/plan``. Saving the page (``PUT``) records
which storage tiers are enabled (feature gating), how much data protection they
license, and their desired appliances.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import audit, security
from ..db import get_db
from ..models import (
    BillingCharge, BillingProfile, PaymentMethod, PricingConfig,
    SearchDocument, Tenant, User, Vault,
)
from .dashboard import _OBJECT_BUCKETS, _bucket_for

router = APIRouter(prefix="/billing", tags=["billing"])

logger = logging.getLogger("cv.billing")

TB = 1024 ** 4  # 1 TB (binary) — matches the byte sizes shown in the UI

# Grace period between unsubscribing from Arkive Cloud and permanent deletion.
CLOUD_DELETE_GRACE_DAYS = 30

# Storage tiers the customer can enable (feature gating keys).
STORAGE_TIERS = [
    {
        "id": "cv-cloud",
        "title": "Arkive Cloud",
        "tagline": "Fully-managed, multi-region cloud vault",
        "icon": "cloud", "color": "#4f7cff",
        "billing": "per-tb",
        "benefits": [
            "Zero setup — protected in minutes",
            "Customer-managed keys; Arkive can never decrypt",
            "Multi-region redundancy & managed failover",
            "Post-quantum hybrid encryption at rest",
        ],
    },
    {
        "id": "appliance",
        "title": "Arkive Secure Hardware",
        "tagline": "Offline appliance you own and keep on-site",
        "icon": "server", "color": "#35d0a5",
        "billing": "device",
        "benefits": [
            "Physical, air-gapped copy under your control",
            "Full local recovery during an internet outage",
            "Tamper-evident HSM-sealed storage",
            "One-time hardware cost — no monthly storage fee",
        ],
    },
    {
        "id": "customer-cloud",
        "title": "Your Cloud (S3 / Azure)",
        "tagline": "Bring your own bucket — you pay the provider",
        "icon": "cloud", "color": "#f5a623",
        "billing": "byo",
        "benefits": [
            "Data stays in your own AWS or Azure account",
            "Independent of the Arkive cloud",
            "Customer-managed or zero-knowledge keys",
            "You pay your provider directly (estimate below)",
        ],
    },
]

_DEFAULT_TIERS = [
    {"capacity_tb": 1, "monthly": 25, "setup": 99, "model": "CV Edge 1"},
    {"capacity_tb": 5, "monthly": 59, "setup": 149, "model": "CV Edge 5"},
    {"capacity_tb": 10, "monthly": 99, "setup": 199, "model": "CV Edge 10"},
    {"capacity_tb": 25, "monthly": 199, "setup": 299, "model": "CV Edge 25"},
    {"capacity_tb": 100, "monthly": 499, "setup": 499, "model": "CV Edge 100"},
]
_DEFAULT_VALUE = {"email": 2, "credential": 50, "document": 15, "photo": 5,
                  "media": 8, "file": 3, "contact": 1}

# Recurring license tiers. Each tenant is on one tier (Tenant.plan == id); the
# tier sets the per-TB/month data-protection rate and the minimum licensed TB.
_DEFAULT_PLANS = [
    {"id": "personal", "name": "Personal", "price_per_tb_month": 10.0, "min_tb": 0},
    {"id": "consumer", "name": "Consumer", "price_per_tb_month": 8.0, "min_tb": 1},
    {"id": "family", "name": "Family", "price_per_tb_month": 6.0, "min_tb": 2},
    {"id": "business", "name": "Business", "price_per_tb_month": 5.0, "min_tb": 5},
    {"id": "enterprise", "name": "Enterprise", "price_per_tb_month": 4.0, "min_tb": 25},
]


def get_pricing(db: Session) -> PricingConfig:
    """Fetch the single platform pricing row, creating sensible defaults once."""
    p = db.get(PricingConfig, "default")
    if p is None:
        p = PricingConfig(id="default", appliance_tiers=_DEFAULT_TIERS,
                          data_value_per_type=_DEFAULT_VALUE,
                          license_plans=_DEFAULT_PLANS)
        db.add(p)
        db.commit()
        db.refresh(p)
    # Backfill JSON defaults if an older row left them empty, and migrate any
    # legacy one-time-price tiers to the lease model (monthly + setup).
    if not p.appliance_tiers or any("monthly" not in t for t in p.appliance_tiers):
        p.appliance_tiers = _DEFAULT_TIERS
    if not p.data_value_per_type:
        p.data_value_per_type = _DEFAULT_VALUE
    if not p.license_plans:
        p.license_plans = _DEFAULT_PLANS
    return p


def effective_plan(p: PricingConfig, plan_id: str | None) -> dict:
    """Resolve the license tier a tenant is on. Falls back to the first tier,
    then to the legacy flat protection rate so pricing always resolves."""
    plans = p.license_plans or _DEFAULT_PLANS
    if plan_id:
        for pl in plans:
            if pl.get("id") == plan_id:
                return pl
    if plans:
        return plans[0]
    return {"id": "custom", "name": "Custom",
            "price_per_tb_month": p.protection_price_per_tb_month, "min_tb": 0}


def pricing_public(p: PricingConfig) -> dict:
    return {
        "currency": p.currency,
        "protection_price_per_tb_month": p.protection_price_per_tb_month,
        "cloud_price_per_tb_month": p.cloud_price_per_tb_month,
        "s3_price_per_tb_month": p.s3_price_per_tb_month,
        "azure_price_per_tb_month": p.azure_price_per_tb_month,
        "license_plans": p.license_plans or _DEFAULT_PLANS,
        "appliance_tiers": p.appliance_tiers or _DEFAULT_TIERS,
        "data_value_per_type": p.data_value_per_type or _DEFAULT_VALUE,
        "tiers": STORAGE_TIERS,
    }


def _agg_by_doctype(db: Session, *conds) -> tuple[int, int, dict]:
    """(objects, protected bytes, per-bucket counts) over the CURRENT row of each
    logical object (is_current ⇒ one row per object) — a single GROUP BY instead of
    hauling every version/destination row into Python to de-duplicate."""
    total = 0
    used_bytes = 0
    by_bucket: dict = {}
    for dt, cnt, sz in (db.query(SearchDocument.doc_type, func.count(),
                                 func.coalesce(func.sum(SearchDocument.size_bytes), 0))
                        .filter(SearchDocument.is_current.is_(True), *conds)
                        .group_by(SearchDocument.doc_type).all()):
        total += int(cnt)
        used_bytes += int(sz or 0)
        bk = _bucket_for(dt)["key"]
        by_bucket[bk] = by_bucket.get(bk, 0) + int(cnt)
    return total, used_bytes, by_bucket


def _usage(db: Session, tenant_id: str) -> tuple[int, int, dict]:
    """Deduped object count, protected bytes, and per-bucket counts."""
    return _agg_by_doctype(db, SearchDocument.tenant_id == tenant_id)


@router.get("/pricing")
def get_pricing_public(tenant: Tenant = Depends(security.get_tenant),
                       db: Session = Depends(get_db)):
    return pricing_public(get_pricing(db))


def _compute_plan(db: Session, tenant: Tenant) -> dict:
    p = get_pricing(db)
    plan = effective_plan(p, tenant.plan)
    total, used_bytes, by_bucket = _usage(db, tenant.id)
    out = _price_breakdown(
        p, plan=plan,
        licensed_bytes=int(tenant.licensed_bytes or 0),
        options=list(tenant.protection_options or []),
        appliance_plan=list(tenant.appliance_plan or []),
        used_bytes=used_bytes, by_bucket=by_bucket,
        charge_on="licensed")
    out["objects_total"] = total
    return out


def _price_breakdown(p: PricingConfig, *, plan: dict, licensed_bytes: int,
                     options: list, appliance_plan: list,
                     used_bytes: int, by_bucket: dict,
                     charge_on: str = "licensed") -> dict:
    """The ONE canonical pricing calculation shared by the customer's Protection
    Setup (`GET /billing/plan`) and every admin view (tenant detail, reports,
    global users). total_monthly = base plan on billable TB + enabled storage
    channels (used TB) + appliance lease.

    charge_on="licensed" bills protection on the committed licensed capacity
    (org tenants). charge_on="used" bills pay-as-you-go on protected data
    (personal accounts on the Personal plan)."""
    rate = float(plan.get("price_per_tb_month", p.protection_price_per_tb_month))
    min_tb = float(plan.get("min_tb", 0) or 0)
    options = list(options or [])
    used_tb = used_bytes / TB
    licensed_tb = (licensed_bytes or 0) / TB
    # The tier's minimum is the floor for what a customer pays for.
    floor_tb = used_tb if charge_on == "used" else licensed_tb
    billable_tb = max(floor_tb, min_tb)

    # Object-value breakdown → estimated worth of the protected data.
    values = p.data_value_per_type or _DEFAULT_VALUE
    breakdown = []
    data_value_total = 0.0
    for b in _OBJECT_BUCKETS:
        n = by_bucket.get(b["key"], 0)
        if not n:
            continue
        each = float(values.get(b["key"], 0))
        breakdown.append({"key": b["key"], "label": b["label"], "icon": b["icon"],
                          "color": b["color"], "count": n, "value_each": each,
                          "value_total": round(n * each, 2)})
        data_value_total += n * each

    # Appliance selection → leased: a monthly fee + a one-time setup fee.
    tiers = {t["capacity_tb"]: t for t in (p.appliance_tiers or _DEFAULT_TIERS)}
    appliance_setup = 0.0
    appliance_monthly = 0.0
    appliance_lines = []
    for sel in (appliance_plan or []):
        cap = sel.get("capacity_tb")
        qty = int(sel.get("qty") or 0)
        t = tiers.get(cap)
        if not t or qty <= 0:
            continue
        monthly = float(t.get("monthly") or 0)
        setup = float(t.get("setup") or 0)
        appliance_monthly += qty * monthly
        appliance_setup += qty * setup
        appliance_lines.append({"capacity_tb": cap, "model": t["model"], "qty": qty,
                                "unit_monthly": monthly, "unit_setup": setup,
                                "monthly_total": qty * monthly, "setup_total": qty * setup})

    protection_monthly = round(billable_tb * rate, 2)
    cloud_storage_monthly = round(used_tb * p.cloud_price_per_tb_month, 2) if "cv-cloud" in options else 0.0
    third_party_monthly = round(used_tb * p.s3_price_per_tb_month, 2) if "customer-cloud" in options else 0.0
    appliance_monthly = round(appliance_monthly, 2)
    total_monthly = round(protection_monthly + cloud_storage_monthly + appliance_monthly, 2)
    annual_cost = round(total_monthly * 12, 2)
    value_ratio = round(data_value_total / annual_cost, 1) if annual_cost > 0 else None

    return {
        "options": options,
        "currency": p.currency,
        "license_plan": {
            "id": plan.get("id"), "name": plan.get("name", "Plan"),
            "price_per_tb_month": rate, "min_tb": min_tb,
        },
        "charge_on": charge_on,
        "licensed_bytes": int(licensed_bytes or 0),
        "licensed_tb": round(licensed_tb, 3),
        "billable_tb": round(billable_tb, 3),
        "min_tb": min_tb,
        "used_bytes": int(used_bytes or 0),
        "used_tb": round(used_tb, 4),
        "percent": round(min(100.0, used_bytes / licensed_bytes * 100), 1) if licensed_bytes else None,
        "objects_total": 0,
        "value_breakdown": breakdown,
        "data_value_total": round(data_value_total, 2),
        "appliance_plan": appliance_lines,
        "costs": {
            "protection_monthly": protection_monthly,
            "cloud_storage_monthly": cloud_storage_monthly,
            "third_party_estimate_monthly": third_party_monthly,
            "appliance_monthly": appliance_monthly,
            "appliance_setup_one_time": round(appliance_setup, 2),
            "total_monthly": total_monthly,
            "annual_cost": annual_cost,
        },
        "value_ratio": value_ratio,
    }


def _user_usage(db: Session, user) -> tuple[int, int, dict]:
    """Deduped object count + protected bytes for a single account, scoped to
    the vaults it owns — the SAME logical-size basis as tenant `_usage`."""
    vault_ids = [vid for (vid,) in
                 db.query(Vault.id).filter(Vault.owner_user_id == user.id).all()]
    if not vault_ids:
        return 0, 0, {}
    return _agg_by_doctype(db, SearchDocument.vault_id.in_(vault_ids))


def bulk_user_usage(db: Session, user_ids: list[str]) -> dict:
    """One pass (objects, bytes, by_bucket) per user for a whole batch of accounts,
    so the admin Users list doesn't run a separate index scan per user (which made
    the page time out). Dedup is per (owner, source_type, object_id)."""
    out: dict = {uid: (0, 0, {}) for uid in user_ids}
    if not user_ids:
        return out
    vrows = (db.query(Vault.id, Vault.owner_user_id)
             .filter(Vault.owner_user_id.in_(user_ids)).all())
    vault_owner = {vid: oid for vid, oid in vrows}
    vault_ids = list(vault_owner.keys())
    if not vault_ids:
        return out
    agg: dict = {}
    for vid, dt, cnt, sz in (db.query(
                SearchDocument.vault_id, SearchDocument.doc_type, func.count(),
                func.coalesce(func.sum(SearchDocument.size_bytes), 0))
            .filter(SearchDocument.vault_id.in_(vault_ids),
                    SearchDocument.is_current.is_(True))
            .group_by(SearchDocument.vault_id, SearchDocument.doc_type).all()):
        owner = vault_owner.get(vid)
        if not owner:
            continue
        a = agg.setdefault(owner, {"total": 0, "bytes": 0, "buckets": {}})
        a["total"] += int(cnt)
        a["bytes"] += int(sz or 0)
        bk = _bucket_for(dt)["key"]
        a["buckets"][bk] = a["buckets"].get(bk, 0) + int(cnt)
    for uid, a in agg.items():
        out[uid] = (a["total"], a["bytes"], a["buckets"])
    return out


def user_plan_from_usage(db: Session, user, tenant: Tenant,
                         total: int, used_bytes: int, by_bucket: dict) -> dict:
    """Same canonical per-account plan as user_plan(), but with usage supplied by
    the caller (see bulk_user_usage) so a batch view needn't rescan per user."""
    p = get_pricing(db)
    shared = (tenant.tenant_type or "dedicated") == "shared"
    plan = effective_plan(p, "personal" if shared else tenant.plan)
    out = _price_breakdown(
        p, plan=plan,
        licensed_bytes=used_bytes if shared else int(tenant.licensed_bytes or 0),
        options=list(user.protection_options or []) if shared else list(tenant.protection_options or []),
        appliance_plan=[] if shared else list(tenant.appliance_plan or []),
        used_bytes=used_bytes, by_bucket=by_bucket,
        charge_on="used" if shared else "licensed")
    out["objects_total"] = total
    return out


def user_protection_options(user, tenant: Tenant) -> list[str]:
    """Protection destinations for this account. Shared-tenant personal accounts
    each own their selection (the tenant is a pool of unrelated accounts); org
    tenants share the tenant-wide selection."""
    if user is not None and (tenant.tenant_type or "dedicated") == "shared":
        return list(user.protection_options or [])
    return list(tenant.protection_options or [])


def cloud_delete_target(user, tenant: Tenant):
    """The row that holds the pending Arkive Cloud deletion timer for this scope:
    the user for shared/personal accounts, else the tenant."""
    return user if (user is not None and (tenant.tenant_type or "dedicated") == "shared") else tenant


def _apply_cloud_unsubscribe(target, prev: set[str], new: set[str]) -> None:
    """Arm the 30-day deletion timer when Arkive Cloud is dropped; cancel it when re-added."""
    if "cv-cloud" in prev and "cv-cloud" not in new:
        if not target.cloud_delete_at:
            target.cloud_delete_at = datetime.utcnow() + timedelta(days=CLOUD_DELETE_GRACE_DAYS)
    elif "cv-cloud" in new and target.cloud_delete_at:
        target.cloud_delete_at = None


def cloud_stored_summary(db: Session, tenant: Tenant, vault_ids) -> tuple[int, int]:
    """(object_count, bytes) currently stored in Arkive Cloud for the given vaults."""
    from ..models import SearchDocument, SnapshotReceipt
    if not vault_ids:
        return (0, 0)
    receipts = (db.query(SnapshotReceipt.snapshot_id, SnapshotReceipt.total_bytes)
                .filter(SnapshotReceipt.tenant_id == tenant.id,
                        SnapshotReceipt.vault_id.in_(vault_ids),
                        SnapshotReceipt.destination == "cv-cloud").all())
    cloud_snaps = {sid for sid, _ in receipts}
    if not cloud_snaps:
        return (0, 0)
    total_bytes = sum(int(b or 0) for _, b in receipts)
    seen = {(st, oid) for st, oid in
            db.query(SearchDocument.source_type, SearchDocument.object_id)
            .filter(SearchDocument.tenant_id == tenant.id,
                    SearchDocument.snapshot_id.in_(list(cloud_snaps))).all()}
    return (len(seen), total_bytes)


def purge_cloud_data(db: Session, tenant: Tenant, vault_ids) -> dict:
    """Permanently delete the scope's Arkive Cloud copies (their receipts, which
    makes the ciphertext unfindable/undecryptable). Irreversible. Caller commits."""
    from ..models import SnapshotReceipt
    if not vault_ids:
        return {"receipts": 0}
    n = db.query(SnapshotReceipt).filter(
        SnapshotReceipt.tenant_id == tenant.id,
        SnapshotReceipt.vault_id.in_(vault_ids),
        SnapshotReceipt.destination == "cv-cloud").delete(synchronize_session=False)
    return {"receipts": int(n or 0)}


def plan_view(db: Session, user, tenant: Tenant) -> dict:
    """Shared accounts see their own per-account plan; org members see the tenant plan."""
    if (tenant.tenant_type or "dedicated") == "shared":
        return user_plan(db, user, tenant)
    return _compute_plan(db, tenant)


def user_plan(db: Session, user, tenant: Tenant) -> dict:
    """Per-account billing via the canonical pricing. Shared-tenant accounts are
    pay-as-you-go on the Personal base plan (charged on used data); members of an
    org tenant inherit the tenant's plan + protection selections."""
    p = get_pricing(db)
    shared = (tenant.tenant_type or "dedicated") == "shared"
    plan = effective_plan(p, "personal" if shared else tenant.plan)
    total, used_bytes, by_bucket = _user_usage(db, user)
    out = _price_breakdown(
        p, plan=plan,
        licensed_bytes=used_bytes if shared else int(tenant.licensed_bytes or 0),
        options=list(user.protection_options or []) if shared else list(tenant.protection_options or []),
        appliance_plan=[] if shared else list(tenant.appliance_plan or []),
        used_bytes=used_bytes, by_bucket=by_bucket,
        charge_on="used" if shared else "licensed")
    out["objects_total"] = total
    return out



@router.get("/plan")
def get_plan(principal: security.Principal = Depends(security.get_principal),
             tenant: Tenant = Depends(security.get_tenant),
             db: Session = Depends(get_db)):
    return plan_view(db, db.get(User, principal.user_id), tenant)


class PlanUpdate(BaseModel):
    options: list[str] | None = None
    licensed_tb: float | None = None
    appliance_plan: list[dict] | None = None


_OPTION_LABELS = {"cv-cloud": "Arkive Cloud", "appliance": "Secure Appliance",
                  "customer-cloud": "Your own cloud storage"}


def _ap_sig(plan) -> tuple:
    """Stable signature of an appliance plan (capacity, qty pairs) for diffing."""
    return tuple(sorted((float(s.get("capacity_tb") or 0), int(s.get("qty") or 0))
                        for s in (plan or []) if int(s.get("qty") or 0) > 0))


def _plan_sig(options, licensed_bytes, appliance_plan) -> tuple:
    """Everything that affects the plan/price — so ANY change reliably notifies."""
    return (tuple(sorted(options or [])), int(licensed_bytes or 0), _ap_sig(appliance_plan))


def _money(n, currency: str = "USD") -> str:
    sym = "$" if currency == "USD" else ""
    return f"{sym}{float(n or 0):,.2f}"


def _notify_plan_change(db, user, view: dict, summary: list[str]) -> None:
    """Email the acting user a confirmation of their plan/billing changes."""
    if not summary:
        return
    from datetime import datetime, timezone
    from .. import notifications
    costs = view.get("costs") or {}
    cur = view.get("currency", "USD")
    lines = []
    plan_name = (view.get("license_plan") or {}).get("name", "Plan")
    if costs.get("protection_monthly"):
        lines.append({"label": f"{plan_name} protection", "amount": _money(costs["protection_monthly"], cur) + " / mo"})
    if costs.get("cloud_storage_monthly"):
        lines.append({"label": "Arkive Cloud storage", "amount": _money(costs["cloud_storage_monthly"], cur) + " / mo"})
    if costs.get("appliance_monthly"):
        lines.append({"label": "Secure appliance lease", "amount": _money(costs["appliance_monthly"], cur) + " / mo"})
    if costs.get("appliance_setup_one_time"):
        lines.append({"label": "Appliance setup (one-time)", "amount": _money(costs["appliance_setup_one_time"], cur)})
    change = {
        "plan_name": plan_name,
        "effective": datetime.now(timezone.utc).strftime("%B %-d, %Y"),
        "summary": summary,
        "line_items": lines,
        "total_label": "New monthly total",
        "total": _money(costs.get("total_monthly", 0), cur) + " / mo",
    }
    try:
        notifications.send_notification(db, user, "plan_change", change=change)
    except Exception:  # noqa: BLE001
        logger.exception("plan-change notification failed for %s", getattr(user, "email", user.id))


@router.put("/plan")
def update_plan(body: PlanUpdate,
                principal: security.Principal = Depends(security.get_principal),
                tenant: Tenant = Depends(security.get_tenant),
                db: Session = Depends(get_db)):
    valid = {t["id"] for t in STORAGE_TIERS}
    user = db.get(User, principal.user_id)
    # Shared-tenant personal accounts each manage their own protection destinations
    # (no org role required); org tenants keep the security-admin-gated tenant-wide plan.
    if (tenant.tenant_type or "dedicated") == "shared":
        summary: list[str] = []
        if body.options is not None:
            prev = set(user.protection_options or [])
            new = {o for o in body.options if o in valid}
            user.protection_options = list(new)
            _apply_cloud_unsubscribe(user, prev, new)
            for o in sorted(new - prev):
                summary.append(f"Enabled {_OPTION_LABELS.get(o, o)}")
            for o in sorted(prev - new):
                summary.append(f"Disabled {_OPTION_LABELS.get(o, o)}")
        db.commit()
        audit.record(db, actor=principal.user_id, action="billing.plan_updated",
                     tenant_id=tenant.id, resource=user.id,
                     detail={"options": user.protection_options})
        view = plan_view(db, user, tenant)
        _notify_plan_change(db, user, view, summary)
        return view
    if not (security.is_org_admin(principal.role) or principal.is_platform_admin):
        raise HTTPException(403, "security-admin role required")
    # Snapshot the whole plan up-front so we reliably notify on ANY change —
    # including appliance removals / capacity changes the old per-field checks missed.
    before_sig = _plan_sig(tenant.protection_options, tenant.licensed_bytes, tenant.appliance_plan)
    summary = []
    if body.options is not None:
        prev = set(tenant.protection_options or [])
        new = {o for o in body.options if o in valid}
        tenant.protection_options = list(new)
        _apply_cloud_unsubscribe(tenant, prev, new)
        for o in sorted(new - prev):
            summary.append(f"Enabled {_OPTION_LABELS.get(o, o)}")
        for o in sorted(prev - new):
            summary.append(f"Disabled {_OPTION_LABELS.get(o, o)}")
    if body.licensed_tb is not None:
        # Never allow licensing below the tenant's tier minimum.
        p = get_pricing(db)
        min_tb = float(effective_plan(p, tenant.plan).get("min_tb", 0) or 0)
        prev_tb = round((tenant.licensed_bytes or 0) / TB, 2)
        tenant.licensed_bytes = int(max(body.licensed_tb, min_tb, 0.0) * TB)
        new_tb = round((tenant.licensed_bytes or 0) / TB, 2)
        if new_tb != prev_tb:
            summary.append(f"Set protected storage to {new_tb:g} TB")
    if body.appliance_plan is not None:
        prev_qty = sum(q for _, q in _ap_sig(tenant.appliance_plan))
        tenant.appliance_plan = [
            {"capacity_tb": s.get("capacity_tb"), "qty": int(s.get("qty") or 0)}
            for s in body.appliance_plan if int(s.get("qty") or 0) > 0
        ]
        new_qty = sum(q for _, q in _ap_sig(tenant.appliance_plan))
        if new_qty == 0 and prev_qty > 0:
            summary.append("Removed all secure appliances")
        elif new_qty != prev_qty:
            summary.append(f"Updated appliance plan to {new_qty} unit{'s' if new_qty != 1 else ''}")
    db.commit()
    audit.record(db, actor=principal.user_id, action="billing.plan_updated",
                 tenant_id=tenant.id, resource=tenant.id,
                 detail={"options": tenant.protection_options,
                         "licensed_bytes": tenant.licensed_bytes})
    view = plan_view(db, user, tenant)
    # Any plan/price change with no granular line (e.g. an appliance capacity swap
    # at the same count) still gets a confirmation — so notifications are reliable.
    after_sig = _plan_sig(tenant.protection_options, tenant.licensed_bytes, tenant.appliance_plan)
    if not summary and before_sig != after_sig:
        summary.append("Updated your protection plan")
    _notify_plan_change(db, user, view, summary)
    return view


# --------------------------------------------------------------------------- #
# Payment methods — processed through the tenant's assigned payment processor   #
# (Stripe / PayPal ServiceObject). Only PCI-safe fields are stored.             #
# --------------------------------------------------------------------------- #

def _pm_view(pm: PaymentMethod) -> dict:
    return {
        "id": pm.id, "processor": pm.processor, "type": pm.type,
        "brand": pm.brand, "last4": pm.last4,
        "exp_month": pm.exp_month, "exp_year": pm.exp_year,
        "holder_name": pm.holder_name, "is_default": bool(pm.is_default),
        "billing_address_id": pm.billing_address_id,
        "created_at": pm.created_at.isoformat() if pm.created_at else None,
    }


@router.get("/payment-config")
def payment_config(principal: security.Principal = Depends(security.get_principal),
                   tenant: Tenant = Depends(security.get_tenant),
                   db: Session = Depends(get_db)):
    """Which processor serves this tenant + the public key the UI needs. Never
    returns any secret."""
    from .. import services
    svc = services.tenant_payment_service(db, tenant.id)
    if not svc:
        return {"configured": False, "processor": None}
    kind = svc.get("kind", "")
    cfg = svc.get("config", {}) or {}
    processor = kind.replace("payment-", "") or None
    out = {"configured": True, "processor": processor, "name": svc.get("name"),
           "currency": (cfg.get("currency") or "USD").upper()}
    if processor == "stripe":
        out["publishable_key"] = cfg.get("publishable_key") or ""
    elif processor == "paypal":
        out["client_id"] = cfg.get("client_id") or ""
        out["environment"] = cfg.get("environment") or "live"
    return out


@router.get("/payment-methods")
def list_payment_methods(principal: security.Principal = Depends(security.get_principal),
                         tenant: Tenant = Depends(security.get_tenant),
                         db: Session = Depends(get_db)):
    rows = (db.query(PaymentMethod)
            .filter(PaymentMethod.tenant_id == tenant.id)
            .order_by(PaymentMethod.is_default.desc(), PaymentMethod.created_at.desc()).all())
    return {"payment_methods": [_pm_view(p) for p in rows]}


class AddCard(BaseModel):
    number: str
    exp_month: int
    exp_year: int
    cvc: str
    holder_name: str | None = None
    billing_address_id: str | None = None
    make_default: bool = True


@router.post("/payment-methods")
def add_payment_method(body: AddCard,
                       principal: security.Principal = Depends(security.require_passkey),
                       tenant: Tenant = Depends(security.get_tenant),
                       db: Session = Depends(get_db)):
    """Vault a card with the tenant's assigned processor and store only the
    PCI-safe fields + processor references (never the PAN / CVC)."""
    from .. import payments, services
    svc = services.tenant_payment_service(db, tenant.id)
    try:
        res = payments.add_card(svc, body.model_dump())
    except payments.PaymentError as exc:
        raise HTTPException(400, str(exc))
    make_default = body.make_default or (
        db.query(PaymentMethod).filter(PaymentMethod.tenant_id == tenant.id).count() == 0)
    if make_default:
        (db.query(PaymentMethod)
         .filter(PaymentMethod.tenant_id == tenant.id)
         .update({PaymentMethod.is_default: False}))
    pm = PaymentMethod(tenant_id=tenant.id, user_id=principal.user_id,
                       billing_address_id=body.billing_address_id or None,
                       is_default=make_default, **res)
    db.add(pm)
    db.commit()
    db.refresh(pm)
    _ensure_billing_profile(db, tenant, db.get(User, principal.user_id), pm)
    audit.record(db, actor=principal.user_id, action="billing.payment_method_added",
                 tenant_id=tenant.id, resource=pm.id,
                 detail={"processor": pm.processor, "brand": pm.brand, "last4": pm.last4})
    return _pm_view(pm)


@router.put("/payment-methods/{pid}/default")
def set_default_payment_method(pid: str,
                               principal: security.Principal = Depends(security.get_principal),
                               tenant: Tenant = Depends(security.get_tenant),
                               db: Session = Depends(get_db)):
    pm = db.get(PaymentMethod, pid)
    if not pm or pm.tenant_id != tenant.id:
        raise HTTPException(404, "payment method not found")
    (db.query(PaymentMethod).filter(PaymentMethod.tenant_id == tenant.id)
     .update({PaymentMethod.is_default: False}))
    pm.is_default = True
    db.commit()
    return _pm_view(pm)


@router.delete("/payment-methods/{pid}")
def delete_payment_method(pid: str,
                          principal: security.Principal = Depends(security.get_principal),
                          tenant: Tenant = Depends(security.get_tenant),
                          db: Session = Depends(get_db)):
    pm = db.get(PaymentMethod, pid)
    if not pm or pm.tenant_id != tenant.id:
        raise HTTPException(404, "payment method not found")
    was_default = pm.is_default
    db.delete(pm)
    db.commit()
    if was_default:
        nxt = (db.query(PaymentMethod).filter(PaymentMethod.tenant_id == tenant.id)
               .order_by(PaymentMethod.created_at.desc()).first())
        if nxt:
            nxt.is_default = True
            db.commit()
    audit.record(db, actor=principal.user_id, action="billing.payment_method_removed",
                 tenant_id=tenant.id, resource=pid)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Recurring billing profiles — the tenant's subscription at the processor.      #
# The customer's card creates the profile (inactive); an admin turns charging   #
# on/off. The amount is the plan's computed monthly total.                      #
# --------------------------------------------------------------------------- #

def _plan_amount_cents(db: Session, user: User, tenant: Tenant) -> tuple[int, str, str, str]:
    """(amount_cents, currency, plan_id, plan_name) from the tenant's computed
    monthly plan cost."""
    view = plan_view(db, user, tenant)
    total = float((view.get("costs") or {}).get("total_monthly") or 0.0)
    plan = view.get("license_plan") or {}
    cur = (view.get("currency") or "USD")
    return int(round(total * 100)), cur, plan.get("id", tenant.plan or ""), plan.get("name", "")


def _ensure_billing_profile(db: Session, tenant: Tenant, user: User,
                            pm: PaymentMethod) -> BillingProfile:
    """Create-or-update the tenant's billing profile from a saved card. Never
    flips charging on — that's an admin action. Keeps the processor customer +
    default method + current plan amount in sync."""
    amount, cur, plan_id, plan_name = _plan_amount_cents(db, user, tenant)
    prof = db.query(BillingProfile).filter(BillingProfile.tenant_id == tenant.id).first()
    if prof is None:
        prof = BillingProfile(tenant_id=tenant.id, status="inactive", active=False)
        db.add(prof)
    prof.processor = pm.processor or prof.processor
    if pm.processor_customer:
        prof.processor_customer = pm.processor_customer
    if pm.is_default or not prof.payment_method_id:
        prof.payment_method_id = pm.id
    prof.amount_cents = amount
    prof.currency = cur
    prof.plan_id = plan_id
    prof.plan_name = plan_name
    db.commit()
    db.refresh(prof)
    return prof


def _profile_view(db: Session, prof: BillingProfile) -> dict:
    pm = db.get(PaymentMethod, prof.payment_method_id) if prof.payment_method_id else None
    return {
        "id": prof.id, "tenant_id": prof.tenant_id, "processor": prof.processor,
        "plan_id": prof.plan_id, "plan_name": prof.plan_name,
        "amount_cents": prof.amount_cents, "currency": prof.currency,
        "interval": prof.interval, "status": prof.status, "active": bool(prof.active),
        "processor_customer": prof.processor_customer,
        "processor_subscription": prof.processor_subscription,
        "current_period_end": prof.current_period_end.isoformat() if prof.current_period_end else None,
        "last_charge_at": prof.last_charge_at.isoformat() if prof.last_charge_at else None,
        "last_status": prof.last_status,
        "payment_method": _pm_view(pm) if pm else None,
        "updated_at": prof.updated_at.isoformat() if prof.updated_at else None,
    }


@router.get("/subscription")
def get_subscription(principal: security.Principal = Depends(security.get_principal),
                     tenant: Tenant = Depends(security.get_tenant),
                     db: Session = Depends(get_db)):
    """The tenant's recurring billing status for the customer's Billing tab."""
    prof = db.query(BillingProfile).filter(BillingProfile.tenant_id == tenant.id).first()
    if not prof:
        amount, cur, plan_id, plan_name = _plan_amount_cents(
            db, db.get(User, principal.user_id), tenant)
        return {"profile": None, "quote": {"amount_cents": amount, "currency": cur,
                                           "plan_id": plan_id, "plan_name": plan_name}}
    return {"profile": _profile_view(db, prof)}


# --------------------------------------------------------------------------- #
# Admin billing — view every tenant's billing profile, toggle recurring        #
# charges, run a charge now, and inspect the charge history.                    #
# --------------------------------------------------------------------------- #

admin_router = APIRouter(prefix="/admin/billing", tags=["admin-billing"])


def _charge_view(c: BillingCharge) -> dict:
    return {"id": c.id, "amount_cents": c.amount_cents, "currency": c.currency,
            "status": c.status, "attempt": c.attempt, "kind": c.kind,
            "processor_charge_id": c.processor_charge_id, "error": c.error,
            "created_at": c.created_at.isoformat() if c.created_at else None}


@admin_router.get("/profiles", dependencies=[Depends(security.require_platform_admin)])
def admin_list_profiles(db: Session = Depends(get_db)):
    """Every tenant's billing profile with its status + recent-charge rollup."""
    profs = db.query(BillingProfile).order_by(BillingProfile.updated_at.desc()).all()
    tenants = {t.id: t for t in db.query(Tenant).all()}
    out = []
    for p in profs:
        charges = (db.query(BillingCharge)
                   .filter(BillingCharge.profile_id == p.id).all())
        succeeded = sum(1 for c in charges if c.status == "succeeded")
        failed = sum(1 for c in charges if c.status == "failed")
        v = _profile_view(db, p)
        t = tenants.get(p.tenant_id)
        v["tenant_name"] = t.name if t else "(unknown)"
        v["charges_total"] = len(charges)
        v["charges_succeeded"] = succeeded
        v["charges_failed"] = failed
        out.append(v)
    return {"profiles": out}


@admin_router.get("/profiles/{pid}", dependencies=[Depends(security.require_platform_admin)])
def admin_profile_detail(pid: str, db: Session = Depends(get_db)):
    prof = db.get(BillingProfile, pid)
    if not prof:
        raise HTTPException(404, "billing profile not found")
    t = db.get(Tenant, prof.tenant_id)
    charges = (db.query(BillingCharge).filter(BillingCharge.profile_id == pid)
               .order_by(BillingCharge.created_at.desc()).limit(100).all())
    v = _profile_view(db, prof)
    v["tenant_name"] = t.name if t else "(unknown)"
    v["charges"] = [_charge_view(c) for c in charges]
    return v


def _sync_amount(db: Session, prof: BillingProfile) -> None:
    """Refresh the profile's amount from the tenant's current plan (owner user)."""
    t = db.get(Tenant, prof.tenant_id)
    owner = (db.query(User).filter(User.tenant_id == prof.tenant_id)
             .order_by(User.created_at.asc()).first())
    if t and owner:
        amount, cur, plan_id, plan_name = _plan_amount_cents(db, owner, t)
        prof.amount_cents, prof.currency = amount, cur
        prof.plan_id, prof.plan_name = plan_id, plan_name


@admin_router.post("/profiles/{pid}/enable", dependencies=[Depends(security.require_platform_admin)])
def admin_enable_profile(pid: str,
                         principal: security.Principal = Depends(security.require_platform_admin),
                         db: Session = Depends(get_db)):
    """Turn recurring charges on: create (or resume) the processor subscription
    for the plan amount using the tenant's default card."""
    from .. import payments, services
    prof = db.get(BillingProfile, pid)
    if not prof:
        raise HTTPException(404, "billing profile not found")
    pm = db.get(PaymentMethod, prof.payment_method_id) if prof.payment_method_id else None
    if not pm:
        raise HTTPException(400, "no payment method on file for this tenant")
    svc = services.tenant_payment_service(db, prof.tenant_id)
    _sync_amount(db, prof)
    if prof.amount_cents <= 0:
        raise HTTPException(400, "plan amount is zero — nothing to bill")
    try:
        if prof.processor_subscription:
            res = payments.set_subscription_paused(svc, prof.processor_subscription, False)
            prof.status = res.get("status", "active")
        else:
            res = payments.start_subscription(
                svc, customer=prof.processor_customer, token=pm.processor_token,
                amount_cents=prof.amount_cents, currency=prof.currency,
                interval=prof.interval, plan_name=prof.plan_name or "Arkive protection")
            prof.processor_subscription = res.get("subscription_id", "")
            prof.status = res.get("status", "active")
            cpe = res.get("current_period_end")
            if cpe:
                prof.current_period_end = datetime.fromisoformat(cpe).replace(tzinfo=None)
    except payments.PaymentError as exc:
        raise HTTPException(400, str(exc))
    prof.active = True
    if not prof.processor:
        prof.processor = payments._processor_name(svc)
    db.commit()
    audit.record(db, actor=principal.user_id, action="admin.billing_enabled",
                 tenant_id=prof.tenant_id, resource=prof.id,
                 detail={"amount_cents": prof.amount_cents, "sub": prof.processor_subscription})
    return _profile_view(db, prof)


@admin_router.post("/profiles/{pid}/disable", dependencies=[Depends(security.require_platform_admin)])
def admin_disable_profile(pid: str,
                          principal: security.Principal = Depends(security.require_platform_admin),
                          db: Session = Depends(get_db)):
    """Pause recurring charges (keeps the subscription so it can be resumed)."""
    from .. import payments, services
    prof = db.get(BillingProfile, pid)
    if not prof:
        raise HTTPException(404, "billing profile not found")
    svc = services.tenant_payment_service(db, prof.tenant_id)
    if prof.processor_subscription:
        try:
            payments.set_subscription_paused(svc, prof.processor_subscription, True)
        except payments.PaymentError as exc:
            raise HTTPException(400, str(exc))
    prof.active = False
    prof.status = "paused"
    db.commit()
    audit.record(db, actor=principal.user_id, action="admin.billing_disabled",
                 tenant_id=prof.tenant_id, resource=prof.id)
    return _profile_view(db, prof)


@admin_router.post("/profiles/{pid}/charge", dependencies=[Depends(security.require_platform_admin)])
def admin_charge_now(pid: str,
                     principal: security.Principal = Depends(security.require_platform_admin),
                     db: Session = Depends(get_db)):
    """Capture a one-off charge for the plan amount against the tenant's card."""
    from .. import payments, services
    prof = db.get(BillingProfile, pid)
    if not prof:
        raise HTTPException(404, "billing profile not found")
    pm = db.get(PaymentMethod, prof.payment_method_id) if prof.payment_method_id else None
    if not pm:
        raise HTTPException(400, "no payment method on file for this tenant")
    svc = services.tenant_payment_service(db, prof.tenant_id)
    _sync_amount(db, prof)
    attempt = (db.query(func.count(BillingCharge.id))
               .filter(BillingCharge.profile_id == prof.id).scalar() or 0) + 1
    res = payments.charge_once(
        svc, customer=prof.processor_customer, token=pm.processor_token,
        amount_cents=prof.amount_cents, currency=prof.currency,
        description=f"{prof.plan_name or 'Arkive'} — manual charge")
    charge = BillingCharge(
        tenant_id=prof.tenant_id, profile_id=prof.id, amount_cents=prof.amount_cents,
        currency=prof.currency, status=res["status"], attempt=attempt, kind="manual",
        processor_charge_id=res.get("charge_id", ""), error=res.get("error", ""))
    db.add(charge)
    prof.last_charge_at = datetime.utcnow()
    prof.last_status = res["status"]
    db.commit()
    db.refresh(charge)
    audit.record(db, actor=principal.user_id, action="admin.billing_charged",
                 tenant_id=prof.tenant_id, resource=prof.id,
                 detail={"amount_cents": prof.amount_cents, "status": res["status"]})
    return _charge_view(charge)
