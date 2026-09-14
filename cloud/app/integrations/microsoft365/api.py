"""Microsoft 365 Managed Integration — control-plane API (self-contained).

Organization-scoped, admin-governed endpoints for the connection lifecycle,
scope, Entra identity mapping and activation. Entitlement (Business/Enterprise
plan + the ``m365_managed_integration`` flag) is enforced server-side on every
route. Collection itself runs on the assigned customer node; this router manages
only the control-plane records the CP is authoritative for (desired state, scope,
mappings, identity decisions, audit) — never Microsoft payloads, tokens or keys.

Registered in ``main.py`` as ``microsoft365.api.router``.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ... import audit, security
from ...config import get_settings
from ...db import get_db
from ...models import IntegrationInstance, SystemSetting, Tenant, User
from . import models as m

logger = logging.getLogger("cv.integrations.m365")

INTEGRATION_TYPE = "microsoft365"
_PLANS = ("business", "enterprise")

router = APIRouter(prefix="/integrations/microsoft365", tags=["integrations-m365"])


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _setting(db: Session, key: str) -> str:
    """Read a platform-level setting (e.g. the Arkive M365 app client id)."""
    row = db.get(SystemSetting, key)
    return (row.value if row and row.value else "") if row else ""


# --------------------------------------------------------------------------- #
# Entitlement — fail closed (plan + feature flag + org-admin)                 #
# --------------------------------------------------------------------------- #
def require_m365(principal: security.Principal = Depends(security.require_org_admin),
                db: Session = Depends(get_db)) -> security.Principal:
    from ... import features
    tenant = db.get(Tenant, principal.tenant_id)
    user = db.get(User, principal.user_id)
    plan = (tenant.plan if tenant else "") or ""
    if plan.lower() not in _PLANS:
        raise HTTPException(403, "Microsoft 365 requires the Business or Enterprise plan")
    if not features.resolve(user, tenant, "m365_managed_integration"):
        raise HTTPException(403, "Microsoft 365 isn't enabled for your organization yet")
    return principal


def _instance(db: Session, tenant_id: str) -> IntegrationInstance | None:
    return (db.query(IntegrationInstance)
            .filter(IntegrationInstance.tenant_id == tenant_id,
                    IntegrationInstance.integration_type == INTEGRATION_TYPE).first())


def _credential(db: Session, inst: IntegrationInstance) -> m.ManagedCredentialRef | None:
    return (db.query(m.ManagedCredentialRef)
            .filter(m.ManagedCredentialRef.integration_instance_id == inst.id).first())


def _status_view(db: Session, inst: IntegrationInstance | None) -> dict:
    if inst is None:
        return {"connected": False, "state": "not_connected"}
    cred = _credential(db, inst)
    ext_q = db.query(m.ExternalIdentity).filter(
        m.ExternalIdentity.integration_instance_id == inst.id)
    mapped = db.query(m.ExternalIdentityBinding).filter(
        m.ExternalIdentityBinding.integration_instance_id == inst.id,
        m.ExternalIdentityBinding.status == "mapped").count()
    sources = db.query(m.ManagedSource).filter(
        m.ManagedSource.integration_instance_id == inst.id).count()
    return {
        "connected": True,
        "instance_id": inst.id,
        "state": inst.provision_state or inst.status,
        "status": inst.status,
        "assigned_node_id": inst.node_id,
        "microsoft_tenant_id": (cred.microsoft_tenant_id if cred else ""),
        "consent_state": (cred.consent_state if cred else "pending"),
        "scopes_granted": (cred.scopes_granted if cred else []),
        "identities_discovered": ext_q.count(),
        "identities_mapped": mapped,
        "managed_sources": sources,
        "collect_enabled": bool((inst.config or {}).get("collect_enabled")),
        "last_run_at": inst.last_run_at.isoformat() if inst.last_run_at else None,
    }


@router.get("")
def status(principal: security.Principal = Depends(require_m365),
           db: Session = Depends(get_db)):
    """Connection state, consent, discovery/mapping counts for this organization."""
    return _status_view(db, _instance(db, principal.tenant_id))


# --------------------------------------------------------------------------- #
# Connect + OAuth admin consent                                               #
# --------------------------------------------------------------------------- #
class ConnectBody(BaseModel):
    node_id: str | None = None           # assigned customer node (defaults to routing)
    capabilities: list[str] = []         # bundles to request consent for


@router.post("/connect")
def connect(body: ConnectBody,
            principal: security.Principal = Depends(require_m365),
            db: Session = Depends(get_db)):
    """Create (or return) the org's Microsoft 365 integration instance and its
    managed-credential reference, ready for admin consent."""
    inst = _instance(db, principal.tenant_id)
    if inst is None:
        inst = IntegrationInstance(
            tenant_id=principal.tenant_id, owner_user_id=None,
            integration_type=INTEGRATION_TYPE, label="Microsoft 365",
            runs_on="node", node_id=body.node_id, enabled=True,
            status="pending", provision_state="starting",
            provision_message="Awaiting Microsoft administrator consent",
            config={"capabilities": body.capabilities})
        db.add(inst)
        db.flush()
    cred = _credential(db, inst)
    if cred is None:
        cred = m.ManagedCredentialRef(
            tenant_id=principal.tenant_id, integration_instance_id=inst.id,
            provider="microsoft", auth_model="oauth_admin_consent",
            consent_state="pending", assigned_node_id=inst.node_id,
            scopes_granted=[])
        db.add(cred)
    db.commit()
    audit.record(db, actor=principal.user_id, action="m365.connect_started",
                 category="admin", resource=inst.id,
                 detail={"capabilities": body.capabilities})
    return {"ok": True, **_status_view(db, inst)}


@router.post("/oauth/start")
def oauth_start(principal: security.Principal = Depends(require_m365),
                db: Session = Depends(get_db)):
    """Return the Microsoft admin-consent URL + a signed state to begin consent.
    Uses the Arkive-published Entra application (client_id from the linked Config
    Object). Microsoft redirects the browser to our public callback, which records
    consent — the reusable secret is never held in the control plane per se; it is
    used app-only on the box that collects."""
    inst = _instance(db, principal.tenant_id)
    if inst is None:
        raise HTTPException(409, "connect first")
    from ... import platform_config
    vals = platform_config.integration_values(INTEGRATION_TYPE)
    client_id = (vals.get("client_id") or "").strip()
    if not client_id:
        return {"consent_configured": False,
                "message": "The Arkive Microsoft 365 application isn't configured on this "
                           "platform yet. A platform administrator must register it and link "
                           "its client id/secret in Admin → Sources → Managed integrations."}
    state = secrets.token_urlsafe(24)
    cred = _credential(db, inst)
    if cred is not None:
        meta = dict(cred.meta or {})
        meta["oauth_state"] = state
        cred.meta = meta
        db.commit()
    redirect = (vals.get("redirect_uri") or "").strip() or _default_redirect()
    consent_url = ("https://login.microsoftonline.com/organizations/v2.0/adminconsent?"
                   + urlencode({"client_id": client_id, "state": state,
                                "redirect_uri": redirect}))
    return {"consent_configured": True, "consent_url": consent_url, "state": state,
            "redirect_uri": redirect}


def _default_redirect() -> str:
    return f"https://{get_settings().domain}/api/integrations/microsoft365/oauth/redirect"


@router.get("/oauth/redirect")
def oauth_redirect(state: str = "", tenant: str = "", admin_consent: str = "",
                   error: str = "", error_description: str = "",
                   db: Session = Depends(get_db)):
    """Public landing for Microsoft's admin-consent redirect (browser GET, no
    session). Matches the signed state to the pending credential, records consent
    and the Microsoft tenant id, then returns the admin to the portal."""
    portal = f"https://{get_settings().domain}/integrations"
    if not state:
        return RedirectResponse(portal + "?m365=error", status_code=302)
    cred = None
    for c in (db.query(m.ManagedCredentialRef)
              .filter(m.ManagedCredentialRef.consent_state != "granted").all()):
        if (c.meta or {}).get("oauth_state") == state:
            cred = c
            break
    if cred is None:
        return RedirectResponse(portal + "?m365=error", status_code=302)
    inst = db.get(IntegrationInstance, cred.integration_instance_id)
    if error or (admin_consent and admin_consent.lower() not in ("true", "1")):
        cred.consent_state = "error"
        db.commit()
        logger.warning("m365 consent denied (instance=%s): %s %s",
                       cred.integration_instance_id, error, error_description[:120])
        return RedirectResponse(portal + "?m365=denied", status_code=302)
    cred.consent_state = "granted"
    cred.microsoft_tenant_id = (tenant or "").strip()
    meta = dict(cred.meta or {})
    meta.pop("oauth_state", None)
    cred.meta = meta
    if inst is not None:
        inst.provision_state = "done"
        inst.provision_message = "Connected — ready to discover users"
        inst.status = "active"
    db.commit()
    audit.record(db, actor="microsoft", action="m365.consent_granted",
                 category="admin", resource=cred.integration_instance_id,
                 detail={"microsoft_tenant_id": cred.microsoft_tenant_id})
    logger.info("m365 consent granted (instance=%s tenant=%s)",
                cred.integration_instance_id, cred.microsoft_tenant_id)
    return RedirectResponse(portal + "?m365=connected", status_code=302)


class ConsentCallback(BaseModel):
    state: str
    microsoft_tenant_id: str
    admin_consent: bool = True


@router.post("/oauth/callback")
def oauth_callback(body: ConsentCallback,
                   principal: security.Principal = Depends(require_m365),
                   db: Session = Depends(get_db)):
    """Record a completed admin-consent. Validates the signed state bound to this
    org's credential reference (fail closed on mismatch)."""
    inst = _instance(db, principal.tenant_id)
    cred = _credential(db, inst) if inst else None
    if inst is None or cred is None:
        raise HTTPException(409, "connect first")
    if not body.admin_consent:
        cred.consent_state = "error"
        db.commit()
        raise HTTPException(400, "administrator did not grant consent")
    if (cred.meta or {}).get("oauth_state") != body.state:
        raise HTTPException(400, "invalid or expired consent state")
    cred.consent_state = "granted"
    cred.microsoft_tenant_id = body.microsoft_tenant_id.strip()
    meta = dict(cred.meta or {})
    meta.pop("oauth_state", None)
    cred.meta = meta
    inst.provision_state = "done"
    inst.provision_message = "Connected — ready to discover users"
    inst.status = "active"
    db.commit()
    audit.record(db, actor=principal.user_id, action="m365.consent_granted",
                 category="admin", resource=inst.id,
                 detail={"microsoft_tenant_id": body.microsoft_tenant_id})
    return {"ok": True, **_status_view(db, inst)}


# --------------------------------------------------------------------------- #
# Scope                                                                        #
# --------------------------------------------------------------------------- #
class ScopeBody(BaseModel):
    rules: dict = {}


@router.get("/scope")
def get_scope(principal: security.Principal = Depends(require_m365),
              db: Session = Depends(get_db)):
    inst = _instance(db, principal.tenant_id)
    if inst is None:
        raise HTTPException(409, "connect first")
    sp = (db.query(m.IdentityScopePolicy)
          .filter(m.IdentityScopePolicy.integration_instance_id == inst.id).first())
    return {"rules": (sp.rules if sp else {}), "version": (sp.version if sp else 0)}


@router.put("/scope")
def set_scope(body: ScopeBody,
              principal: security.Principal = Depends(require_m365),
              db: Session = Depends(get_db)):
    inst = _instance(db, principal.tenant_id)
    if inst is None:
        raise HTTPException(409, "connect first")
    sp = (db.query(m.IdentityScopePolicy)
          .filter(m.IdentityScopePolicy.integration_instance_id == inst.id).first())
    if sp is None:
        sp = m.IdentityScopePolicy(tenant_id=principal.tenant_id,
                                   integration_instance_id=inst.id,
                                   rules=body.rules, version=1,
                                   updated_by=principal.user_id)
        db.add(sp)
    else:
        sp.rules = body.rules
        sp.version = int(sp.version or 0) + 1
        sp.updated_by = principal.user_id
    db.commit()
    audit.record(db, actor=principal.user_id, action="m365.scope_updated",
                 category="admin", resource=inst.id, detail={"version": sp.version})
    return {"rules": sp.rules, "version": sp.version}


# --------------------------------------------------------------------------- #
# Identities + mapping decisions                                              #
# --------------------------------------------------------------------------- #
@router.get("/identities")
def list_identities(state: str = "", limit: int = 500,
                    principal: security.Principal = Depends(require_m365),
                    db: Session = Depends(get_db)):
    inst = _instance(db, principal.tenant_id)
    if inst is None:
        raise HTTPException(409, "connect first")
    q = db.query(m.ExternalIdentity).filter(
        m.ExternalIdentity.integration_instance_id == inst.id)
    if state:
        q = q.filter(m.ExternalIdentity.state == state)
    rows = q.order_by(m.ExternalIdentity.display_name.asc()).limit(max(1, min(2000, limit))).all()
    bindings = {b.external_identity_id: b for b in db.query(m.ExternalIdentityBinding).filter(
        m.ExternalIdentityBinding.integration_instance_id == inst.id).all()}
    out = []
    for e in rows:
        b = bindings.get(e.id)
        out.append({
            "id": e.id, "display_name": e.display_name, "upn": e.upn, "email": e.email,
            "entra_object_id": e.entra_object_id, "account_enabled": e.account_enabled,
            "user_type": e.user_type, "in_scope": e.in_scope, "state": e.state,
            "scope_reason": e.scope_reason,
            "binding": None if not b else {
                "user_id": b.user_id, "status": b.status, "protected_only": b.protected_only,
                "mapping_method": b.mapping_method},
        })
    return {"identities": out, "count": len(out)}


class IdentityDecision(BaseModel):
    external_identity_id: str
    action: str                 # map | create_user | protected_only | exclude
    user_id: str | None = None  # for action=map


class IdentityDecisions(BaseModel):
    decisions: list[IdentityDecision]


@router.post("/identities/decisions")
def identity_decisions(body: IdentityDecisions,
                       principal: security.Principal = Depends(require_m365),
                       db: Session = Depends(get_db)):
    """Apply per-identity mapping decisions (idempotent, per-item results). Mapping
    never grants portal access; account creation/invite is a separate action."""
    inst = _instance(db, principal.tenant_id)
    if inst is None:
        raise HTTPException(409, "connect first")
    results = []
    for d in body.decisions:
        e = db.get(m.ExternalIdentity, d.external_identity_id)
        if not e or e.integration_instance_id != inst.id:
            results.append({"id": d.external_identity_id, "ok": False, "error": "not found"})
            continue
        binding = (db.query(m.ExternalIdentityBinding)
                   .filter(m.ExternalIdentityBinding.external_identity_id == e.id).first())
        if binding is None:
            binding = m.ExternalIdentityBinding(
                tenant_id=principal.tenant_id, integration_instance_id=inst.id,
                external_identity_id=e.id)
            db.add(binding)
        if d.action == "map":
            if not d.user_id:
                results.append({"id": e.id, "ok": False, "error": "user_id required"})
                continue
            member = db.get(User, d.user_id)
            if not member or member.tenant_id != principal.tenant_id:
                results.append({"id": e.id, "ok": False, "error": "user not in org"})
                continue
            binding.user_id = d.user_id
            binding.protected_only = False
            binding.status = "mapped"
            binding.mapping_method = binding.mapping_method or "manual"
            binding.approved_by = principal.user_id
            e.state = "mapped"
        elif d.action == "protected_only":
            binding.user_id = None
            binding.protected_only = True
            binding.status = "protected_only"
            e.state = "protected_only"
        elif d.action == "exclude":
            binding.status = "excluded"
            e.state = "excluded"
            e.in_scope = False
        elif d.action == "create_user":
            # Account creation is a distinct, audited flow; record intent as a
            # candidate for the identity-admin to complete (no silent seat grant).
            binding.status = "suggested"
            e.state = "new_user_candidate"
        else:
            results.append({"id": e.id, "ok": False, "error": "unknown action"})
            continue
        binding.updated_at = _now()
        results.append({"id": e.id, "ok": True, "state": e.state})
    db.commit()
    # Establish admin-level managed sources for the newly mapped users so protection
    # can begin (idempotent). Collection itself only runs once an admin enables it.
    try:
        from . import collect
        collect.provision_sources(db, inst)
    except Exception:  # noqa: BLE001
        logger.exception("m365 provision_sources after mapping failed (instance=%s)", inst.id)
    audit.record(db, actor=principal.user_id, action="m365.identity_decisions",
                 category="admin", resource=inst.id,
                 detail={"count": len(body.decisions)})
    return {"results": results}


# --------------------------------------------------------------------------- #
# Activation + desired state + disconnect                                     #
# --------------------------------------------------------------------------- #
@router.post("/activate")
def activate(principal: security.Principal = Depends(security.require_passkey),
             db: Session = Depends(get_db)):
    """Approve the activation plan: bump the signed desired-state the assigned node
    reconciles. Passkey step-up required. (Node applies + begins collection.)"""
    # Re-check entitlement explicitly (require_passkey doesn't imply it).
    require_m365(principal, db)
    inst = _instance(db, principal.tenant_id)
    if inst is None:
        raise HTTPException(409, "connect first")
    cred = _credential(db, inst)
    if not cred or cred.consent_state != "granted":
        raise HTTPException(409, "Microsoft administrator consent is required first")
    ds = (db.query(m.IntegrationDesiredState)
          .filter(m.IntegrationDesiredState.integration_instance_id == inst.id).first())
    scope = (db.query(m.IdentityScopePolicy)
             .filter(m.IdentityScopePolicy.integration_instance_id == inst.id).first())
    desired = {"scope_version": (scope.version if scope else 0),
               "capabilities": (inst.config or {}).get("capabilities", []),
               "microsoft_tenant_id": cred.microsoft_tenant_id}
    if ds is None:
        ds = m.IntegrationDesiredState(
            tenant_id=principal.tenant_id, integration_instance_id=inst.id,
            node_id=inst.node_id, version=1, desired=desired, status="pending_node")
        db.add(ds)
    else:
        ds.version = int(ds.version or 0) + 1
        ds.desired = desired
        ds.status = "pending_node"
        ds.node_id = inst.node_id
    inst.status = "active"
    inst.provision_state = "done"
    db.commit()
    audit.record(db, actor=principal.user_id, action="m365.activated",
                 category="admin", severity="notice", resource=inst.id,
                 detail={"desired_version": ds.version})
    return {"ok": True, "desired_version": ds.version, "status": "pending_node",
            "note": "The assigned node will apply this desired state and begin discovery/collection."}


@router.post("/disconnect")
def disconnect(principal: security.Principal = Depends(security.require_passkey),
               db: Session = Depends(get_db)):
    """Safely disconnect: stop future collection, retain protected data + checkpoints.
    Does NOT delete Arkive data (a purge is a separate, audited action)."""
    require_m365(principal, db)
    inst = _instance(db, principal.tenant_id)
    if inst is None:
        raise HTTPException(404, "not connected")
    inst.enabled = False
    inst.status = "disabled"
    inst.provision_state = "idle"
    cred = _credential(db, inst)
    if cred:
        cred.consent_state = "revoked"
    db.commit()
    audit.record(db, actor=principal.user_id, action="m365.disconnected",
                 category="admin", severity="warning", resource=inst.id)
    return {"ok": True, "note": "Disconnected. Protected data and recovery points are retained."}


@router.get("/members")
def org_members(principal: security.Principal = Depends(require_m365),
                db: Session = Depends(get_db)):
    """The organization's Arkive users, for mapping discovered Entra identities."""
    users = (db.query(User).filter(User.tenant_id == principal.tenant_id)
             .order_by(User.email.asc()).all())
    return {"members": [{"id": u.id,
                         "name": (getattr(u, "full_name", None) or u.display_name or u.email),
                         "email": u.email} for u in users]}


@router.post("/discover")
def discover(principal: security.Principal = Depends(require_m365),
             db: Session = Depends(get_db)):
    """Run Entra identity discovery now (synchronous). Requires granted consent and
    the platform Microsoft 365 app credentials (admin Config)."""
    inst = _instance(db, principal.tenant_id)
    if inst is None:
        raise HTTPException(409, "connect first")
    cred = _credential(db, inst)
    if not cred or cred.consent_state != "granted":
        raise HTTPException(409, "Microsoft administrator consent is required first")
    from ... import platform_config
    from . import discovery
    vals = platform_config.integration_values(INTEGRATION_TYPE)
    res = discovery.run_discovery(db, inst,
                                  client_id=(vals.get("client_id") or "").strip(),
                                  client_secret=(vals.get("client_secret") or "").strip())
    audit.record(db, actor=principal.user_id, action="m365.discovery_run",
                 category="admin", resource=inst.id,
                 detail={"ok": res.get("ok"), "discovered": res.get("discovered"),
                         "error": res.get("error")})
    if not res.get("ok"):
        raise HTTPException(400, res.get("error") or "discovery failed")
    return {**res, **_status_view(db, inst)}


@router.get("/sources")
def list_managed_sources(principal: security.Principal = Depends(require_m365),
                         db: Session = Depends(get_db)):
    """The admin-established managed sources (per-user Exchange / OneDrive)."""
    inst = _instance(db, principal.tenant_id)
    if inst is None:
        raise HTTPException(409, "connect first")
    rows = (db.query(m.ManagedSource)
            .filter(m.ManagedSource.integration_instance_id == inst.id)
            .order_by(m.ManagedSource.name.asc()).all())
    return {"collect_enabled": bool((inst.config or {}).get("collect_enabled")),
            "sources": [{"id": s.id, "workload": s.workload, "name": s.name,
                         "owner_user_id": s.owner_user_id, "state": s.state,
                         "last_collected_at": s.last_collected_at.isoformat() if s.last_collected_at else None}
                        for s in rows]}


class CollectionToggle(BaseModel):
    enabled: bool


@router.post("/collection")
def set_collection(body: CollectionToggle,
                   principal: security.Principal = Depends(security.require_passkey),
                   db: Session = Depends(get_db)):
    """Enable/disable admin-level content protection (Exchange + OneDrive) for the
    mapped users. Passkey step-up required. Provisions sources on enable."""
    require_m365(principal, db)
    inst = _instance(db, principal.tenant_id)
    if inst is None:
        raise HTTPException(409, "connect first")
    cred = _credential(db, inst)
    if body.enabled and (not cred or cred.consent_state != "granted"):
        raise HTTPException(409, "Microsoft administrator consent is required first")
    cfg = dict(inst.config or {})
    cfg["collect_enabled"] = bool(body.enabled)
    inst.config = cfg
    created = 0
    if body.enabled:
        from . import collect
        created = collect.provision_sources(db, inst)
    db.commit()
    audit.record(db, actor=principal.user_id, action="m365.collection_toggled",
                 category="admin", severity="notice", resource=inst.id,
                 detail={"enabled": body.enabled, "sources_provisioned": created})
    return {"ok": True, "collect_enabled": body.enabled, "sources_provisioned": created}


# --------------------------------------------------------------------------- #
# Managed rules (immutable versions + activation) + effective policy          #
# --------------------------------------------------------------------------- #
class RuleBody(BaseModel):
    name: str
    rule_family: str = "collection"
    spec: dict = {}


@router.get("/rules")
def list_rules(principal: security.Principal = Depends(require_m365),
               db: Session = Depends(get_db)):
    inst = _instance(db, principal.tenant_id)
    if inst is None:
        raise HTTPException(409, "connect first")
    rows = (db.query(m.ManagedRule)
            .filter(m.ManagedRule.integration_instance_id == inst.id)
            .order_by(m.ManagedRule.created_at.desc()).all())
    return {"rules": [{"id": r.id, "name": r.name, "rule_family": r.rule_family,
                       "status": r.status, "active_version": r.active_version} for r in rows]}


@router.post("/rules")
def create_rule(body: RuleBody,
                principal: security.Principal = Depends(require_m365),
                db: Session = Depends(get_db)):
    """Create a managed rule with an immutable draft version 1."""
    inst = _instance(db, principal.tenant_id)
    if inst is None:
        raise HTTPException(409, "connect first")
    rule = m.ManagedRule(tenant_id=principal.tenant_id, integration_instance_id=inst.id,
                         name=body.name.strip() or "Rule", rule_family=body.rule_family,
                         status="draft", active_version=0, created_by=principal.user_id)
    db.add(rule)
    db.flush()
    db.add(m.ManagedRuleVersion(tenant_id=principal.tenant_id, rule_id=rule.id,
                                version=1, spec=body.spec or {}))
    db.commit()
    audit.record(db, actor=principal.user_id, action="m365.rule_created",
                 category="admin", resource=rule.id, detail={"family": body.rule_family})
    return {"id": rule.id, "version": 1, "status": "draft"}


@router.post("/rules/{rule_id}/activate")
def activate_rule(rule_id: str,
                  principal: security.Principal = Depends(require_m365),
                  db: Session = Depends(get_db)):
    """Activate a rule's latest version — but only if the compiled effective policy
    has no invariant conflicts (legal hold / immutable retention / residency)."""
    from . import policy
    inst = _instance(db, principal.tenant_id)
    rule = db.get(m.ManagedRule, rule_id)
    if inst is None or not rule or rule.integration_instance_id != inst.id:
        raise HTTPException(404, "rule not found")
    latest = (db.query(m.ManagedRuleVersion)
              .filter(m.ManagedRuleVersion.rule_id == rule.id)
              .order_by(m.ManagedRuleVersion.version.desc()).first())
    if not latest:
        raise HTTPException(409, "rule has no version")
    prev_active = rule.active_version
    rule.active_version = latest.version
    rule.status = "active"
    rule.approved_by = principal.user_id
    db.flush()
    result = policy.compile_effective(db, principal.tenant_id, inst.id)
    if result["conflicts"]:
        db.rollback()  # never activate a rule that breaks an invariant
        raise HTTPException(409, {"error": "policy conflict", "conflicts": result["conflicts"]})
    db.commit()
    audit.record(db, actor=principal.user_id, action="m365.rule_activated",
                 category="admin", resource=rule.id,
                 detail={"version": latest.version, "prev": prev_active})
    return {"id": rule.id, "active_version": rule.active_version, "status": "active"}


class AssignBody(BaseModel):
    assignee_type: str = "organization"  # organization|group|user|source
    assignee_id: str = ""


@router.post("/rules/{rule_id}/assign")
def assign_rule(rule_id: str, body: AssignBody,
                principal: security.Principal = Depends(require_m365),
                db: Session = Depends(get_db)):
    inst = _instance(db, principal.tenant_id)
    rule = db.get(m.ManagedRule, rule_id)
    if inst is None or not rule or rule.integration_instance_id != inst.id:
        raise HTTPException(404, "rule not found")
    db.add(m.ManagedRuleAssignment(tenant_id=principal.tenant_id, rule_id=rule.id,
                                   assignee_type=body.assignee_type, assignee_id=body.assignee_id))
    db.commit()
    return {"ok": True}


@router.get("/policy")
def effective_policy(scope_ref: str = "",
                     principal: security.Principal = Depends(require_m365),
                     db: Session = Depends(get_db)):
    """Compile + return the effective policy for a scope (does not persist)."""
    from . import policy
    inst = _instance(db, principal.tenant_id)
    if inst is None:
        raise HTTPException(409, "connect first")
    return policy.compile_effective(db, principal.tenant_id, inst.id, scope_ref)


# --------------------------------------------------------------------------- #
# Managed mappings (immutable versions + activation)                          #
# --------------------------------------------------------------------------- #
class MappingBody(BaseModel):
    name: str
    spec: dict = {}
    visibility: dict = {}


@router.get("/mappings")
def list_mappings(principal: security.Principal = Depends(require_m365),
                  db: Session = Depends(get_db)):
    inst = _instance(db, principal.tenant_id)
    if inst is None:
        raise HTTPException(409, "connect first")
    rows = (db.query(m.ManagedMapping)
            .filter(m.ManagedMapping.integration_instance_id == inst.id).all())
    return {"mappings": [{"id": r.id, "name": r.name, "status": r.status,
                          "active_version": r.active_version} for r in rows]}


@router.post("/mappings")
def create_mapping(body: MappingBody,
                   principal: security.Principal = Depends(require_m365),
                   db: Session = Depends(get_db)):
    inst = _instance(db, principal.tenant_id)
    if inst is None:
        raise HTTPException(409, "connect first")
    mp = m.ManagedMapping(tenant_id=principal.tenant_id, integration_instance_id=inst.id,
                          name=body.name.strip() or "Mapping", status="draft",
                          active_version=0, visibility=body.visibility or {},
                          created_by=principal.user_id)
    db.add(mp)
    db.flush()
    db.add(m.ManagedMappingVersion(tenant_id=principal.tenant_id, mapping_id=mp.id,
                                   version=1, spec=body.spec or {}))
    db.commit()
    return {"id": mp.id, "version": 1, "status": "draft"}


@router.post("/mappings/{mapping_id}/activate")
def activate_mapping(mapping_id: str,
                     principal: security.Principal = Depends(require_m365),
                     db: Session = Depends(get_db)):
    inst = _instance(db, principal.tenant_id)
    mp = db.get(m.ManagedMapping, mapping_id)
    if inst is None or not mp or mp.integration_instance_id != inst.id:
        raise HTTPException(404, "mapping not found")
    latest = (db.query(m.ManagedMappingVersion)
              .filter(m.ManagedMappingVersion.mapping_id == mp.id)
              .order_by(m.ManagedMappingVersion.version.desc()).first())
    if not latest:
        raise HTTPException(409, "mapping has no version")
    mp.active_version = latest.version
    mp.status = "active"
    mp.approved_by = principal.user_id
    db.commit()
    audit.record(db, actor=principal.user_id, action="m365.mapping_activated",
                 category="admin", resource=mp.id, detail={"version": latest.version})
    return {"id": mp.id, "active_version": mp.active_version, "status": "active"}


# --------------------------------------------------------------------------- #
# Managed + organization sources                                              #
# --------------------------------------------------------------------------- #
class OrgSourceBody(BaseModel):
    workload: str                 # sharepoint|teams|exchange(shared)|onedrive(service)
    name: str
    source_key: str = ""          # Microsoft resource id
    custodian_user_ids: list[str] = []


@router.get("/sources")
def list_sources(principal: security.Principal = Depends(require_m365),
                 db: Session = Depends(get_db)):
    inst = _instance(db, principal.tenant_id)
    if inst is None:
        raise HTTPException(409, "connect first")
    rows = (db.query(m.ManagedSource)
            .filter(m.ManagedSource.integration_instance_id == inst.id)
            .order_by(m.ManagedSource.created_at.desc()).all())
    return {"sources": [{"id": s.id, "workload": s.workload, "name": s.name,
                         "ownership_type": s.ownership_type, "owner_user_id": s.owner_user_id,
                         "state": s.state, "source_key": s.source_key,
                         "last_collected_at": s.last_collected_at.isoformat() if s.last_collected_at else None}
                        for s in rows]}


@router.post("/organization-sources")
def create_org_source(body: OrgSourceBody,
                      principal: security.Principal = Depends(require_m365),
                      db: Session = Depends(get_db)):
    """Create an organization-owned source (no individual owner). At least one
    custodian assignment is expected; a fake user owner is never invented."""
    inst = _instance(db, principal.tenant_id)
    if inst is None:
        raise HTTPException(409, "connect first")
    src = m.ManagedSource(tenant_id=principal.tenant_id, integration_instance_id=inst.id,
                          workload=body.workload, ownership_type="organization",
                          owner_user_id=None, source_key=body.source_key.strip(),
                          name=body.name.strip() or body.workload, state="planned",
                          assigned_node_id=inst.node_id)
    db.add(src)
    db.flush()
    for uid in body.custodian_user_ids:
        member = db.get(User, uid)
        if member and member.tenant_id == principal.tenant_id:
            db.add(m.SourceAssignment(tenant_id=principal.tenant_id, managed_source_id=src.id,
                                      assignee_type="user", assignee_id=uid, role="custodian"))
    db.commit()
    audit.record(db, actor=principal.user_id, action="m365.org_source_created",
                 category="admin", resource=src.id, detail={"workload": body.workload})
    return {"id": src.id, "state": src.state}
