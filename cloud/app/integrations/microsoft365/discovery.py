"""Microsoft 365 — Entra identity discovery (node-side collector, phase 2).

Discovers the organization's Entra ID users into ``ExternalIdentity`` rows so an
identity-admin can map them to Arkive users. Runs where the integration instance
lives: on the control plane for CP-hosted tenants, or on the assigned customer
node for federated tenants (discovered identities then replicate up to the CP for
the portal). Content collection (Exchange/OneDrive) is a later phase.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import graph
from . import models as m

logger = logging.getLogger("cv.integrations.m365.discovery")

INTEGRATION_TYPE = "microsoft365"
_SELECT = "id,displayName,userPrincipalName,mail,accountEnabled,userType"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _friendly_graph_error(e: "graph.GraphError") -> str:
    """Turn a raw Graph error into admin-actionable remediation text."""
    reason = (e.reason or "").strip()
    if e.status == 403 or reason in ("Authorization_RequestDenied", "Authorization_IdentityNotFound"):
        return ("The Arkive Microsoft 365 app is missing directory permissions. In the Azure "
                "portal → App registrations → (the Arkive app) → API permissions → Add a permission "
                "→ Microsoft Graph → APPLICATION permissions, add User.Read.All (and Mail.Read, "
                "Files.Read.All for content backup; Sites.Read.All for SharePoint; "
                "ChannelMessage.Read.All + Chat.Read.All for Teams — under the ChannelMessage and Chat "
                "groups; AiEnterpriseInteraction.Read.All for Microsoft 365 Copilot), then click "
                "'Grant admin consent'. Note: app-only reading of Teams channel/chat "
                "messages is a Microsoft 'protected API' — it also requires completing Microsoft's "
                "'Request access to protected APIs' process, or Teams stays 403 even after consent. "
                "Re-run discovery afterwards.")
    if e.status == 401 or reason == "InvalidAuthenticationToken":
        return ("Microsoft rejected the app credentials. Re-check the client id/secret linked in "
                "Admin → Integrations, then reconnect and grant admin consent again.")
    if reason == "quotaExceeded" or e.status == 429:
        return "Microsoft is throttling requests (quota/429). Wait a few minutes and retry."
    return f"graph: {reason or e}"


def _in_scope(ident: "m.ExternalIdentity", rules: dict) -> tuple[bool, str]:
    """Deterministic scope decision. Precedence: explicit exclude > guest gate >
    explicit include list > domain allow-list > default (enabled members)."""
    rules = rules or {}
    upn = (ident.upn or ident.email or "").lower()
    oid = (ident.entra_object_id or "").lower()
    excludes = {str(x).lower() for x in (rules.get("excludes") or [])}
    if upn in excludes or oid in excludes:
        return False, "excluded"
    if (ident.user_type or "member").lower() == "guest" and not rules.get("include_guests"):
        return False, "guest"
    includes = [str(x).lower() for x in (rules.get("includes") or [])]
    if includes:
        return (upn in includes or oid in includes,
                "included" if (upn in includes or oid in includes) else "not_in_include_list")
    domains = [str(d).lower().lstrip("@") for d in (rules.get("domains") or [])]
    if domains:
        dom = upn.split("@")[-1] if "@" in upn else ""
        return (dom in domains, "domain_match" if dom in domains else "domain_excluded")
    return (bool(ident.account_enabled), "default" if ident.account_enabled else "account_disabled")


def run_discovery(db: Session, inst, *, client_id: str, client_secret: str,
                  can_provision: bool = False) -> dict:
    """Discover Entra users for one connected instance into ExternalIdentity.

    Returns a result dict {ok, discovered, in_scope, error?, http?}. Never raises —
    the worker records the outcome. Requires granted admin consent + the platform
    Entra app credentials (from the linked ConfigObject). ``can_provision`` allows
    auto-creating Arkive members (control plane only)."""
    cred = (db.query(m.ManagedCredentialRef)
            .filter(m.ManagedCredentialRef.integration_instance_id == inst.id).first())
    if cred is None or cred.consent_state != "granted" or not cred.microsoft_tenant_id:
        return {"ok": False, "error": "microsoft administrator consent required"}
    if not client_id or not client_secret:
        return {"ok": False, "error": "platform microsoft 365 app is not configured"}
    try:
        # force=True: never reuse a token minted before consent took effect.
        token = graph.app_token(client_id, client_secret, cred.microsoft_tenant_id, force=True)
    except graph.GraphError as e:
        logger.warning("m365 token failed (instance=%s tenant=%s): %s",
                       inst.id, cred.microsoft_tenant_id, e)
        return {"ok": False, "error": _friendly_graph_error(e), "http": e.status}

    # Breadcrumb (no secret): which app + tenant + granted roles this run uses, so
    # a 403 is triageable even without decoding — the roles claim is decisive.
    _claims = graph.token_claims(token)
    logger.info("m365 discovery start (instance=%s): app appid=%s token_tid=%s "
                "connected_tenant=%s client_id=…%s roles=%s",
                inst.id, _claims.get("appid") or _claims.get("azp") or "?",
                _claims.get("tid") or "?", cred.microsoft_tenant_id,
                (client_id or "")[-6:], _claims.get("roles") or "[]")

    scope = (db.query(m.IdentityScopePolicy)
             .filter(m.IdentityScopePolicy.integration_instance_id == inst.id).first())
    rules = (scope.rules if scope else {}) or {}
    discovered = 0
    in_scope_n = 0
    try:
        for u in graph.get_paged(token, "/users",
                                 params={"$select": _SELECT, "$top": "999"}):
            oid = u.get("id") or ""
            if not oid:
                continue
            ident = (db.query(m.ExternalIdentity)
                     .filter(m.ExternalIdentity.microsoft_tenant_id == cred.microsoft_tenant_id,
                             m.ExternalIdentity.entra_object_id == oid).first())
            if ident is None:
                ident = m.ExternalIdentity(
                    tenant_id=inst.tenant_id, integration_instance_id=inst.id,
                    microsoft_tenant_id=cred.microsoft_tenant_id, entra_object_id=oid,
                    state="discovered")
                db.add(ident)
            ident.integration_instance_id = inst.id
            ident.tenant_id = inst.tenant_id
            ident.upn = u.get("userPrincipalName") or ""
            ident.email = u.get("mail") or ident.upn
            ident.display_name = u.get("displayName") or ident.upn or oid
            ident.account_enabled = bool(u.get("accountEnabled", True))
            ident.user_type = (u.get("userType") or "member").lower()
            ident.last_seen = _now()
            ins, reason = _in_scope(ident, rules)
            ident.in_scope = ins
            ident.scope_reason = reason
            discovered += 1
            if ins:
                in_scope_n += 1
        db.commit()
    except graph.GraphError as e:
        db.rollback()
        msg = _friendly_graph_error(e)
        if e.status == 403:
            # Decode the app-only token to pinpoint WHY: no roles = consent not
            # effective for this app in this tenant; tid mismatch = wrong tenant.
            claims = graph.token_claims(token)
            roles = claims.get("roles") or []
            tid = claims.get("tid") or ""
            appid = claims.get("appid") or claims.get("azp") or ""
            logger.warning("m365 discovery 403 (instance=%s): app appid=%s token_tid=%s "
                           "connected_tenant=%s roles=%s",
                           inst.id, appid, tid, cred.microsoft_tenant_id, roles or "[]")
            if not roles:
                msg = (f"The app-only token carries NO Graph roles, so admin consent for the "
                       f"Application permissions (User.Read.All …) has not taken effect for this "
                       f"app (client {appid or 'unknown'}) in tenant {tid or cred.microsoft_tenant_id}. "
                       f"In Azure → this app → API permissions, confirm each Application permission "
                       f"shows 'Granted for <org>' (green check), then wait a few minutes and retry.")
            elif tid and cred.microsoft_tenant_id and tid.lower() != cred.microsoft_tenant_id.lower():
                msg = (f"The token was issued for Microsoft tenant {tid}, but this connection "
                       f"targets {cred.microsoft_tenant_id}. The connected tenant id is wrong — "
                       f"reconnect Microsoft 365 and confirm the Directory (tenant) ID.")
        else:
            logger.warning("m365 discovery failed (instance=%s): %s", inst.id, e)
        inst.last_error = msg
        meta = dict(cred.meta or {})
        meta["last_checked_at"] = _now().isoformat()
        if e.status == 403 and not (graph.token_claims(token).get("roles") or []):
            # Consent recorded but the Application permissions never took effect —
            # the admin must (re-)grant consent for the app roles.
            meta["permissions_ok"] = False
            meta["needs_consent"] = True
        cred.meta = meta
        db.commit()
        return {"ok": False, "error": msg, "http": e.status}
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.exception("m365 discovery crashed (instance=%s)", inst.id)
        return {"ok": False, "error": str(e)[:200]}

    inst.last_run_at = _now()
    inst.last_success_at = _now()
    inst.last_error = None
    inst.last_stats = {"identities": discovered, "in_scope": in_scope_n}
    # Permissions verified working — clear any stale "needs consent" flag.
    cred.meta = {**(cred.meta or {}), "permissions_ok": True,
                 "needs_consent": False, "last_checked_at": _now().isoformat()}
    db.commit()
    # Auto-suggest/map/create bindings for the in-scope identities.
    from . import provisioning
    recon = provisioning.reconcile_bindings(db, inst, can_provision=can_provision)
    logger.info("m365 discovery ok (instance=%s): %d identities, %d in scope "
                "(suggested=%d mapped=%d created=%d)",
                inst.id, discovered, in_scope_n, recon["suggested"], recon["mapped"],
                recon["created"])
    return {"ok": True, "discovered": discovered, "in_scope": in_scope_n, **recon}
