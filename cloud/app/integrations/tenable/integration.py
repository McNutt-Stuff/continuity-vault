"""Tenable — Arkive compliance integration (control-plane spec).

Fold your Tenable (Nessus / Tenable.io) program into Arkive compliance — asset scan coverage, exposure findings and remediation SLA adherence.

Ships as a compliance shell (``status="coming_soon"``) until its collection backend
lands. Business/Enterprise; entitlement is enforced server-side by the plan list.
"""

from __future__ import annotations

from ..base import Integration, IntegrationSpec, register_integration

# Capability bundles this package will consent to during setup.
CAPABILITIES = ['scans', 'assets', 'findings']


@register_integration
class TenableIntegration(Integration):
    """Tenable organization integration (managed compliance evidence). Runs on the
    assigned customer node; authenticates via the vendor's admin OAuth / API token."""

    integration_type = "tenable"

    def spec(self) -> IntegrationSpec:
        return IntegrationSpec(
            integration_type=self.integration_type,
            display_name="Tenable",
            description=('Fold your Tenable (Nessus / Tenable.io) program into Arkive compliance — asset scan coverage, exposure findings and remediation SLA adherence.'),
            icon="shield",
            color="#0C1E5B",
            category="security",
            runs_on="node",
            needs_appliance=False,
            default_interval_minutes=360,
            auto_provision_key=False,
            provides=['vulnerabilities'],
            version="0.1.0",
            status="coming_soon",
            plans=["business", "enterprise"],
            min_plan="business",
            capabilities=CAPABILITIES,
            ownership_models=["organization"],
            managed=True,
            workspace=False,
            docs_slug="integration-tenable",
            credential_fields=[],
        )
