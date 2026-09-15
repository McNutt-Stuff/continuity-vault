"""Microsoft 365 compliance posture collector.

Fetches tenant-level security posture from Microsoft Graph (app-only) and records
it as compliance SIGNALS the platform engine scores: MFA registration, conditional
access, DLP, external sharing and data residency. Each fetch is best-effort — a
missing Graph permission records an ``unknown`` signal that names the permission to
grant, so the control is *assessable* the moment the app is authorized.

Runs on the box that owns the M365 instance (the CP has the credential; posture is
tenant metadata, not per-user content). Throttled so it refreshes at most a few
times a day. This is the reference implementation for the reusable
``compliance.signals`` pattern — see .github/instructions/compliance.instructions.md.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ...compliance import signals
from . import graph, models as m

logger = logging.getLogger("cv.integrations.m365.posture")

_PROVIDER = "microsoft365"
_MIN_REFRESH = timedelta(hours=6)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _token_for(db: Session, inst) -> str:
    from ... import platform_config
    vals = platform_config.integration_values("microsoft365")
    cid = (vals.get("client_id") or "").strip()
    csec = (vals.get("client_secret") or "").strip()
    cred = (db.query(m.ManagedCredentialRef)
            .filter(m.ManagedCredentialRef.integration_instance_id == inst.id).first())
    if not (cid and csec and cred and cred.consent_state == "granted" and cred.microsoft_tenant_id):
        return ""
    try:
        return graph.app_token(cid, csec, cred.microsoft_tenant_id)
    except graph.GraphError as e:
        logger.warning("m365 posture: token failed (instance=%s): %s", inst.id, e)
        return ""


def _rec(db, tid, cap, status, summary, **detail):
    signals.record(db, tid, _PROVIDER, cap, status=status, summary=summary, detail=detail)


def _needs(db, tid, cap, perm, err):
    """Record an 'unknown' signal that names the missing permission (403/insufficient)."""
    _rec(db, tid, cap, "unknown", f"Needs Microsoft Graph {perm} to assess", permission=perm,
         error=str(err)[:160])


def refresh(db: Session, inst, *, force: bool = False) -> int:
    """Collect posture for one M365 instance into compliance signals. Returns count."""
    tid = inst.tenant_id
    if not force:
        last = signals.latest_at(db, tid, _PROVIDER)
        if last and (_now() - last) < _MIN_REFRESH:
            return 0
    token = _token_for(db, inst)
    if not token:
        return 0
    n = 0

    # --- MFA registration (phishing-resistant / any strong method) --------------
    try:
        total = strong = 0
        for u in graph.get_paged(token, "/reports/authenticationMethods/userRegistrationDetails",
                                 params={"$top": "500"}, cap=100000):
            total += 1
            if u.get("isMfaRegistered") or u.get("isMfaCapable"):
                strong += 1
        if total:
            ratio = strong / total
            status = "met" if ratio >= 0.95 else ("partial" if ratio >= 0.5 else "unmet")
            _rec(db, tid, "mfa", status, f"{strong}/{total} users MFA-registered", total=total, strong=strong)
            n += 1
    except graph.GraphError as e:
        _needs(db, tid, "mfa", "Reports.Read.All (or AuditLog.Read.All)", e)
        n += 1

    # --- Conditional access policies -------------------------------------------
    try:
        pols = list(graph.get_paged(token, "/identity/conditionalAccess/policies", cap=500))
        enabled = sum(1 for p in pols if (p.get("state") == "enabled"))
        status = "met" if enabled else ("partial" if pols else "unmet")
        _rec(db, tid, "conditional_access", status,
             f"{enabled} enabled conditional-access polic(ies)" if pols else "No conditional-access policies",
             policies=len(pols), enabled=enabled)
        n += 1
    except graph.GraphError as e:
        _needs(db, tid, "conditional_access", "Policy.Read.All", e)
        n += 1

    # --- Data loss prevention (best-effort; Purview DLP is not in Graph v1.0) ---
    # There is no stable app-only Graph v1.0 surface for Purview DLP policies, so we
    # can't auto-evidence it yet — record 'unknown' so it's tracked, not silently 0.
    _rec(db, tid, "dlp", "unknown",
         "Automatic DLP assessment isn't available via Graph yet — attest manually")
    n += 1

    # --- External sharing (SharePoint tenant sharing capability) ---------------
    try:
        s = graph.get_one(token, "/admin/sharepoint/settings")
        cap = (s.get("sharingCapability") or "").lower()
        # disabled | existingExternalUserSharingOnly | externalUserSharingOnly | externalUserAndGuestSharing
        if cap in ("disabled", "existingexternalusersharingonly"):
            status, msg = "met", f"External sharing restricted ({cap})"
        elif cap == "externalusersharingonly":
            status, msg = "partial", "External sharing limited to authenticated externals"
        else:
            status, msg = "unmet", f"External sharing is open ({cap or 'unknown'})"
        _rec(db, tid, "external_sharing_control", status, msg, sharing_capability=cap)
        n += 1
    except graph.GraphError as e:
        _needs(db, tid, "external_sharing_control", "SharePointTenantSettings.Read.All", e)
        n += 1

    # --- Data residency (tenant country + preferred data location) -------------
    try:
        org = graph.get_one(token, "/organization",
                            params={"$select": "countryLetterCode,preferredDataLocation,id"})
        row = (org.get("value") or [{}])[0] if isinstance(org.get("value"), list) else org
        country = row.get("countryLetterCode") or ""
        pdl = row.get("preferredDataLocation") or ""
        loc = pdl or country
        _rec(db, tid, "data_residency", "met" if loc else "partial",
             f"Tenant data location: {loc}" if loc else "Data location known to Microsoft",
             country=country, preferred_data_location=pdl)
        n += 1
    except graph.GraphError as e:
        _needs(db, tid, "data_residency", "Organization.Read.All", e)
        n += 1

    db.commit()
    logger.info("m365 posture refreshed (instance=%s): %d signal(s)", inst.id, n)
    return n


def refresh_for_tenant(db: Session, tenant) -> None:
    """Refresher entry point registered with the compliance engine — refresh every
    connected M365 instance for this tenant."""
    from ...models import IntegrationInstance
    for inst in (db.query(IntegrationInstance)
                 .filter(IntegrationInstance.tenant_id == tenant.id,
                         IntegrationInstance.integration_type == "microsoft365").all()):
        try:
            refresh(db, inst)
        except Exception:  # noqa: BLE001
            logger.exception("m365 posture refresh failed (instance=%s)", inst.id)
