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
                "portal → App registrations → (the Arkive app) → API permissions, add the "
                "APPLICATION permissions User.Read.All (and Mail.Read, Files.Read.All for content "
                "backup), then click 'Grant admin consent'. Re-run discovery afterwards.")
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


def run_discovery(db: Session, inst, *, client_id: str, client_secret: str) -> dict:
    """Discover Entra users for one connected instance into ExternalIdentity.

    Returns a result dict {ok, discovered, in_scope, error?, http?}. Never raises —
    the worker records the outcome. Requires granted admin consent + the platform
    Entra app credentials (from the linked ConfigObject)."""
    cred = (db.query(m.ManagedCredentialRef)
            .filter(m.ManagedCredentialRef.integration_instance_id == inst.id).first())
    if cred is None or cred.consent_state != "granted" or not cred.microsoft_tenant_id:
        return {"ok": False, "error": "microsoft administrator consent required"}
    if not client_id or not client_secret:
        return {"ok": False, "error": "platform microsoft 365 app is not configured"}
    try:
        token = graph.app_token(client_id, client_secret, cred.microsoft_tenant_id)
    except graph.GraphError as e:
        logger.warning("m365 token failed (instance=%s tenant=%s): %s",
                       inst.id, cred.microsoft_tenant_id, e)
        return {"ok": False, "error": _friendly_graph_error(e), "http": e.status}

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
        logger.warning("m365 discovery failed (instance=%s): %s", inst.id, e)
        inst.last_error = _friendly_graph_error(e)
        db.commit()
        return {"ok": False, "error": _friendly_graph_error(e), "http": e.status}
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.exception("m365 discovery crashed (instance=%s)", inst.id)
        return {"ok": False, "error": str(e)[:200]}

    inst.last_run_at = _now()
    inst.last_success_at = _now()
    inst.last_error = None
    inst.last_stats = {"identities": discovered, "in_scope": in_scope_n}
    db.commit()
    logger.info("m365 discovery ok (instance=%s): %d identities, %d in scope",
                inst.id, discovered, in_scope_n)
    return {"ok": True, "discovered": discovered, "in_scope": in_scope_n}
