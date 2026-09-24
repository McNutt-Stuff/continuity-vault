"""Proofpoint compliance driver — maps Proofpoint posture to Arkive compliance capabilities.

Registers a compliance evidence provider for this integration. Until the collection
backend ships (``status="coming_soon"``) no live instance exists, so the provider
contributes no evidence — Arkive never fabricates posture it hasn't observed. The
CAPABILITIES map documents exactly what this integration evidences once connected;
its keys are real capability keys from ``compliance.registry.CAPABILITIES``.
"""

from __future__ import annotations

from ..shell_compliance import register_shell_driver

# compliance capability -> how Proofpoint evidences it.
CAPABILITIES: dict[str, str] = {
    "security_training": 'Awareness-training assignment completion + phishing simulation results.',
}

register_shell_driver("proofpoint", CAPABILITIES)
