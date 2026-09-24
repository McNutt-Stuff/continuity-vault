"""Google Workspace compliance driver — maps Google Workspace posture to Arkive compliance capabilities.

Registers a compliance evidence provider for this integration. Until the collection
backend ships (``status="coming_soon"``) no live instance exists, so the provider
contributes no evidence — Arkive never fabricates posture it hasn't observed. The
CAPABILITIES map documents exactly what this integration evidences once connected;
its keys are real capability keys from ``compliance.registry.CAPABILITIES``.
"""

from __future__ import annotations

from ..shell_compliance import register_shell_driver

# compliance capability -> how Google Workspace evidences it.
CAPABILITIES: dict[str, str] = {
    "mfa": '2-Step Verification enrollment across the directory (Admin SDK reports).',
    "phishing_resistant_mfa": 'Security-key / passkey enrollment (phishing-resistant methods).',
    "password_policy": 'Password-strength policy and reuse/length enforcement.',
    "conditional_access": 'Context-Aware Access policies gating sign-in.',
    "external_sharing_control": 'Drive external-sharing controls at the org/OU level.',
    "guest_access": 'External / visitor account exposure vs the directory.',
    "data_residency": 'Assigned data region (Google Workspace data regions).',
    "audit_logging": 'Admin + login audit logging is enabled and retained.',
}

register_shell_driver("google_workspace", CAPABILITIES)
