"""Kandji compliance driver — maps Kandji posture to Arkive compliance capabilities.

Registers a compliance evidence provider for this integration. Until the collection
backend ships (``status="coming_soon"``) no live instance exists, so the provider
contributes no evidence — Arkive never fabricates posture it hasn't observed. The
CAPABILITIES map documents exactly what this integration evidences once connected;
its keys are real capability keys from ``compliance.registry.CAPABILITIES``.
"""

from __future__ import annotations

from ..shell_compliance import register_shell_driver

# compliance capability -> how Kandji evidences it.
CAPABILITIES: dict[str, str] = {
    "device_compliance": 'Devices passing their assigned Kandji Blueprint / Library items.',
    "device_encryption": 'FileVault enforced with escrowed recovery keys.',
}

register_shell_driver("kandji", CAPABILITIES)
