"""Protection Setup / billing.

The customer's Protection Setup page reads its pricing from ``GET /billing/pricing``
(admin-managed platform pricing) and its current plan + computed monthly cost +
data-value estimate from ``GET /billing/plan``. Saving the page (``PUT``) records
which storage tiers are enabled (feature gating), how much data protection they
license, and their desired appliances.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import audit, security
from ..db import get_db
from ..models import PricingConfig, SearchDocument, Tenant, User, Vault
from .dashboard import _OBJECT_BUCKETS, _bucket_for

router = APIRouter(prefix="/billing", tags=["billing"])

TB = 1024 ** 4  # 1 TB (binary) — matches the byte sizes shown in the UI

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


def _usage(db: Session, tenant_id: str) -> tuple[int, int, dict]:
    """Deduped object count, protected bytes, and per-bucket counts."""
    docs = (db.query(SearchDocument)
            .filter(SearchDocument.tenant_id == tenant_id)
            .order_by(SearchDocument.created_at.desc()).all())
    seen: set = set()
    used_bytes = 0
    total = 0
    by_bucket: dict = {}
    for d in docs:
        key = (d.source_type, d.object_id)
        if key in seen:
            continue
        seen.add(key)
        total += 1
        used_bytes += int(d.size_bytes or 0)
        bk = _bucket_for(d.doc_type)["key"]
        by_bucket[bk] = by_bucket.get(bk, 0) + 1
    return total, used_bytes, by_bucket


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
    docs = (db.query(SearchDocument)
            .filter(SearchDocument.vault_id.in_(vault_ids))
            .order_by(SearchDocument.created_at.desc()).all())
    seen: set = set()
    used_bytes = 0
    total = 0
    by_bucket: dict = {}
    for d in docs:
        key = (d.source_type, d.object_id)
        if key in seen:
            continue
        seen.add(key)
        total += 1
        used_bytes += int(d.size_bytes or 0)
        bk = _bucket_for(d.doc_type)["key"]
        by_bucket[bk] = by_bucket.get(bk, 0) + 1
    return total, used_bytes, by_bucket


def user_protection_options(user, tenant: Tenant) -> list[str]:
    """Protection destinations for this account. Shared-tenant personal accounts
    each own their selection (the tenant is a pool of unrelated accounts); org
    tenants share the tenant-wide selection."""
    if user is not None and (tenant.tenant_type or "dedicated") == "shared":
        return list(user.protection_options or [])
    return list(tenant.protection_options or [])


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
        if body.options is not None:
            user.protection_options = [o for o in body.options if o in valid]
        db.commit()
        audit.record(db, actor=principal.user_id, action="billing.plan_updated",
                     tenant_id=tenant.id, resource=user.id,
                     detail={"options": user.protection_options})
        return plan_view(db, user, tenant)
    if not (security.is_org_admin(principal.role) or principal.is_platform_admin):
        raise HTTPException(403, "security-admin role required")
    if body.options is not None:
        tenant.protection_options = [o for o in body.options if o in valid]
    if body.licensed_tb is not None:
        # Never allow licensing below the tenant's tier minimum.
        p = get_pricing(db)
        min_tb = float(effective_plan(p, tenant.plan).get("min_tb", 0) or 0)
        tenant.licensed_bytes = int(max(body.licensed_tb, min_tb, 0.0) * TB)
    if body.appliance_plan is not None:
        tenant.appliance_plan = [
            {"capacity_tb": s.get("capacity_tb"), "qty": int(s.get("qty") or 0)}
            for s in body.appliance_plan if int(s.get("qty") or 0) > 0
        ]
    db.commit()
    audit.record(db, actor=principal.user_id, action="billing.plan_updated",
                 tenant_id=tenant.id, resource=tenant.id,
                 detail={"options": tenant.protection_options,
                         "licensed_bytes": tenant.licensed_bytes})
    return plan_view(db, user, tenant)
