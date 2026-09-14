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
                    IntegrationInstance.integration_type == INTEGRATION_TYPE)
            .order_by(IntegrationInstance.created_at.desc()).first())


def _resolve(db: Session, tenant_id: str, instance_id: str = "") -> IntegrationInstance | None:
    """The M365 integration is multi-instance. Resolve the caller's target: an
    explicit ``instance_id`` (tenant-scoped) when given, else the most recent one
    for back-compat. Every workspace call passes an explicit id."""
    q = db.query(IntegrationInstance).filter(
        IntegrationInstance.tenant_id == tenant_id,
        IntegrationInstance.integration_type == INTEGRATION_TYPE)
    if instance_id:
        return q.filter(IntegrationInstance.id == instance_id).first()
    return q.order_by(IntegrationInstance.created_at.desc()).first()


def _node_hosted(db: Session, tenant_id: str) -> bool:
    """True when the tenant is assigned to a customer node — its M365 discovery,
    collection and storage run THERE (this CP only owns config/UI)."""
    t = db.get(Tenant, tenant_id)
    return bool(t and t.node_id)


def _bump_desired(db: Session, inst) -> None:
    """Bump the signed desired state so the assigned node reconciles promptly."""
    ds = (db.query(m.IntegrationDesiredState)
          .filter(m.IntegrationDesiredState.integration_instance_id == inst.id).first())
    if ds is None:
        ds = m.IntegrationDesiredState(
            tenant_id=inst.tenant_id, integration_instance_id=inst.id,
            node_id=inst.node_id, version=1, desired={}, status="pending_node")
        db.add(ds)
    else:
        ds.version = int(ds.version or 0) + 1
        ds.status = "pending_node"
    db.commit()


def _credential(db: Session, inst: IntegrationInstance) -> m.ManagedCredentialRef | None:
    return (db.query(m.ManagedCredentialRef)
            .filter(m.ManagedCredentialRef.integration_instance_id == inst.id).first())


def _source_object_counts(db: Session, inst: IntegrationInstance) -> dict[str, dict]:
    """Actual protected footprint per managed source = the SearchDocuments in its
    managed Collection (the true, replicated count + byte volume) — NOT the runtime
    objects_total counter, which only counts objects collected since it was
    introduced and misses everything captured before (or after a delta advanced).
    Returns {source_id: {"objects": n, "bytes": b}}."""
    from ...models import Collection, SearchDocument
    from sqlalchemy import func
    src_by_coll: dict[str, str] = {}
    for c in db.query(Collection).filter(Collection.tenant_id == inst.tenant_id).all():
        cfg = c.config or {}
        if cfg.get("m365_instance_id") == inst.id and cfg.get("managed") and cfg.get("m365_source_id"):
            src_by_coll[c.id] = cfg["m365_source_id"]
    if not src_by_coll:
        return {}
    out: dict[str, dict] = {}
    rows = (db.query(SearchDocument.collection_id, func.count(SearchDocument.id),
                     func.coalesce(func.sum(SearchDocument.size_bytes), 0))
            .filter(SearchDocument.collection_id.in_(list(src_by_coll.keys())))
            .group_by(SearchDocument.collection_id).all())
    for coll_id, n, b in rows:
        sid = src_by_coll.get(coll_id)
        if sid:
            agg = out.setdefault(sid, {"objects": 0, "bytes": 0})
            agg["objects"] += int(n or 0)
            agg["bytes"] += int(b or 0)
    return out


def _status_view(db: Session, inst: IntegrationInstance | None) -> dict:
    if inst is None:
        return {"connected": False, "state": "not_connected"}
    cred = _credential(db, inst)
    ext_q = db.query(m.ExternalIdentity).filter(
        m.ExternalIdentity.integration_instance_id == inst.id)
    mapped = db.query(m.ExternalIdentityBinding).filter(
        m.ExternalIdentityBinding.integration_instance_id == inst.id,
        m.ExternalIdentityBinding.status == "mapped").count()
    suggested = db.query(m.ExternalIdentityBinding).filter(
        m.ExternalIdentityBinding.integration_instance_id == inst.id,
        m.ExternalIdentityBinding.status == "suggested").count()
    sources = db.query(m.ManagedSource).filter(
        m.ManagedSource.integration_instance_id == inst.id).count()
    protected_objects = sum(v["objects"] for v in _source_object_counts(db, inst).values())
    cfg = inst.config or {}
    cmeta = (cred.meta if cred else {}) or {}
    consent_state = (cred.consent_state if cred else "pending")
    # Consent is recorded but the app-only token lacks the Application roles, so
    # collection/discovery 403s until an admin (re-)grants consent for the perms.
    needs_consent = bool(consent_state == "granted" and cmeta.get("needs_consent"))
    return {
        "connected": True,
        "instance_id": inst.id,
        "state": inst.provision_state or inst.status,
        "status": inst.status,
        "assigned_node_id": inst.node_id,
        "microsoft_tenant_id": (cred.microsoft_tenant_id if cred else ""),
        "consent_state": consent_state,
        "scopes_granted": (cred.scopes_granted if cred else []),
        "identities_discovered": ext_q.count(),
        "identities_mapped": mapped,
        "identities_suggested": suggested,
        "managed_sources": sources,
        "protected_objects": protected_objects,
        "auto_map": bool(cfg.get("auto_map")),
        "auto_create": bool(cfg.get("auto_create")),
        "collect_enabled": bool(cfg.get("collect_enabled")),
        "last_run_at": inst.last_run_at.isoformat() if inst.last_run_at else None,
        "last_error": inst.last_error or None,
        "permissions_ok": bool(cmeta.get("permissions_ok")) if cmeta.get("permissions_ok") is not None or needs_consent else None,
        "needs_consent": needs_consent,
        "last_checked_at": cmeta.get("last_checked_at"),
    }


@router.get("")
def status(instance_id: str = "",
           principal: security.Principal = Depends(require_m365),
           db: Session = Depends(get_db)):
    """Connection state, consent, discovery/mapping counts for one instance."""
    return _status_view(db, _resolve(db, principal.tenant_id, instance_id))


@router.get("/instances")
def list_instances(principal: security.Principal = Depends(require_m365),
                   db: Session = Depends(get_db)):
    """All Microsoft 365 instances for this org, in the shared integration-card
    shape. Served from the control plane (authoritative for managed integrations),
    so a just-created instance shows in the portal pane immediately — the generic
    /api/integrations list is proxied to the node and can lag behind replication."""
    from ...api.integrations import _instance_view  # lazy: avoid import cycle
    rows = (db.query(IntegrationInstance)
            .filter(IntegrationInstance.tenant_id == principal.tenant_id,
                    IntegrationInstance.integration_type == INTEGRATION_TYPE)
            .order_by(IntegrationInstance.created_at.desc()).all())
    # Overlay the managed-integration status so the card shows live M365 data
    # (consent + identities + sources), not the generic UniFi client/app stats.
    out = []
    for i in rows:
        view = _instance_view(i)
        sv = _status_view(db, i)
        view["m365"] = {
            "consent_state": sv.get("consent_state"),
            "needs_consent": sv.get("needs_consent"),
            "identities_discovered": sv.get("identities_discovered"),
            "identities_mapped": sv.get("identities_mapped"),
            "managed_sources": sv.get("managed_sources"),
            "protected_objects": sv.get("protected_objects"),
            "collect_enabled": sv.get("collect_enabled"),
        }
        out.append(view)
    return {"instances": out}


# --------------------------------------------------------------------------- #
# Connect + OAuth admin consent                                               #
# --------------------------------------------------------------------------- #
class ConnectBody(BaseModel):
    node_id: str | None = None           # assigned customer node (defaults to routing)
    capabilities: list[str] = []         # bundles to request consent for
    label: str | None = None             # friendly name (multiple instances allowed)


@router.post("/connect")
def connect(body: ConnectBody,
            principal: security.Principal = Depends(require_m365),
            db: Session = Depends(get_db)):
    """Create a NEW Microsoft 365 integration instance and its managed-credential
    reference, ready for admin consent. Always creates a fresh instance — an org
    may connect several Microsoft tenants."""
    tenant = db.get(Tenant, principal.tenant_id)
    # Assign to the tenant's node (if federated) so discovery/collection run
    # THERE; a CP-hosted tenant has node_id NULL and runs on the control plane.
    node_id = body.node_id or (tenant.node_id if tenant else None)
    # Number existing instances so the default label is unique-ish.
    existing = (db.query(IntegrationInstance)
                .filter(IntegrationInstance.tenant_id == principal.tenant_id,
                        IntegrationInstance.integration_type == INTEGRATION_TYPE).count())
    label = (body.label or "").strip() or (
        "Microsoft 365" if existing == 0 else f"Microsoft 365 #{existing + 1}")
    inst = IntegrationInstance(
        tenant_id=principal.tenant_id, owner_user_id=None,
        integration_type=INTEGRATION_TYPE, label=label,
        runs_on="node", node_id=node_id, enabled=True,
        status="pending", provision_state="starting",
        provision_message="Awaiting Microsoft administrator consent",
        config={"capabilities": body.capabilities})
    db.add(inst)
    db.flush()
    cred = m.ManagedCredentialRef(
        tenant_id=principal.tenant_id, integration_instance_id=inst.id,
        provider="microsoft", auth_model="oauth_admin_consent",
        consent_state="pending", assigned_node_id=inst.node_id,
        scopes_granted=[])
    db.add(cred)
    db.commit()
    audit.record(db, actor=principal.user_id, action="m365.connect_started",
                 category="admin", resource=inst.id,
                 detail={"capabilities": body.capabilities, "label": label})
    logger.info("m365 connect: created instance=%s label=%s node=%s (tenant=%s)",
                inst.id, label, node_id or "control-plane", principal.tenant_id)
    return {"ok": True, **_status_view(db, inst)}


@router.post("/oauth/start")
def oauth_start(instance_id: str = "",
                principal: security.Principal = Depends(require_m365),
                db: Session = Depends(get_db)):
    """Return the Microsoft admin-consent URL + a signed state to begin consent.
    Uses the Arkive-published Entra application (client_id from the linked Config
    Object). Microsoft redirects the browser to our public callback, which records
    consent — the reusable secret is never held in the control plane per se; it is
    used app-only on the box that collects."""
    inst = _resolve(db, principal.tenant_id, instance_id)
    if inst is None:
        raise HTTPException(409, "connect first")
    from ... import platform_config
    vals = platform_config.integration_values(INTEGRATION_TYPE)
    client_id = (vals.get("client_id") or "").strip()
    if not client_id:
        logger.warning("m365 oauth start: no client_id configured (integration_values "
                       "for %s is empty) — cannot begin admin consent", INTEGRATION_TYPE)
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
    else:
        # No credential row to stamp the state on — the redirect can never
        # correlate consent back. Recreate it rather than fail silently.
        logger.warning("m365 oauth start: instance=%s had no credential row — "
                       "recreating so consent can be correlated", inst.id)
        cred = m.ManagedCredentialRef(
            tenant_id=inst.tenant_id, integration_instance_id=inst.id,
            provider="microsoft", auth_model="oauth_admin_consent",
            consent_state="pending", assigned_node_id=inst.node_id,
            scopes_granted=[], meta={"oauth_state": state})
        db.add(cred)
        db.commit()
    redirect = (vals.get("redirect_uri") or "").strip() or _default_redirect()
    consent_url = ("https://login.microsoftonline.com/organizations/v2.0/adminconsent?"
                   + urlencode({"client_id": client_id, "state": state,
                                "scope": "https://graph.microsoft.com/.default",
                                "redirect_uri": redirect}))
    logger.info("m365 oauth start: instance=%s state=%s redirect_uri=%s",
                inst.id, state[:8], redirect)
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
    logger.info("m365 oauth redirect received: state=%s admin_consent=%s tenant=%s error=%s",
                (state or "")[:8] or "(none)", admin_consent or "(none)",
                (tenant or "")[:12] or "(none)", error or "(none)")
    if not state:
        logger.warning("m365 oauth redirect: no state parameter — cannot correlate consent")
        return RedirectResponse(portal + "?m365=error", status_code=302)
    cred = None
    # Match by the unique one-time state token, regardless of current consent
    # state — a RE-authorize (adding permissions) happens on an already-"granted"
    # credential, so filtering to non-granted rows would drop it and never record
    # the new consent.
    candidates = (db.query(m.ManagedCredentialRef)
                  .filter(m.ManagedCredentialRef.provider == "microsoft").all())
    for c in candidates:
        if (c.meta or {}).get("oauth_state") == state:
            cred = c
            break
    if cred is None:
        logger.warning("m365 oauth redirect: no credential matched state=%s "
                       "(%d candidate(s) checked) — consent NOT recorded",
                       state[:8], len(candidates))
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
def oauth_callback(body: ConsentCallback, instance_id: str = "",
                   principal: security.Principal = Depends(require_m365),
                   db: Session = Depends(get_db)):
    """Record a completed admin-consent. Validates the signed state bound to this
    org's credential reference (fail closed on mismatch)."""
    inst = _resolve(db, principal.tenant_id, instance_id)
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
def get_scope(instance_id: str = "",
              principal: security.Principal = Depends(require_m365),
              db: Session = Depends(get_db)):
    inst = _resolve(db, principal.tenant_id, instance_id)
    if inst is None:
        raise HTTPException(409, "connect first")
    sp = (db.query(m.IdentityScopePolicy)
          .filter(m.IdentityScopePolicy.integration_instance_id == inst.id).first())
    return {"rules": (sp.rules if sp else {}), "version": (sp.version if sp else 0)}


@router.put("/scope")
def set_scope(body: ScopeBody, instance_id: str = "",
              principal: security.Principal = Depends(require_m365),
              db: Session = Depends(get_db)):
    inst = _resolve(db, principal.tenant_id, instance_id)
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
# Automation settings (auto-map / auto-create)                                #
# --------------------------------------------------------------------------- #
class SettingsBody(BaseModel):
    auto_map: bool | None = None      # auto-accept suggested matches on discovery
    auto_create: bool | None = None   # auto-create Arkive members for unmatched users


@router.get("/settings")
def m365_get_settings(instance_id: str = "",
                      principal: security.Principal = Depends(require_m365),
                      db: Session = Depends(get_db)):
    inst = _resolve(db, principal.tenant_id, instance_id)
    if inst is None:
        raise HTTPException(409, "connect first")
    cfg = inst.config or {}
    return {"auto_map": bool(cfg.get("auto_map")), "auto_create": bool(cfg.get("auto_create"))}


@router.put("/settings")
def m365_set_settings(body: SettingsBody, instance_id: str = "",
                      principal: security.Principal = Depends(require_m365),
                      db: Session = Depends(get_db)):
    inst = _resolve(db, principal.tenant_id, instance_id)
    if inst is None:
        raise HTTPException(409, "connect first")
    cfg = dict(inst.config or {})
    if body.auto_map is not None:
        cfg["auto_map"] = bool(body.auto_map)
    if body.auto_create is not None:
        cfg["auto_create"] = bool(body.auto_create)
    inst.config = cfg
    if _node_hosted(db, principal.tenant_id):
        _bump_desired(db, inst)   # node applies the new automation on its next reconcile
    db.commit()
    audit.record(db, actor=principal.user_id, action="m365.settings_updated",
                 category="admin", resource=inst.id,
                 detail={"auto_map": cfg.get("auto_map"), "auto_create": cfg.get("auto_create")})
    logger.info("m365 settings (instance=%s): auto_map=%s auto_create=%s",
                inst.id, cfg.get("auto_map"), cfg.get("auto_create"))
    return {"auto_map": bool(cfg.get("auto_map")), "auto_create": bool(cfg.get("auto_create"))}


# --------------------------------------------------------------------------- #
# Managed protection profile (workloads / destinations / schedule)            #
# --------------------------------------------------------------------------- #
class ProfileBody(BaseModel):
    workloads: list[str] | None = None                 # exchange | onedrive | sharepoint | teams | teams_chat
    destinations: list[str] | None = None              # cv-cloud | store:<id> | byos:<id> | ...
    backup_interval_minutes: int | None = None         # <0 clears (use default cadence)


_WORKLOAD_CATALOG = [
    {"id": "exchange", "label": "Exchange Online", "scope": "user",
     "description": "Each protected user's mailbox"},
    {"id": "onedrive", "label": "OneDrive", "scope": "user",
     "description": "Each protected user's files"},
    {"id": "sharepoint", "label": "SharePoint", "scope": "org",
     "description": "Organization SharePoint site document libraries"},
    {"id": "teams", "label": "Teams channels", "scope": "org",
     "description": "Organization Teams channel conversations"},
    {"id": "teams_chat", "label": "Teams chats", "scope": "user",
     "description": "Each protected user's 1:1 and group chats"},
]
_VALID_WORKLOADS = {w["id"] for w in _WORKLOAD_CATALOG}


@router.get("/profile")
def m365_get_profile(instance_id: str = "",
                     principal: security.Principal = Depends(require_m365),
                     db: Session = Depends(get_db)):
    """The org's managed-protection profile + the workload catalog + how many
    users/sources it currently protects."""
    inst = _resolve(db, principal.tenant_id, instance_id)
    if inst is None:
        raise HTTPException(409, "connect first")
    from . import collect
    prof = collect.profile(inst)
    # Sources currently under protection = everything not paused/decommissioned
    # (a source that returned 0 new items on its last delta is still protected).
    protected = db.query(m.ManagedSource).filter(
        m.ManagedSource.integration_instance_id == inst.id,
        m.ManagedSource.state.notin_(("paused_by_admin", "decommissioned", "planned"))).count()
    return {**prof, "workload_catalog": _WORKLOAD_CATALOG, "active_sources": protected}


@router.put("/profile")
def m365_set_profile(body: ProfileBody, instance_id: str = "",
                     principal: security.Principal = Depends(require_m365),
                     db: Session = Depends(get_db)):
    """Update the managed-protection profile and (re)provision the per-user
    managed sources + Data Map collections to match. Runs on the control plane;
    node-hosted tenants reconcile on their node via bumped desired state."""
    inst = _resolve(db, principal.tenant_id, instance_id)
    if inst is None:
        raise HTTPException(409, "connect first")
    from . import collect
    cfg = dict(inst.config or {})
    mp = dict(cfg.get("managed_profile") or {})
    if body.workloads is not None:
        mp["workloads"] = [w for w in body.workloads if w in _VALID_WORKLOADS]
    if body.destinations is not None:
        mp["destinations"] = [str(d) for d in body.destinations if d]
    if body.backup_interval_minutes is not None:
        mp["backup_interval_minutes"] = (None if body.backup_interval_minutes < 0
                                         else int(body.backup_interval_minutes))
    cfg["managed_profile"] = mp
    inst.config = cfg
    db.commit()
    if _node_hosted(db, principal.tenant_id):
        _bump_desired(db, inst)   # node re-provisions sources/collections on reconcile
    else:
        try:
            collect.provision_sources(db, inst)
            _provision_org_now(db, inst)   # discover SharePoint/Teams now
        except Exception:  # noqa: BLE001
            logger.exception("m365 provision after profile update failed (instance=%s)", inst.id)
    prof = collect.profile(inst)
    audit.record(db, actor=principal.user_id, action="m365.profile_updated",
                 category="admin", resource=inst.id, detail=prof)
    logger.info("m365 profile (instance=%s): workloads=%s destinations=%s interval=%s",
                inst.id, prof["workloads"], prof["destinations"], prof["backup_interval_minutes"])
    return {**prof, "workload_catalog": _WORKLOAD_CATALOG}


# --------------------------------------------------------------------------- #
# Identities + mapping decisions                                              #
# --------------------------------------------------------------------------- #
@router.get("/identities")
def list_identities(state: str = "", limit: int = 500, instance_id: str = "",
                    principal: security.Principal = Depends(require_m365),
                    db: Session = Depends(get_db)):
    inst = _resolve(db, principal.tenant_id, instance_id)
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
def identity_decisions(body: IdentityDecisions, instance_id: str = "",
                       principal: security.Principal = Depends(require_m365),
                       db: Session = Depends(get_db)):
    """Apply per-identity mapping decisions (idempotent, per-item results). Mapping
    never grants portal access; account creation/invite is a separate action."""
    inst = _resolve(db, principal.tenant_id, instance_id)
    if inst is None:
        raise HTTPException(409, "connect first")
    tenant = db.get(Tenant, principal.tenant_id)
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
            # Provision a real Arkive member (member + vault + keys) for this
            # identity and map it. Runs on the control plane (vault keys live here).
            from . import provisioning
            member = provisioning.provision_member(
                db, tenant, e.email or e.upn, e.display_name) if tenant else None
            if member is None:
                results.append({"id": e.id, "ok": False, "error": "could not create member"})
                continue
            binding.user_id = member.id
            binding.protected_only = False
            binding.status = "mapped"
            binding.mapping_method = "manual_created"
            binding.approved_by = principal.user_id
            e.state = "mapped"
        else:
            results.append({"id": e.id, "ok": False, "error": "unknown action"})
            continue
        binding.updated_at = _now()
        results.append({"id": e.id, "ok": True, "state": e.state})
    db.commit()
    # Establish admin-level managed sources for the newly mapped users so protection
    # can begin (idempotent). Node-hosted tenants provision on their node (bindings
    # federate down); only the CP-hosted case provisions here.
    if not _node_hosted(db, principal.tenant_id):
        try:
            from . import collect
            collect.provision_sources(db, inst)
        except Exception:  # noqa: BLE001
            logger.exception("m365 provision_sources after mapping failed (instance=%s)", inst.id)
    else:
        _bump_desired(db, inst)
    audit.record(db, actor=principal.user_id, action="m365.identity_decisions",
                 category="admin", resource=inst.id,
                 detail={"count": len(body.decisions)})
    return {"results": results}


@router.post("/identities/accept-suggestions")
def accept_suggestions(instance_id: str = "",
                       principal: security.Principal = Depends(require_m365),
                       db: Session = Depends(get_db)):
    """Accept every auto-suggested match at once (bulk map). Only suggested
    bindings that already carry a matched user are promoted to 'mapped'."""
    inst = _resolve(db, principal.tenant_id, instance_id)
    if inst is None:
        raise HTTPException(409, "connect first")
    binds = (db.query(m.ExternalIdentityBinding)
             .filter(m.ExternalIdentityBinding.integration_instance_id == inst.id,
                     m.ExternalIdentityBinding.status == "suggested",
                     m.ExternalIdentityBinding.user_id.isnot(None)).all())
    accepted = 0
    for b in binds:
        b.status = "mapped"
        b.approved_by = principal.user_id
        b.updated_at = _now()
        e = db.get(m.ExternalIdentity, b.external_identity_id)
        if e is not None:
            e.state = "mapped"
        accepted += 1
    if not _node_hosted(db, principal.tenant_id):
        try:
            from . import collect
            collect.provision_sources(db, inst)
        except Exception:  # noqa: BLE001
            logger.exception("m365 provision_sources after accept failed (instance=%s)", inst.id)
    else:
        _bump_desired(db, inst)
    db.commit()
    audit.record(db, actor=principal.user_id, action="m365.suggestions_accepted",
                 category="admin", resource=inst.id, detail={"accepted": accepted})
    return {"accepted": accepted}


# --------------------------------------------------------------------------- #
# Activation + desired state + disconnect                                     #
# --------------------------------------------------------------------------- #
@router.post("/activate")
def activate(instance_id: str = "",
             principal: security.Principal = Depends(security.require_passkey),
             db: Session = Depends(get_db)):
    """Approve the activation plan: bump the signed desired-state the assigned node
    reconciles. Passkey step-up required. (Node applies + begins collection.)"""
    # Re-check entitlement explicitly (require_passkey doesn't imply it).
    require_m365(principal, db)
    inst = _resolve(db, principal.tenant_id, instance_id)
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
def disconnect(instance_id: str = "",
               principal: security.Principal = Depends(security.require_passkey),
               db: Session = Depends(get_db)):
    """Safely disconnect: stop future collection, retain protected data + checkpoints.
    Does NOT delete Arkive data (a purge is a separate, audited action)."""
    require_m365(principal, db)
    inst = _resolve(db, principal.tenant_id, instance_id)
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


@router.post("/remove")
def remove(instance_id: str = "",
           principal: security.Principal = Depends(require_m365),
           db: Session = Depends(get_db)):
    """Purge a Microsoft 365 integration instance — stalled, failed OR active.
    Deletes the instance and all its control-plane rows (credential, desired
    state, scope, identities, bindings, managed sources) so it disappears from the
    portal and the node stops collecting for it. Protected recovery points are
    retained under their retention policy (a data purge is a separate action)."""
    inst = _resolve(db, principal.tenant_id, instance_id)
    if inst is None:
        return {"ok": True, "note": "Nothing to remove."}
    node_hosted = _node_hosted(db, principal.tenant_id)
    sources = (db.query(m.ManagedSource)
               .filter(m.ManagedSource.integration_instance_id == inst.id).count())
    # For a node-hosted tenant, disable + bump desired state first so the node
    # tears down collection on its next reconcile, THEN delete the CP rows.
    if node_hosted:
        inst.enabled = False
        try:
            _bump_desired(db, inst)
        except Exception:  # noqa: BLE001 — never block removal on a bump failure
            logger.warning("m365 remove: desired-state bump failed for instance=%s", inst.id)
    # Delete the child rows first (no cascade guarantee across the m365 tables).
    for model in (m.ManagedSource, m.ExternalIdentityBinding, m.ExternalIdentity,
                  m.IdentityScopePolicy, m.IntegrationDesiredState, m.ManagedCredentialRef):
        db.query(model).filter(
            model.integration_instance_id == inst.id).delete(synchronize_session=False)
    inst_id = inst.id
    db.delete(inst)
    db.commit()
    audit.record(db, actor=principal.user_id, action="m365.removed",
                 category="admin", severity="warning", resource=inst_id,
                 detail={"managed_sources": sources, "node_hosted": node_hosted})
    logger.info("m365 remove: purged instance=%s (%d managed source(s), node_hosted=%s, tenant=%s)",
                inst_id, sources, node_hosted, principal.tenant_id)
    return {"ok": True, "note": "Removed. Any protected recovery points are retained "
                                "under your retention policy."}



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
def discover(instance_id: str = "",
             principal: security.Principal = Depends(require_m365),
             db: Session = Depends(get_db)):
    """Run Entra identity discovery now (synchronous). Requires granted consent and
    the platform Microsoft 365 app credentials (admin Config)."""
    inst = _resolve(db, principal.tenant_id, instance_id)
    if inst is None:
        raise HTTPException(409, "connect first")
    cred = _credential(db, inst)
    if not cred or cred.consent_state != "granted":
        raise HTTPException(409, "Microsoft administrator consent is required first")
    # Node-hosted tenants discover on their assigned node (polling + storage live
    # there). Bump the desired state so the node reconciles promptly.
    if _node_hosted(db, principal.tenant_id):
        _bump_desired(db, inst)
        audit.record(db, actor=principal.user_id, action="m365.discovery_queued",
                     category="admin", resource=inst.id)
        return {"ok": True, "queued": True, **_status_view(db, inst),
                "note": "Discovery runs on your assigned node; results appear shortly."}
    from ... import platform_config
    from . import discovery
    vals = platform_config.integration_values(INTEGRATION_TYPE)
    res = discovery.run_discovery(db, inst,
                                  client_id=(vals.get("client_id") or "").strip(),
                                  client_secret=(vals.get("client_secret") or "").strip(),
                                  can_provision=True)
    audit.record(db, actor=principal.user_id, action="m365.discovery_run",
                 category="admin", resource=inst.id,
                 detail={"ok": res.get("ok"), "discovered": res.get("discovered"),
                         "error": res.get("error")})
    if not res.get("ok"):
        raise HTTPException(400, res.get("error") or "discovery failed")
    return {**res, **_status_view(db, inst)}


@router.get("/sources")
def list_managed_sources(instance_id: str = "",
                         principal: security.Principal = Depends(require_m365),
                         db: Session = Depends(get_db)):
    """The admin-established managed sources (Exchange / OneDrive / SharePoint /
    Teams / Teams chats) with per-source status + object counts, plus a per-
    workload rollup — so the integration shows exactly what's protected and how
    much, like any standard source."""
    inst = _resolve(db, principal.tenant_id, instance_id)
    if inst is None:
        raise HTTPException(409, "connect first")
    rows = (db.query(m.ManagedSource)
            .filter(m.ManagedSource.integration_instance_id == inst.id)
            .order_by(m.ManagedSource.workload.asc(), m.ManagedSource.name.asc()).all())
    obj_counts = _source_object_counts(db, inst)  # actual protected objects/bytes per source
    _WL_LABEL = {"exchange": "Exchange Online", "onedrive": "OneDrive",
                 "sharepoint": "SharePoint", "teams": "Teams channels",
                 "teams_chat": "Teams chats"}
    sources = []
    rollup: dict[str, dict] = {}
    for s in rows:
        cfg = s.config or {}
        agg = obj_counts.get(s.id) or {"objects": 0, "bytes": 0}
        objects = agg["objects"]
        vbytes = agg["bytes"]
        last = cfg.get("last_result") or {}
        # A source with protected objects is active regardless of the last delta
        # (an incremental run that returns 0 changes must not read as "empty").
        eff_state = "active" if (objects > 0 and s.state == "empty") else s.state
        sources.append({
            "id": s.id, "workload": s.workload, "name": s.name,
            "ownership_type": s.ownership_type, "owner_user_id": s.owner_user_id,
            "state": eff_state, "source_key": s.source_key,
            "objects": objects, "bytes": vbytes,
            "last_collected_at": s.last_collected_at.isoformat() if s.last_collected_at else None,
            "last_error": last.get("error"),
        })
        r = rollup.setdefault(s.workload, {
            "workload": s.workload, "label": _WL_LABEL.get(s.workload, s.workload),
            "sources": 0, "active": 0, "objects": 0, "bytes": 0, "errors": 0})
        r["sources"] += 1
        r["objects"] += objects
        r["bytes"] += vbytes
        if eff_state == "active":
            r["active"] += 1
        if eff_state in ("permission_required", "credential_error", "delayed"):
            r["errors"] += 1
    return {"collect_enabled": bool((inst.config or {}).get("collect_enabled")),
            "total_objects": sum(x["objects"] for x in rollup.values()),
            "total_bytes": sum(x["bytes"] for x in rollup.values()),
            "workloads": list(rollup.values()),
            "sources": sources}


@router.get("/compliance-rules")
def compliance_rules(instance_id: str = "",
                     principal: security.Principal = Depends(require_m365),
                     db: Session = Depends(get_db)):
    """Compliance (rules-engine) rules that apply to THIS integration's managed
    collections, so the admin can see + manage governance from the workspace.
    Managed M365 sources are real Data Map Collections, so they use the main
    rules engine (bind by collection id / source type) — not a separate system."""
    from ...models import Collection, Rule
    from ... import features
    inst = _resolve(db, principal.tenant_id, instance_id)
    if inst is None:
        raise HTTPException(409, "connect first")
    user = db.get(User, principal.user_id)
    tenant = db.get(Tenant, principal.tenant_id)
    if not features.resolve(user, tenant, "rules_enabled"):
        return {"enabled": False, "managed_collections": [], "rules": []}
    # Managed collections for this instance (Collection.config.m365_instance_id).
    colls = [c for c in db.query(Collection).filter(Collection.tenant_id == tenant.id).all()
             if (c.config or {}).get("m365_instance_id") == inst.id]
    coll_ids = {c.id for c in colls}
    coll_stypes = {c.source_type for c in colls}
    rules = (db.query(Rule)
             .filter(Rule.tenant_id == tenant.id)
             .order_by(Rule.priority.asc(), Rule.created_at.asc()).all())
    applies = []
    for r in rules:
        cids = set(r.collection_ids or [])
        stypes = set(r.source_types or [])
        # A rule applies to a managed source when it's unscoped, or targets one of
        # these collections, or targets one of their source types.
        if (not cids and not stypes) or (cids & coll_ids) or (stypes & coll_stypes):
            applies.append({"id": r.id, "name": r.name, "enabled": bool(r.enabled),
                            "priority": r.priority, "actions": r.actions or [],
                            "scoped": bool(cids or stypes)})
    return {
        "enabled": True,
        "managed_collections": [{"id": c.id, "name": c.name, "source_type": c.source_type,
                                 "workload": (c.config or {}).get("m365_workload") or ""}
                                for c in colls],
        "rules": applies,
    }


class CollectionToggle(BaseModel):
    enabled: bool


@router.post("/collection")
def set_collection(body: CollectionToggle, instance_id: str = "",
                   principal: security.Principal = Depends(security.require_passkey),
                   db: Session = Depends(get_db)):
    """Enable/disable admin-level content protection (Exchange + OneDrive) for the
    mapped users. Passkey step-up required. Provisions sources on enable."""
    require_m365(principal, db)
    inst = _resolve(db, principal.tenant_id, instance_id)
    if inst is None:
        raise HTTPException(409, "connect first")
    cred = _credential(db, inst)
    if body.enabled and (not cred or cred.consent_state != "granted"):
        raise HTTPException(409, "Microsoft administrator consent is required first")
    cfg = dict(inst.config or {})
    cfg["collect_enabled"] = bool(body.enabled)
    inst.config = cfg
    created = 0
    if body.enabled and not _node_hosted(db, principal.tenant_id):
        from . import collect
        created = collect.provision_sources(db, inst)
        _provision_org_now(db, inst)   # SharePoint/Teams discovery so they appear now
    if _node_hosted(db, principal.tenant_id):
        _bump_desired(db, inst)
    db.commit()
    audit.record(db, actor=principal.user_id, action="m365.collection_toggled",
                 category="admin", severity="notice", resource=inst.id,
                 detail={"enabled": body.enabled, "sources_provisioned": created})
    return {"ok": True, "collect_enabled": body.enabled, "sources_provisioned": created}


def _app_token(db: Session, inst) -> str:
    """Mint a fresh app-only Graph token for this instance, or "" if unavailable."""
    from ... import platform_config
    from . import graph
    vals = platform_config.integration_values(INTEGRATION_TYPE)
    cid = (vals.get("client_id") or "").strip()
    csec = (vals.get("client_secret") or "").strip()
    cred = _credential(db, inst)
    if not cid or not csec or not cred or cred.consent_state != "granted" or not cred.microsoft_tenant_id:
        return ""
    try:
        return graph.app_token(cid, csec, cred.microsoft_tenant_id, force=True)
    except Exception:  # noqa: BLE001
        logger.exception("m365 token mint failed (instance=%s)", inst.id)
        return ""


def _provision_org_now(db: Session, inst) -> None:
    """CP-hosted: discover org resources (SharePoint sites / Teams) immediately so
    they appear in the managed-sources table without waiting for the worker."""
    from . import collect
    token = _app_token(db, inst)
    if not token:
        return
    try:
        collect.provision_org_sources(db, inst, token)
    except Exception:  # noqa: BLE001
        logger.exception("m365 org provision-now failed (instance=%s)", inst.id)


@router.post("/collect-now")
def collect_now(instance_id: str = "",
                principal: security.Principal = Depends(require_m365),
                db: Session = Depends(get_db)):
    """Back up now — trigger an immediate managed backup through the SAME job
    pipeline as every other source. Each managed source's Collection gets a tracked
    SyncJob (visible in Activity); node-hosted tenants run the jobs on their node."""
    from ...models import Collection
    from ...workers.jobs import start_backup_job
    inst = _resolve(db, principal.tenant_id, instance_id)
    if inst is None:
        raise HTTPException(409, "connect first")
    cred = _credential(db, inst)
    if not cred or cred.consent_state != "granted":
        raise HTTPException(409, "Microsoft administrator consent is required first")

    node_hosted = _node_hosted(db, principal.tenant_id)
    if node_hosted:
        # Nudge the node to (re)provision + let its scheduler run the managed
        # collections immediately (the node worker resets their due time on a
        # desired-state bump). Any managed collections the CP already knows about
        # are also queued for the node so the run shows up right away.
        _bump_desired(db, inst)
    else:
        # CP-hosted: provision now so SharePoint/Teams collections exist, then queue.
        from . import collect
        token = _app_token(db, inst)
        if token:
            try:
                collect.provision_sources(db, inst)
                collect.provision_org_sources(db, inst, token)
            except Exception:  # noqa: BLE001
                logger.exception("m365 collect-now provision failed (instance=%s)", inst.id)

    colls = [c for c in db.query(Collection)
             .filter(Collection.tenant_id == inst.tenant_id).all()
             if (c.config or {}).get("m365_instance_id") == inst.id
             and (c.config or {}).get("managed")]
    queued = 0
    for c in colls:
        try:
            start_backup_job(db, inst.tenant_id, c.id, kind="backup")
            queued += 1
        except Exception:  # noqa: BLE001
            logger.exception("m365 collect-now enqueue failed (collection=%s)", c.id)
    db.commit()
    audit.record(db, actor=principal.user_id, action="m365.collect_now",
                 category="admin", resource=inst.id,
                 detail={"queued": queued, "node_hosted": node_hosted})
    note = ("Backup queued on your assigned node — sources update shortly."
            if node_hosted else
            f"Backup started for {queued} managed source(s).")
    return {"ok": True, "queued": queued, "note": note}


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
