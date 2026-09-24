"""Microsoft Azure compliance driver — maps Microsoft Azure posture to Arkive compliance capabilities.

Registers a compliance evidence provider for this integration. Until the collection
backend ships (``status="coming_soon"``) no live instance exists, so the provider
contributes no evidence — Arkive never fabricates posture it hasn't observed. The
CAPABILITIES map documents exactly what this integration evidences once connected;
its keys are real capability keys from ``compliance.registry.CAPABILITIES``.
"""

from __future__ import annotations

from ..shell_compliance import register_shell_driver

# compliance capability -> how Microsoft Azure evidences it.
CAPABILITIES: dict[str, str] = {
    "encryption_at_rest": 'Storage Service / disk encryption enabled (customer-managed keys).',
    "encryption_in_transit": 'Secure-transfer-required on storage; TLS minimums enforced.',
    "audit_logging": 'Azure Activity Log + diagnostic settings exported.',
    "monitoring": 'Microsoft Defender for Cloud enabled on subscriptions.',
    "mfa": 'MFA enforced via Entra ID sign-in policy.',
    "data_residency": 'Resources deployed only in approved Azure regions.',
}

register_shell_driver("azure", CAPABILITIES)
