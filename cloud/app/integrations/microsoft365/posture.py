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
# A protected source counts as "fresh" if it collected successfully within this
# window; the backup_freshness signal also expires after it so a silently-stalled
# box can't leave a stale "fresh" reading counting as met.
_STALE_AFTER = timedelta(hours=48)
# ManagedSource.state buckets for coverage reconciliation.
_EXCLUDED_STATES = {"decommissioned", "paused_by_admin"}       # intentionally not protected
_FAILED_STATES = {"permission_required", "credential_error",   # broken → unprotected
                  "source_unavailable", "disconnected"}


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


def _age(now: datetime, dt: datetime) -> str:
    secs = max(0, int((now - (dt.replace(tzinfo=None) if dt.tzinfo else dt)).total_seconds()))
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _refresh_coverage(db: Session, inst) -> int:
    """Reconcile ACTUAL protection coverage + freshness for one instance from LOCAL
    state (no Graph call) into scope-aware signals — the gap between what SHOULD be
    protected and what actually has a fresh, recoverable recovery point.

    expected = in-scope managed sources (excluding intentionally paused/decommissioned)
    covered  = state ``active`` AND a recoverable recovery point exists
    failed   = broken sources (permission/credential/unavailable/disconnected)
    Freshness track = active sources that collected within ``_STALE_AFTER`` AND are
    recoverable. Runs on the owning box; the signals replicate UP to the CP engine."""
    from ...models import Collection, SnapshotReceipt
    tid = inst.tenant_id
    now = _now()

    sources = (db.query(m.ManagedSource)
               .filter(m.ManagedSource.integration_instance_id == inst.id).all())
    if not sources:
        return 0

    # Map source -> its managed Collection, to check for a recoverable recovery point.
    colls = [c for c in db.query(Collection).filter(Collection.tenant_id == tid).all()
             if (c.config or {}).get("m365_instance_id") == inst.id and (c.config or {}).get("managed")]
    src_to_coll = {(c.config or {}).get("m365_source_id"): c.id for c in colls
                   if (c.config or {}).get("m365_source_id")}
    coll_ids = [cid for cid in src_to_coll.values() if cid]
    recoverable: set = set()
    if coll_ids:
        recoverable = {cid for (cid,) in db.query(SnapshotReceipt.collection_id)
                       .filter(SnapshotReceipt.collection_id.in_(coll_ids),
                               SnapshotReceipt.recoverable.is_(True)).distinct().all()}

    in_scope_ids = (db.query(m.ExternalIdentity)
                    .filter(m.ExternalIdentity.integration_instance_id == inst.id,
                            m.ExternalIdentity.in_scope.is_(True)).count())

    expected = covered = failed = 0
    fresh_expected = fresh_covered = fresh_failed = 0
    gaps: list[dict] = []
    stale: list[dict] = []
    for s in sources:
        if s.state in _EXCLUDED_STATES:
            continue
        expected += 1
        has_rp = src_to_coll.get(s.id) in recoverable
        lc = s.last_collected_at
        if s.state == "active" and has_rp:
            covered += 1
        elif s.state in _FAILED_STATES:
            failed += 1
            if len(gaps) < 50:
                gaps.append({"kind": "source", "label": s.name or s.source_key, "status": "unmet",
                             "note": f"{s.workload} — {(s.state or '').replace('_', ' ')}"})
        else:  # planned/provisioning/baseline_pending/empty/partial/active-without-recovery-point
            if len(gaps) < 50:
                gaps.append({"kind": "source", "label": s.name or s.source_key, "status": "partial",
                             "note": f"{s.workload} — {(s.state or 'pending').replace('_', ' ')}"
                                     + ("" if has_rp else "; no recovery point yet")})
        if s.state == "active":  # freshness only applies to sources meant to be collecting
            fresh_expected += 1
            fresh = bool(lc and (now - (lc.replace(tzinfo=None) if lc.tzinfo else lc)) <= _STALE_AFTER)
            if fresh and has_rp:
                fresh_covered += 1
            else:
                fresh_failed += 1
                if len(stale) < 50:
                    stale.append({"kind": "source", "label": s.name or s.source_key, "status": "partial",
                                  "note": f"{s.workload} — last protected {('never' if not lc else _age(now, lc))}"})

    n = 0
    if expected:
        ratio = covered / expected
        status = "met" if ratio >= 0.98 else ("partial" if ratio >= 0.5 else "unmet")
        signals.record(
            db, tid, _PROVIDER, "coverage_completeness", status=status,
            summary=f"{covered}/{expected} in-scope Microsoft 365 source(s) protected & recoverable"
                    + (f"; {failed} failed" if failed else ""),
            scope_type="integration", scope_id=inst.id, integration_instance_id=inst.id,
            expected=expected, covered=covered, failed=failed,
            evidence_level="verified_test" if covered else "observed", entities=gaps,
            detail={"in_scope_identities": in_scope_ids, "sources": expected},
            remediation="Resolve permission/credential errors and let provisioning finish so every "
                        "in-scope source has a recoverable recovery point.")
        n += 1
    if fresh_expected:
        fratio = fresh_covered / fresh_expected
        fstatus = "met" if fratio >= 0.95 else ("partial" if fratio >= 0.5 else "unmet")
        hours = int(_STALE_AFTER.total_seconds() // 3600)
        signals.record(
            db, tid, _PROVIDER, "backup_freshness", status=fstatus,
            summary=f"{fresh_covered}/{fresh_expected} active source(s) protected within {hours}h",
            scope_type="integration", scope_id=inst.id, integration_instance_id=inst.id,
            expected=fresh_expected, covered=fresh_covered, failed=fresh_failed,
            evidence_level="observed", expires_at=now + _STALE_AFTER, entities=stale,
            remediation="Investigate sources whose last successful backup is stale or missing a recovery point.")
        n += 1
    return n


def refresh(db: Session, inst, *, force: bool = False) -> int:
    """Collect posture for one M365 instance into compliance signals. Returns count."""
    tid = inst.tenant_id
    # Local coverage + freshness reconciliation (no Graph call) — always runs so
    # protection gaps surface even before admin consent for the posture perms.
    n = _refresh_coverage(db, inst)
    # Throttle ONLY the Graph posture fetch (keyed on a Graph capability so the
    # coverage signals recorded above don't reset the refresh clock).
    if not force:
        last = signals.latest_at(db, tid, _PROVIDER, capability="mfa")
        if last and (_now() - last) < _MIN_REFRESH:
            db.commit()
            return n
    token = _token_for(db, inst)
    if not token:
        db.commit()
        return n

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
