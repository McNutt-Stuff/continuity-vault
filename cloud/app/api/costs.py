"""Cloud cost / financial admin API (platform-admin only).

Exposes the month-to-date cloud spend the cost worker samples (by category and
over time), the IAM/role guidance the billing credentials need, and a
Revenue & Costs summary that pairs collected revenue with cloud cost.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import cloud_costs, security
from ..db import get_db

router = APIRouter(prefix="/admin/costs", tags=["costs"],
                   dependencies=[Depends(security.require_platform_admin)])


@router.get("/iam/{provider}")
def iam_guidance(provider: str):
    g = cloud_costs.IAM_GUIDANCE.get((provider or "").lower())
    if not g:
        raise HTTPException(404, "unknown provider")
    return g


@router.get("/summary")
def summary(db: Session = Depends(get_db)):
    """Revenue (collected) vs. cloud cost (month-to-date), plus cost by category."""
    from .billing import admin_billing_summary
    cost = cloud_costs.summary(db)
    rev = admin_billing_summary(db)  # cents
    month_rev = (rev.get("month_revenue_cents", 0) or 0) / 100.0
    mrr = (rev.get("mrr_cents", 0) or 0) / 100.0
    cost_total = cost.get("total", 0.0)
    return {
        "currency": cost.get("currency", "USD"),
        "period": cost.get("period"),
        "updated_at": cost.get("updated_at"),
        "configured": cost.get("configured", False),
        "cost_mtd": cost_total,
        "cost_by_category": cost.get("by_category", {}),
        "revenue_mtd": round(month_rev, 2),
        "revenue_all_time": round((rev.get("all_time_cents", 0) or 0) / 100.0, 2),
        "mrr": round(mrr, 2),
        "arr": round(mrr * 12, 2),
        "gross_profit_mtd": round(month_rev - cost_total, 2),
        "gross_margin_pct": round((month_rev - cost_total) / month_rev * 100, 1) if month_rev else None,
        "active_subscriptions": rev.get("active_subscriptions", 0),
    }


@router.get("/trends")
def trends(days: int = 30, db: Session = Depends(get_db)):
    return cloud_costs.trends(db, days=days)


@router.post("/sample-now")
def sample_now(principal: security.Principal = Depends(security.require_platform_admin),
               db: Session = Depends(get_db)):
    """Force an immediate cost sample (otherwise hourly). Best-effort."""
    n = cloud_costs.sample_all(db)
    return {"ok": True, "sampled": n, "summary": cloud_costs.summary(db)}
