"""Jamf Pro compliance driver — maps Jamf Pro posture to Arkive compliance capabilities.

Registers a compliance evidence provider for this integration. Until the collection
backend ships (``status="coming_soon"``) no live instance exists, so the provider
contributes no evidence — Arkive never fabricates posture it hasn't observed. The
CAPABILITIES map documents exactly what this integration evidences once connected;
its keys are real capability keys from ``compliance.registry.CAPABILITIES``.
"""

from __future__ import annotations

from ..shell_compliance import register_shell_driver

# compliance capability -> how Jamf Pro evidences it.
CAPABILITIES: dict[str, str] = {
    "device_compliance": 'Enrolled Macs meeting Jamf compliance / smart-group policy.',
    "device_encryption": 'FileVault disk encryption enabled and key-escrowed.',
}

register_shell_driver("jamf", CAPABILITIES)
