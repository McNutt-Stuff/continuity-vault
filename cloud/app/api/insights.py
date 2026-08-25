"""Digital-footprint Insights API.

Serves the precomputed insights payload for the signed-in user (refreshed daily
by ``workers.insights``). The page reads this one endpoint, so it stays fast. If
a report hasn't been generated yet (new user / first visit), it is computed
on-demand for just that user.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import features, security
from ..db import get_db
from ..models import Tenant, User, UserInsights

router = APIRouter(prefix="/insights", tags=["insights"])


def _enabled(db: Session, principal: security.Principal) -> bool:
    user = db.get(User, principal.user_id)
    tenant = db.get(Tenant, principal.tenant_id)
    return features.resolve(user, tenant, "insights_enabled")


def _view(row: UserInsights) -> dict:
    return {
        "status": row.status,
        "generated_at": row.generated_at.isoformat() if row.generated_at else None,
        "stats": row.stats or {},
        "timeline": row.timeline or {},
        "cards": row.cards or [],
    }


@router.get("")
def get_insights(principal: security.Principal = Depends(security.get_principal),
                 db: Session = Depends(get_db)):
    if not _enabled(db, principal):
        raise HTTPException(404, "insights are not enabled for this account")
    row = db.query(UserInsights).filter(UserInsights.user_id == principal.user_id).one_or_none()
    if row is None:
        user = db.get(User, principal.user_id)
        tenant = db.get(Tenant, principal.tenant_id)
        if tenant is not None and tenant.node_id:
            # Node-hosted: the control plane can't mine the index. Flag it so the
            # assigned node builds and pushes the report back, and report pending.
            from ..workers.insights import mark_pending
            row = mark_pending(db, user)
        else:
            # First visit before the daily job ran — compute this one user's report now.
            from ..workers.insights import generate_for_user
            row = generate_for_user(db, user)
    return _view(row)


@router.post("/refresh")
def refresh_insights(principal: security.Principal = Depends(security.get_principal),
                     db: Session = Depends(get_db)):
    """Recompute the signed-in user's insights on demand."""
    if not _enabled(db, principal):
        raise HTTPException(404, "insights are not enabled for this account")
    user = db.get(User, principal.user_id)
    tenant = db.get(Tenant, principal.tenant_id)
    if tenant is not None and tenant.node_id:
        from ..workers.insights import mark_pending
        return _view(mark_pending(db, user))
    from ..workers.insights import generate_for_user
    return _view(generate_for_user(db, user))
