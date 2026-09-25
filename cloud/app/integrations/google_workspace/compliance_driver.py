"""Google Workspace compliance driver — folds live posture into the compliance engine.

Registers a compliance REFRESHER that collects Google Workspace directory posture
into ComplianceSignal rows. Those signals are surfaced by the platform's standard
``integration_signals`` provider, so Google Workspace impacts compliance exactly
like Microsoft 365 — and IN ADDITION to it (the engine takes the best status per
capability across providers). Posture is collected on the box that OWNS the tenant;
during a CP evaluate we must not re-probe a node-owned tenant (the CP has no
credentials for it and would clobber the good replicated signals).

CAPABILITIES documents what this integration evidences today; FUTURE_CAPABILITIES
lists capabilities that land as their Admin SDK reads are wired (never faked).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ...compliance.providers import register_refresher

# compliance capability -> how Google Workspace evidences it (assessed today).
CAPABILITIES: dict[str, str] = {
    "mfa": "2-Step Verification enrollment across active directory users.",
    "privileged_access_review": "Google Workspace admin-role footprint.",
    "audit_logging": "Admin + login audit logging (always on in Workspace).",
}

# Wired in later slices as their Admin SDK reads land (kept for parity with M365).
FUTURE_CAPABILITIES: dict[str, str] = {
    "phishing_resistant_mfa": "Security-key / passkey enrollment.",
    "password_policy": "Password-strength policy and reuse/length enforcement.",
    "conditional_access": "Context-Aware Access policies gating sign-in.",
    "external_sharing_control": "Drive external-sharing controls at the org/OU level.",
    "guest_access": "External / visitor account exposure.",
    "data_residency": "Assigned Google Workspace data region.",
}


def _refresh(db: Session, tenant) -> None:
    from ...config import get_settings
    from . import posture
    role = (get_settings().node_role or "control-plane")
    if role == "control-plane" and getattr(tenant, "node_id", None):
        return  # node-owned tenant — its own node collects + replicates the signals up
    posture.refresh_for_tenant(db, tenant)


register_refresher("google_workspace", _refresh)
