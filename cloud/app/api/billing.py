"""Protection Setup / billing.

The customer's Protection Setup page reads its pricing from ``GET /billing/pricing``
(admin-managed platform pricing) and its current plan + computed monthly cost +
data-value estimate from ``GET /billing/plan``. Saving the page (``PUT``) records
which storage tiers are enabled (feature gating), how much data protection they
license, and their desired appliances.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import audit, security
from ..db import get_db
from ..models import PricingConfig, SearchDocument, Tenant
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


def get_pricing(db: Session) -> PricingConfig:
    """Fetch the single platform pricing row, creating sensible defaults once."""
    p = db.get(PricingConfig, "default")
    if p is None:
        p = PricingConfig(id="default", appliance_tiers=_DEFAULT_TIERS,
                          data_value_per_type=_DEFAULT_VALUE)
        db.add(p)
        db.commit()
        db.refresh(p)
    # Backfill JSON defaults if an older row left them empty, and migrate any
    # legacy one-time-price tiers to the lease model (monthly + setup).
    if not p.appliance_tiers or any("monthly" not in t for t in p.appliance_tiers):
        p.appliance_tiers = _DEFAULT_TIERS
    if not p.data_value_per_type:
        p.data_value_per_type = _DEFAULT_VALUE
    return p


def pricing_public(p: PricingConfig) -> dict:
    return {
        "currency": p.currency,
        "protection_price_per_tb_month": p.protection_price_per_tb_month,
        "cloud_price_per_tb_month": p.cloud_price_per_tb_month,
        "s3_price_per_tb_month": p.s3_price_per_tb_month,
        "azure_price_per_tb_month": p.azure_price_per_tb_month,
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
    options = list(tenant.protection_options or [])
    total, used_bytes, by_bucket = _usage(db, tenant.id)
    used_tb = used_bytes / TB
    licensed_bytes = int(tenant.licensed_bytes or 0)
    licensed_tb = licensed_bytes / TB

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
    for sel in (tenant.appliance_plan or []):
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

    protection_monthly = round(licensed_tb * p.protection_price_per_tb_month, 2)
    cloud_storage_monthly = round(used_tb * p.cloud_price_per_tb_month, 2) if "cv-cloud" in options else 0.0
    third_party_monthly = round(used_tb * p.s3_price_per_tb_month, 2) if "customer-cloud" in options else 0.0
    appliance_monthly = round(appliance_monthly, 2)
    total_monthly = round(protection_monthly + cloud_storage_monthly + appliance_monthly, 2)
    annual_cost = round(total_monthly * 12, 2)
    value_ratio = round(data_value_total / annual_cost, 1) if annual_cost > 0 else None

    return {
        "options": options,
        "currency": p.currency,
        "licensed_bytes": licensed_bytes,
        "licensed_tb": round(licensed_tb, 3),
        "used_bytes": used_bytes,
        "used_tb": round(used_tb, 4),
        "percent": round(min(100.0, used_bytes / licensed_bytes * 100), 1) if licensed_bytes else None,
        "objects_total": total,
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


@router.get("/plan")
def get_plan(tenant: Tenant = Depends(security.get_tenant),
             db: Session = Depends(get_db)):
    return _compute_plan(db, tenant)


class PlanUpdate(BaseModel):
    options: list[str] | None = None
    licensed_tb: float | None = None
    appliance_plan: list[dict] | None = None


@router.put("/plan")
def update_plan(body: PlanUpdate,
                principal: security.Principal = Depends(security.require_security_admin),
                tenant: Tenant = Depends(security.get_tenant),
                db: Session = Depends(get_db)):
    valid = {t["id"] for t in STORAGE_TIERS}
    if body.options is not None:
        tenant.protection_options = [o for o in body.options if o in valid]
    if body.licensed_tb is not None:
        tenant.licensed_bytes = int(max(0.0, body.licensed_tb) * TB)
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
    return _compute_plan(db, tenant)
