"""Compliance engine API — framework posture for Business/Enterprise organizations.

Feature-flagged (``compliance_enabled``), Business/Enterprise only, org-admin
gated. Reads/writes flow through ``compliance.engine``; every mutation is audited
and recorded in the compliance change ledger (posture history).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import audit, features, security
from ..compliance import engine, models as m, registry
from ..db import get_db
from ..models import Tenant, User

router = APIRouter(prefix="/compliance", tags=["compliance"])

_PLANS = {"business", "enterprise"}


def require_compliance(principal: security.Principal = Depends(security.require_org_admin),
                       db: Session = Depends(get_db)) -> security.Principal:
    tenant = db.get(Tenant, principal.tenant_id)
    user = db.get(User, principal.user_id)
    plan = (tenant.plan if tenant else "") or ""
    if plan.lower() not in _PLANS:
        raise HTTPException(403, "Compliance requires the Business or Enterprise plan")
    if not features.resolve(user, tenant, "compliance_enabled"):
        raise HTTPException(403, "The Compliance engine isn't enabled for your organization")
    return principal


def _tenant(db: Session, principal: security.Principal) -> Tenant:
    t = db.get(Tenant, principal.tenant_id)
    if t is None:
        raise HTTPException(404, "tenant not found")
    return t


@router.get("/catalog")
def catalog(principal: security.Principal = Depends(require_compliance)):
    """The framework + control + capability registry (static reference)."""
    return {
        "capabilities": registry.CAPABILITIES,
        "frameworks": [{"framework": fw, "label": s["label"], "version": s["version"],
                        "authority": s.get("authority"), "url": s.get("url"),
                        "description": s.get("description"),
                        "controls": s["controls"]}
                       for fw, s in registry.FRAMEWORKS.items()],
    }


@router.get("/frameworks")
def frameworks(principal: security.Principal = Depends(require_compliance),
               db: Session = Depends(get_db)):
    """Every framework with enabled state + latest score."""
    return {"frameworks": engine.available(db, _tenant(db, principal))}


class FrameworkToggle(BaseModel):
    enabled: bool


@router.post("/frameworks/{framework}")
def set_framework(framework: str, body: FrameworkToggle,
                  principal: security.Principal = Depends(require_compliance),
                  db: Session = Depends(get_db)):
    """Enable/disable a framework (seeds controls + assesses on enable)."""
    tenant = _tenant(db, principal)
    try:
        engine.enable_pack(db, tenant, framework, body.enabled, actor=principal.user_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit.record(db, actor=principal.user_id, action="compliance.framework",
                 category="admin", resource=framework,
                 detail={"framework": framework, "enabled": body.enabled})
    return {"frameworks": engine.available(db, tenant), "report": engine.report(db, tenant)}


@router.post("/evaluate")
def evaluate(principal: security.Principal = Depends(require_compliance),
             db: Session = Depends(get_db)):
    """Re-assess every enabled framework against current live evidence."""
    tenant = _tenant(db, principal)
    res = engine.evaluate(db, tenant, actor=principal.user_id)
    return {**res, "report": engine.report(db, tenant)}


@router.get("/report")
def report(principal: security.Principal = Depends(require_compliance),
           db: Session = Depends(get_db)):
    return engine.report(db, _tenant(db, principal))


@router.get("/controls")
def controls(framework: str,
             principal: security.Principal = Depends(require_compliance),
             db: Session = Depends(get_db)):
    return {"controls": engine.controls_view(db, _tenant(db, principal), framework)}


@router.get("/history")
def history(framework: str = "",
            principal: security.Principal = Depends(require_compliance),
            db: Session = Depends(get_db)):
    return engine.history(db, _tenant(db, principal), framework)


class ControlUpdate(BaseModel):
    state: str | None = None
    owner: str | None = None


@router.patch("/controls/{control_id}")
def update_control(control_id: str, body: ControlUpdate,
                   principal: security.Principal = Depends(require_compliance),
                   db: Session = Depends(get_db)):
    """Override a control's state / owner (a manual assessment the engine won't clobber)."""
    tenant = _tenant(db, principal)
    c = db.get(m.ComplianceControl, control_id)
    if c is None or c.tenant_id != tenant.id:
        raise HTTPException(404, "control not found")
    prior = c.state
    if body.state:
        c.state = body.state
        c.auto = False
    if body.owner is not None:
        c.owner = body.owner
    c.last_evaluated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add(m.ComplianceEvent(tenant_id=tenant.id, framework=c.framework, control_id=c.control_id,
                             kind="control_state", actor=principal.user_id,
                             summary=f"{c.control_id}: {prior} → {c.state} (manual)",
                             detail={"from": prior, "to": c.state, "manual": True}))
    db.commit()
    audit.record(db, actor=principal.user_id, action="compliance.control",
                 category="admin", resource=control_id,
                 detail={"control": c.control_id, "state": c.state})
    return {"ok": True, "state": c.state, "owner": c.owner}


class ExceptionBody(BaseModel):
    reason: str
    expires_at: str | None = None


@router.post("/controls/{control_id}/exception")
def add_exception(control_id: str, body: ExceptionBody,
                  principal: security.Principal = Depends(require_compliance),
                  db: Session = Depends(get_db)):
    """Record a time-boxed, audited exception for a control (state → exception)."""
    tenant = _tenant(db, principal)
    c = db.get(m.ComplianceControl, control_id)
    if c is None or c.tenant_id != tenant.id:
        raise HTTPException(404, "control not found")
    if not (body.reason or "").strip():
        raise HTTPException(400, "a reason is required")
    exp = None
    if body.expires_at:
        try:
            exp = datetime.fromisoformat(body.expires_at.replace("Z", "+00:00"))
            exp = exp.replace(tzinfo=None) if exp.tzinfo else exp
        except ValueError:
            raise HTTPException(400, "invalid expires_at")
    for e in db.query(m.ComplianceException).filter(
            m.ComplianceException.control_id == control_id).all():
        db.delete(e)
    db.add(m.ComplianceException(tenant_id=tenant.id, control_id=control_id,
                                 reason=body.reason.strip(),
                                 approved_by=principal.user_id, expires_at=exp))
    c.state = "exception"
    c.auto = False
    db.add(m.ComplianceEvent(tenant_id=tenant.id, framework=c.framework, control_id=c.control_id,
                             kind="exception", actor=principal.user_id,
                             summary=f"Exception recorded for {c.control_id}",
                             detail={"reason": body.reason.strip()[:200]}))
    db.commit()
    audit.record(db, actor=principal.user_id, action="compliance.exception",
                 category="admin", severity="warning", resource=control_id,
                 detail={"control": c.control_id, "reason": body.reason.strip()[:200]})
    return {"ok": True}
