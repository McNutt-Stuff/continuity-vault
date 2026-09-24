"""Okta compliance driver — maps Okta posture to Arkive compliance capabilities.

Registers a compliance evidence provider for this integration. Until the collection
backend ships (``status="coming_soon"``) no live instance exists, so the provider
contributes no evidence — Arkive never fabricates posture it hasn't observed. The
CAPABILITIES map documents exactly what this integration evidences once connected;
its keys are real capability keys from ``compliance.registry.CAPABILITIES``.
"""

from __future__ import annotations

from ..shell_compliance import register_shell_driver

# compliance capability -> how Okta evidences it.
CAPABILITIES: dict[str, str] = {
    "mfa": 'Authenticator enrollment + sign-on MFA policy coverage.',
    "phishing_resistant_mfa": 'FIDO2/WebAuthn (Okta FastPass, security keys) coverage.',
    "privileged_mfa": 'MFA enforced for admin/super-admin sign-on.',
    "privileged_access_review": 'Standing admin-role holders (super/org admins) footprint.',
    "password_policy": 'Password policy strength, lockout and reuse rules.',
    "conditional_access": 'Network-zone / device / risk-based sign-on policies.',
    "access_control": 'Group/app assignment governs least-privilege access.',
}

register_shell_driver("okta", CAPABILITIES)
