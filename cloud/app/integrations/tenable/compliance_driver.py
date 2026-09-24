"""Tenable compliance driver — maps Tenable posture to Arkive compliance capabilities.

Registers a compliance evidence provider for this integration. Until the collection
backend ships (``status="coming_soon"``) no live instance exists, so the provider
contributes no evidence — Arkive never fabricates posture it hasn't observed. The
CAPABILITIES map documents exactly what this integration evidences once connected;
its keys are real capability keys from ``compliance.registry.CAPABILITIES``.
"""

from __future__ import annotations

from ..shell_compliance import register_shell_driver

# compliance capability -> how Tenable evidences it.
CAPABILITIES: dict[str, str] = {
    "vulnerability_management": 'Scan coverage across assets + open vulnerabilities by severity and SLA adherence.',
}

register_shell_driver("tenable", CAPABILITIES)
