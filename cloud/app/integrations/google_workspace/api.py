"""Google Workspace Managed Integration — control-plane API (self-contained).

Organization-scoped, admin-governed endpoints for the connection lifecycle and
directory discovery, mirroring the Microsoft 365 router. Entitlement
(Business/Enterprise plan + org-admin) is enforced on every route. The reusable
service-account key is stored ENCRYPTED in the instance credentials blob and never
returned; discovery reads it from there. Collection + posture run on the assigned
customer node in later phases; this router owns the control-plane records.

Registered in ``main.py`` as ``google_workspace.api.router``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ... import audit, credstore, security
from ...db import get_db
from ...models import IntegrationInstance, Tenant, User
from . import collect
from . import directory
from . import models as m

logger = logging.getLogger("cv.integrations.google_workspace")

INTEGRATION_TYPE = "google_workspace"
_PLANS = ("business", "enterprise")

router = APIRouter(prefix="/integrations/google_workspace", tags=["integrations-gw"])


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def require_gw(principal: security.Principal = Depends(security.require_org_admin),
              db: Session = Depends(get_db)) -> security.Principal:
    """Fail-closed entitlement: org-admin + Business/Enterprise plan."""
    tenant = db.get(Tenant, principal.tenant_id)
    plan = (tenant.plan if tenant else "") or ""
    if plan.lower() not in _PLANS:
        raise HTTPException(403, "Google Workspace requires the Business or Enterprise plan")
    return principal


def _resolve(db: Session, tenant_id: str, instance_id: str = "") -> IntegrationInstance | None:
    q = db.query(IntegrationInstance).filter(
        IntegrationInstance.tenant_id == tenant_id,
        IntegrationInstance.integration_type == INTEGRATION_TYPE)
    if instance_id:
        return q.filter(IntegrationInstance.id == instance_id).first()
    return q.order_by(IntegrationInstance.created_at.desc()).first()


def _credential(db: Session, inst: IntegrationInstance) -> m.GwManagedCredential | None:
    return (db.query(m.GwManagedCredential)
            .filter(m.GwManagedCredential.integration_instance_id == inst.id).first())


def _status_view(db: Session, inst: IntegrationInstance) -> dict:
    cred = _credential(db, inst)
    idq = db.query(m.GwExternalIdentity).filter(
        m.GwExternalIdentity.integration_instance_id == inst.id)
    total = idq.count()
    in_scope = idq.filter(m.GwExternalIdentity.in_scope.is_(True)).count()
    mapped = (db.query(m.GwIdentityBinding)
              .filter(m.GwIdentityBinding.integration_instance_id == inst.id,
                      m.GwIdentityBinding.status.in_(("mapped", "protected_only"))).count())
    sq = db.query(m.GwManagedSource).filter(
        m.GwManagedSource.integration_instance_id == inst.id)
    src_total = sq.count()
    src_active = sq.filter(m.GwManagedSource.state == "active").count()
    cfg = inst.config or {}
    prof = cfg.get("managed_profile") or {}
    return {
        "instance_id": inst.id,
        "label": inst.label,
        "connected": bool(cred and cred.consent_state == "granted"),
        "node_id": inst.node_id,
        "subject_admin": cred.subject_admin if cred else "",
        "primary_domain": cred.primary_domain if cred else "",
        "customer_id": cred.customer_id if cred else "",
        "consent_state": cred.consent_state if cred else "pending",
        "service_account_email": cred.service_account_email if cred else "",
        "identities": {"total": total, "in_scope": in_scope, "mapped": mapped},
        "sources": {"total": src_total, "active": src_active},
        "collect_enabled": bool(cfg.get("collect_enabled")),
        "workloads": [w for w in (prof.get("workloads") or []) if w in collect._WORKLOADS]
                      or list(collect._DEFAULT_WORKLOADS),
        "status": inst.status,
        "last_error": inst.last_error or "",
    }


# --------------------------------------------------------------------------- #
# Views                                                                        #
# --------------------------------------------------------------------------- #
@router.get("")
def overview(instance_id: str = "",
             principal: security.Principal = Depends(require_gw),
             db: Session = Depends(get_db)):
    inst = _resolve(db, principal.tenant_id, instance_id)
    instances = (db.query(IntegrationInstance)
                 .filter(IntegrationInstance.tenant_id == principal.tenant_id,
                         IntegrationInstance.integration_type == INTEGRATION_TYPE)
                 .order_by(IntegrationInstance.created_at.asc()).all())
    return {
        "connected": inst is not None,
        "instances": [{"id": i.id, "label": i.label, "status": i.status} for i in instances],
        "instance": _status_view(db, inst) if inst else None,
        "workload_catalog": [{"id": w, "label": v["label"]} for w, v in collect._WORKLOADS.items()],
    }


@router.get("/identities")
def identities(instance_id: str = "",
               principal: security.Principal = Depends(require_gw),
               db: Session = Depends(get_db)):
    inst = _resolve(db, principal.tenant_id, instance_id)
    if inst is None:
        raise HTTPException(404, "not connected")
    bindings = {b.external_identity_id: b for b in db.query(m.GwIdentityBinding).filter(
        m.GwIdentityBinding.integration_instance_id == inst.id).all()}
    rows = (db.query(m.GwExternalIdentity)
            .filter(m.GwExternalIdentity.integration_instance_id == inst.id)
            .order_by(m.GwExternalIdentity.primary_email.asc()).limit(5000).all())
    out = []
    for r in rows:
        b = bindings.get(r.id)
        out.append({
            "id": r.id, "email": r.primary_email, "display_name": r.display_name,
            "suspended": r.suspended, "is_admin": r.is_admin, "org_unit": r.org_unit_path,
            "user_type": r.user_type, "in_scope": r.in_scope, "scope_reason": r.scope_reason,
            "state": r.state,
            "binding": {"user_id": b.user_id, "status": b.status,
                        "method": b.mapping_method} if b else None,
        })
    return {"identities": out}


@router.get("/sources")
def sources(instance_id: str = "",
            principal: security.Principal = Depends(require_gw),
            db: Session = Depends(get_db)):
    inst = _resolve(db, principal.tenant_id, instance_id)
    if inst is None:
        raise HTTPException(404, "not connected")
    rows = (db.query(m.GwManagedSource)
            .filter(m.GwManagedSource.integration_instance_id == inst.id)
            .order_by(m.GwManagedSource.workload.asc()).all())
    return {"sources": [{
        "id": s.id, "workload": s.workload, "ownership_type": s.ownership_type,
        "owner_user_id": s.owner_user_id, "name": s.name, "state": s.state,
        "source_key": s.source_key,
        "last_collected_at": s.last_collected_at.isoformat() if s.last_collected_at else None,
    } for s in rows]}


# --------------------------------------------------------------------------- #
# Lifecycle                                                                    #
# --------------------------------------------------------------------------- #
class ConnectBody(BaseModel):
    label: str = ""
    service_account_json: str = ""   # the downloaded SA key (JSON string)
    subject_admin: str = ""          # admin email the SA impersonates for directory reads
    primary_domain: str = ""
    customer_id: str = "my_customer"
    node_id: str | None = None


def _validate_sa(raw: str) -> dict:
    """Parse + sanity-check a service-account key JSON (never log its contents)."""
    try:
        info = json.loads(raw)
    except (ValueError, TypeError):
        raise HTTPException(400, "The service-account key must be valid JSON.")
    if not isinstance(info, dict) or info.get("type") != "service_account" \
            or not info.get("client_email") or not info.get("private_key"):
        raise HTTPException(400, "That doesn't look like a Google service-account key "
                                 "(expected type=service_account with client_email + private_key).")
    return info


@router.post("/connect")
def connect(body: ConnectBody,
            principal: security.Principal = Depends(require_gw),
            db: Session = Depends(get_db)):
    """Create a Google Workspace integration instance, store its domain-wide-
    delegation service-account key (encrypted), and record the authorization. Runs
    an initial directory discovery best-effort so the admin sees users immediately."""
    if not body.subject_admin.strip():
        raise HTTPException(400, "A Workspace admin email to impersonate is required.")
    info = _validate_sa(body.service_account_json)
    tenant = db.get(Tenant, principal.tenant_id)
    node_id = body.node_id or (tenant.node_id if tenant else None)
    existing = (db.query(IntegrationInstance)
                .filter(IntegrationInstance.tenant_id == principal.tenant_id,
                        IntegrationInstance.integration_type == INTEGRATION_TYPE).count())
    label = (body.label or "").strip() or (
        "Google Workspace" if existing == 0 else f"Google Workspace #{existing + 1}")
    inst = IntegrationInstance(
        tenant_id=principal.tenant_id, owner_user_id=None,
        integration_type=INTEGRATION_TYPE, label=label,
        runs_on="node", node_id=node_id, enabled=True,
        status="pending", provision_state="starting",
        provision_message="Discovering directory users",
        # The reusable SA key lives ONLY in the encrypted credentials blob.
        credentials=credstore.encrypt(principal.tenant_id,
                                      {"service_account_json": body.service_account_json}),
        config={})
    db.add(inst)
    db.flush()
    cred = m.GwManagedCredential(
        tenant_id=principal.tenant_id, integration_instance_id=inst.id,
        provider="google", auth_model="domain_wide_delegation",
        customer_id=(body.customer_id or "my_customer").strip(),
        primary_domain=body.primary_domain.strip(),
        service_account_email=info.get("client_email", ""),
        subject_admin=body.subject_admin.strip(),
        consent_state="granted", assigned_node_id=inst.node_id,
        scopes_granted=[directory.DIRECTORY_SCOPE])
    db.add(cred)
    db.commit()
    audit.record(db, actor=principal.user_id, action="google_workspace.connect",
                 category="admin", resource=inst.id,
                 detail={"label": label, "subject_admin": body.subject_admin.strip(),
                         "domain": body.primary_domain.strip()})
    logger.info("gw connect: created instance=%s label=%s node=%s (tenant=%s)",
                inst.id, label, node_id or "control-plane", principal.tenant_id)
    _run_discovery_safe(db, inst)
    return {"ok": True, **_status_view(db, inst)}


@router.post("/discover")
def discover(instance_id: str = "",
             principal: security.Principal = Depends(require_gw),
             db: Session = Depends(get_db)):
    inst = _resolve(db, principal.tenant_id, instance_id)
    if inst is None:
        raise HTTPException(404, "not connected")
    n = _run_discovery_safe(db, inst)
    return {"ok": True, "discovered": n, **_status_view(db, inst)}


def _run_discovery_safe(db: Session, inst: IntegrationInstance) -> int:
    """Run directory discovery, turning a directory error into an actionable
    instance ``last_error`` (never a 500). Returns the count discovered."""
    try:
        n = directory.run_discovery(db, inst)
        inst.status = "active" if n else "pending"
        inst.last_error = ""
        db.commit()
        return n
    except directory.DirectoryError as exc:
        inst.status = "error"
        inst.last_error = str(exc)[:300]
        db.commit()
        logger.warning("gw discovery error (instance=%s): %s", inst.id, exc)
        raise HTTPException(502, f"Directory discovery failed: {exc}")


class CollectionBody(BaseModel):
    enabled: bool = True
    workloads: list[str] | None = None


def _auto_map(db: Session, inst: IntegrationInstance) -> int:
    """Bind in-scope directory identities to Arkive members by verified email — the
    frictionless path so protection can start without a manual mapping pass. Manual
    mapping/overrides land in a later slice; this never binds an unmatched user."""
    bound = 0
    users = {(u.email or "").lower(): u.id for u in db.query(User).filter(
        User.tenant_id == inst.tenant_id).all()}
    for ident in (db.query(m.GwExternalIdentity)
                  .filter(m.GwExternalIdentity.integration_instance_id == inst.id,
                          m.GwExternalIdentity.in_scope.is_(True)).all()):
        uid = users.get((ident.primary_email or "").lower())
        if not uid:
            continue
        b = (db.query(m.GwIdentityBinding)
             .filter(m.GwIdentityBinding.integration_instance_id == inst.id,
                     m.GwIdentityBinding.external_identity_id == ident.id).first())
        if b is None:
            b = m.GwIdentityBinding(
                tenant_id=inst.tenant_id, integration_instance_id=inst.id,
                external_identity_id=ident.id)
            db.add(b)
        b.user_id = uid
        b.status = "mapped"
        b.mapping_method = "verified_email"
        if ident.state == "discovered":
            ident.state = "mapped"
        bound += 1
    db.commit()
    return bound


@router.post("/collection")
def set_collection(body: CollectionBody, instance_id: str = "",
                   principal: security.Principal = Depends(require_gw),
                   db: Session = Depends(get_db)):
    """Enable/disable managed protection for this instance. Enabling auto-maps
    in-scope users to Arkive members by email and provisions a managed source +
    protecting Collection per user per selected workload (Gmail/Drive/Calendar/
    Contacts), collected via domain-wide delegation on the shared scheduler."""
    inst = _resolve(db, principal.tenant_id, instance_id)
    if inst is None:
        raise HTTPException(404, "not connected")
    cfg = dict(inst.config or {})
    prof = dict(cfg.get("managed_profile") or {})
    if body.workloads is not None:
        prof["workloads"] = [w for w in body.workloads if w in collect._WORKLOADS]
    cfg["managed_profile"] = prof
    cfg["collect_enabled"] = bool(body.enabled)
    inst.config = cfg
    db.commit()
    provisioned = 0
    if body.enabled:
        mapped = _auto_map(db, inst)
        provisioned = collect.provision_sources(db, inst)
        audit.record(db, actor=principal.user_id, action="google_workspace.collection_enabled",
                     category="admin", resource=inst.id,
                     detail={"mapped": mapped, "sources": provisioned,
                             "workloads": prof.get("workloads") or list(collect._DEFAULT_WORKLOADS)})
    else:
        for s in db.query(m.GwManagedSource).filter(
                m.GwManagedSource.integration_instance_id == inst.id,
                m.GwManagedSource.state != "decommissioned").all():
            s.state = "paused_by_admin"
        db.commit()
        audit.record(db, actor=principal.user_id, action="google_workspace.collection_disabled",
                     category="admin", resource=inst.id)
    return {"ok": True, "collect_enabled": bool(body.enabled),
            "sources_provisioned": provisioned, **_status_view(db, inst)}


@router.post("/disconnect")
def disconnect(instance_id: str = "",
               principal: security.Principal = Depends(require_gw),
               db: Session = Depends(get_db)):
    inst = _resolve(db, principal.tenant_id, instance_id)
    if inst is None:
        raise HTTPException(404, "not connected")
    inst.enabled = False
    inst.status = "disabled"
    inst.credentials = None  # drop the reusable SA key
    cred = _credential(db, inst)
    if cred:
        cred.consent_state = "revoked"
    db.commit()
    audit.record(db, actor=principal.user_id, action="google_workspace.disconnect",
                 category="admin", resource=inst.id)
    return {"ok": True}


@router.post("/remove")
def remove(instance_id: str = "",
           principal: security.Principal = Depends(require_gw),
           db: Session = Depends(get_db)):
    inst = _resolve(db, principal.tenant_id, instance_id)
    if inst is None:
        raise HTTPException(404, "not connected")
    iid = inst.id
    for model in (m.GwManagedSource, m.GwIdentityBinding, m.GwExternalIdentity,
                  m.GwScopePolicy, m.GwManagedCredential):
        db.query(model).filter(model.integration_instance_id == iid).delete(
            synchronize_session=False)
    db.delete(inst)
    db.commit()
    audit.record(db, actor=principal.user_id, action="google_workspace.remove",
                 category="admin", resource=iid)
    logger.info("gw remove: deleted instance=%s (tenant=%s)", iid, principal.tenant_id)
    return {"ok": True}
