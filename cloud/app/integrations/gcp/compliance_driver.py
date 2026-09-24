"""Google Cloud compliance driver — maps Google Cloud posture to Arkive compliance capabilities.

Registers a compliance evidence provider for this integration. Until the collection
backend ships (``status="coming_soon"``) no live instance exists, so the provider
contributes no evidence — Arkive never fabricates posture it hasn't observed. The
CAPABILITIES map documents exactly what this integration evidences once connected;
its keys are real capability keys from ``compliance.registry.CAPABILITIES``.
"""

from __future__ import annotations

from ..shell_compliance import register_shell_driver

# compliance capability -> how Google Cloud evidences it.
CAPABILITIES: dict[str, str] = {
    "encryption_at_rest": 'Customer-managed encryption keys (CMEK) on storage/disks.',
    "encryption_in_transit": 'TLS / Google-managed transit encryption enforced.',
    "audit_logging": 'Cloud Audit Logs (admin + data access) enabled.',
    "monitoring": 'Security Command Center monitoring enabled.',
    "mfa": '2-Step Verification enforced for principals.',
    "data_residency": 'Resources constrained to approved locations (org policy).',
}

register_shell_driver("gcp", CAPABILITIES)
