"""Amazon Web Services compliance driver — maps Amazon Web Services posture to Arkive compliance capabilities.

Registers a compliance evidence provider for this integration. Until the collection
backend ships (``status="coming_soon"``) no live instance exists, so the provider
contributes no evidence — Arkive never fabricates posture it hasn't observed. The
CAPABILITIES map documents exactly what this integration evidences once connected;
its keys are real capability keys from ``compliance.registry.CAPABILITIES``.
"""

from __future__ import annotations

from ..shell_compliance import register_shell_driver

# compliance capability -> how Amazon Web Services evidences it.
CAPABILITIES: dict[str, str] = {
    "encryption_at_rest": 'Default at-rest encryption on S3 / EBS / RDS (KMS).',
    "encryption_in_transit": 'TLS enforced on public endpoints / S3 policies.',
    "audit_logging": 'CloudTrail enabled across regions with log-file validation.',
    "monitoring": 'GuardDuty / Security Hub monitoring enabled.',
    "mfa": 'MFA enforced for IAM users and the root account.',
    "data_residency": 'Resources pinned to approved AWS regions.',
}

register_shell_driver("aws", CAPABILITIES)
