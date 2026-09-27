"""Arkive Signal Platform — query + findings API.

Read the normalized signal store, provider health, coverage, and findings for the
caller's tenant. Gated by the signal_platform_enabled feature flag and org-admin
(the raw explorer is an admin/assurance surface, spec §22/§31). For node-hosted
tenants this router is proxied CP→node (see api/node_proxy._should_proxy), so the
node serves its own locally-generated signals.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from .. import audit, features, security
from ..db import get_db
from ..models import Finding, Signal, SignalProviderHealth, Tenant, User

router = APIRouter(prefix="/signals", tags=["signals"])


def _guard(principal: security.Principal, tenant: Tenant, db: Session) -> User | None:
    user = db.get(User, principal.user_id)
    if not features.resolve(user, tenant, "signal_platform_enabled", db):
        raise HTTPException(403, "The Signal Platform is not enabled for this account.")
    if not (security.is_org_admin(principal.role) or principal.is_platform_admin):
        raise HTTPException(403, "Signals require an organization administrator.")
    return user


def _signal_view(s: Signal) -> dict:
    return {
        "id": s.id, "signal_type": s.signal_type, "category": s.category,
        "kind": s.observation_kind, "provider": s.provider,
        "subject_type": s.subject_type, "subject_id": s.subject_id,
        "resource_type": s.resource_type, "resource_id": s.resource_id,
        "actor_id": s.actor_id, "value": s.value,
        "normalized_value": s.normalized_value, "severity": s.severity,
        "confidence": s.confidence, "confidence_reason": s.confidence_reason,
        "freshness_state": s.freshness_state,
        "observed_at": s.observed_at.isoformat() if s.observed_at else None,
        "first_seen": s.first_seen.isoformat() if s.first_seen else None,
        "last_seen": s.last_seen.isoformat() if s.last_seen else None,
        "occurrence_count": int(s.occurrence_count or 0),
        "evidence_id": s.evidence_id,
        "source_integration_id": s.source_integration_id,
    }


def _finding_view(f: Finding) -> dict:
    return {
        "id": f.id, "finding_type": f.finding_type, "category": f.category,
        "severity": f.severity, "status": f.status, "title": f.title,
        "description": f.description, "subject_type": f.subject_type,
        "subject_id": f.subject_id, "resource_type": f.resource_type,
        "resource_id": f.resource_id, "signal_ids": f.signal_ids or [],
        "occurrence_count": int(f.occurrence_count or 0),
        "first_seen": f.first_seen.isoformat() if f.first_seen else None,
        "last_seen": f.last_seen.isoformat() if f.last_seen else None,
        "remediation": f.remediation or {}, "assigned_to": f.assigned_to,
        "exception_reason": f.exception_reason,
        "resolved_at": f.resolved_at.isoformat() if f.resolved_at else None,
        "meta": f.meta or {},
    }


@router.get("")
def list_signals(provider: str | None = None, category: str | None = None,
                 signal_type: str | None = None, subject_type: str | None = None,
                 subject_id: str | None = None, freshness: str | None = None,
                 severity: str | None = None, q: str | None = None,
                 status: str = "active", limit: int = 200, offset: int = 0,
                 principal: security.Principal = Depends(security.get_principal),
                 tenant: Tenant = Depends(security.get_tenant),
                 db: Session = Depends(get_db)):
    _guard(principal, tenant, db)
    query = db.query(Signal).filter(Signal.tenant_id == tenant.id)
    if status and status != "all":
        query = query.filter(Signal.status == status)
    if provider:
        query = query.filter(Signal.provider == provider)
    if category:
        query = query.filter(Signal.category == category)
    if signal_type:
        query = query.filter(Signal.signal_type == signal_type)
    if subject_type:
        query = query.filter(Signal.subject_type == subject_type)
    if subject_id:
        query = query.filter(Signal.subject_id == subject_id)
    if freshness:
        query = query.filter(Signal.freshness_state == freshness)
    if severity:
        query = query.filter(Signal.severity == severity)
    if q:
        like = f"%{q.lower()}%"
        query = query.filter(or_(func.lower(Signal.signal_type).like(like),
                                 func.lower(Signal.normalized_value).like(like),
                                 func.lower(Signal.subject_id).like(like)))
    total = query.count()
    rows = (query.order_by(Signal.last_seen.desc())
            .offset(max(0, offset)).limit(min(1000, max(1, limit))).all())
    return {"total": total, "signals": [_signal_view(s) for s in rows]}


@router.get("/facets")
def facets(principal: security.Principal = Depends(security.get_principal),
           tenant: Tenant = Depends(security.get_tenant),
           db: Session = Depends(get_db)):
    _guard(principal, tenant, db)
    def _counts(col):
        return {k: int(n) for k, n in
                db.query(col, func.count(Signal.id))
                .filter(Signal.tenant_id == tenant.id, Signal.status == "active")
                .group_by(col).all()}
    return {"providers": _counts(Signal.provider),
            "categories": _counts(Signal.category),
            "severities": _counts(Signal.severity),
            "freshness": _counts(Signal.freshness_state)}


@router.get("/overview")
def overview(principal: security.Principal = Depends(security.get_principal),
             tenant: Tenant = Depends(security.get_tenant),
             db: Session = Depends(get_db)):
    _guard(principal, tenant, db)
    tid = tenant.id
    active = db.query(func.count(Signal.id)).filter(
        Signal.tenant_id == tid, Signal.status == "active").scalar() or 0
    stale = db.query(func.count(Signal.id)).filter(
        Signal.tenant_id == tid, Signal.status == "active",
        Signal.freshness_state.in_(["stale", "expired"])).scalar() or 0
    open_findings = db.query(func.count(Finding.id)).filter(
        Finding.tenant_id == tid,
        Finding.status.in_(["open", "reopened", "in_progress"])).scalar() or 0
    by_provider = {p: int(n) for p, n in db.query(Signal.provider, func.count(Signal.id))
                   .filter(Signal.tenant_id == tid, Signal.status == "active")
                   .group_by(Signal.provider).all()}
    return {"active_signals": int(active), "stale_signals": int(stale),
            "open_findings": int(open_findings), "by_provider": by_provider,
            "providers": _provider_health(db, tid), "coverage": _coverage(db, tid)}


@router.get("/{signal_id}")
def get_signal(signal_id: str,
               principal: security.Principal = Depends(security.get_principal),
               tenant: Tenant = Depends(security.get_tenant),
               db: Session = Depends(get_db)):
    _guard(principal, tenant, db)
    s = db.get(Signal, signal_id)
    if not s or s.tenant_id != tenant.id:
        raise HTTPException(404, "signal not found")
    view = _signal_view(s)
    view["meta"] = s.meta or {}
    view["related_findings"] = [
        _finding_view(f) for f in db.query(Finding)
        .filter(Finding.tenant_id == tenant.id,
                Finding.subject_id == s.subject_id).limit(20).all()]
    return view


@router.get("/providers/health")
def providers_health(principal: security.Principal = Depends(security.get_principal),
                     tenant: Tenant = Depends(security.get_tenant),
                     db: Session = Depends(get_db)):
    _guard(principal, tenant, db)
    return {"providers": _provider_health(db, tenant.id)}


@router.get("/findings")
def list_findings(status: str | None = None, severity: str | None = None,
                  finding_type: str | None = None, limit: int = 200, offset: int = 0,
                  principal: security.Principal = Depends(security.get_principal),
                  tenant: Tenant = Depends(security.get_tenant),
                  db: Session = Depends(get_db)):
    _guard(principal, tenant, db)
    q = db.query(Finding).filter(Finding.tenant_id == tenant.id)
    if status:
        q = q.filter(Finding.status == status)
    elif status is None:
        q = q.filter(Finding.status.in_(["open", "reopened", "in_progress", "acknowledged"]))
    if severity:
        q = q.filter(Finding.severity == severity)
    if finding_type:
        q = q.filter(Finding.finding_type == finding_type)
    total = q.count()
    rows = (q.order_by(Finding.last_seen.desc())
            .offset(max(0, offset)).limit(min(1000, max(1, limit))).all())
    return {"total": total, "findings": [_finding_view(f) for f in rows]}


@router.post("/findings/{finding_id}/status")
def set_finding_status(finding_id: str, body: dict,
                       principal: security.Principal = Depends(security.get_principal),
                       tenant: Tenant = Depends(security.get_tenant),
                       db: Session = Depends(get_db)):
    _guard(principal, tenant, db)
    f = db.get(Finding, finding_id)
    if not f or f.tenant_id != tenant.id:
        raise HTTPException(404, "finding not found")
    new_status = (body or {}).get("status") or ""
    allowed = {"open", "acknowledged", "in_progress", "risk_accepted", "exception",
               "resolved", "reopened"}
    if new_status not in allowed:
        raise HTTPException(400, f"invalid status; allowed: {sorted(allowed)}")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    f.status = new_status
    if new_status == "resolved":
        f.resolved_at = now
    if new_status in ("risk_accepted", "exception"):
        f.exception_reason = ((body or {}).get("reason") or "")[:1000]
    f.updated_at = now
    audit.record(db, actor=principal.user_id, action="finding.status_changed",
                 tenant_id=tenant.id, resource=f.id, category="admin", severity="info",
                 detail={"finding_type": f.finding_type, "status": new_status,
                         "reason": (body or {}).get("reason", "")[:300]})
    db.commit()
    return _finding_view(f)


# ------------------------------------------------------------------ helpers ----
def _provider_health(db: Session, tid: str) -> list[dict]:
    out = []
    for h in db.query(SignalProviderHealth).filter(
            SignalProviderHealth.tenant_id == tid).all():
        out.append({
            "provider": h.provider, "connection_status": h.connection_status,
            "last_attempt_at": h.last_attempt_at.isoformat() if h.last_attempt_at else None,
            "last_success_at": h.last_success_at.isoformat() if h.last_success_at else None,
            "objects_processed": int(h.objects_processed or 0),
            "signals_produced": int(h.signals_produced or 0),
            "errors": int(h.errors or 0), "last_error": h.last_error or "",
            "permissions_state": h.permissions_state or "",
            "capabilities": h.capabilities or [], "coverage": h.coverage or {},
        })
    return out


def _coverage(db: Session, tid: str) -> dict:
    """Simple coverage per dimension derived from provider health + entity reporting."""
    from ..models import DesktopAgent
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    health = {h.provider: h for h in db.query(SignalProviderHealth)
              .filter(SignalProviderHealth.tenant_id == tid).all()}

    def _configured(prov: str) -> bool:
        h = health.get(prov)
        return bool(h and h.connection_status == "ok")

    # Endpoint: reporting agents / total agents.
    agents = db.query(DesktopAgent).filter(DesktopAgent.tenant_id == tid,
                                           DesktopAgent.state != "retired").all()
    reporting = sum(1 for a in agents if a.last_heartbeat_at
                    and (now - a.last_heartbeat_at.replace(tzinfo=None)).total_seconds() < 86400)
    endpoint_pct = round(reporting * 100 / len(agents)) if agents else 100
    return {
        "Identity": 100 if _configured("m365") else 0,
        "Endpoint": endpoint_pct,
        "Network": 100 if _configured("ubiquiti") else 0,
        "Applications": 100 if (_configured("ubiquiti") or _configured("endpoint")) else 0,
        "Protection": 100 if _configured("arkive") else 0,
        "Recovery": 100 if _configured("arkive") else 0,
    }
