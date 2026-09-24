"""Qualys — Arkive compliance integration (control-plane spec).

Fold your Qualys VMDR program into Arkive compliance — scan coverage, open findings by severity and remediation SLA adherence.

Ships as a compliance shell (``status="coming_soon"``) until its collection backend
lands. Business/Enterprise; entitlement is enforced server-side by the plan list.
"""

from __future__ import annotations

from ..base import Integration, IntegrationSpec, register_integration

# Capability bundles this package will consent to during setup.
CAPABILITIES = ['vmdr', 'assets', 'findings']


@register_integration
class QualysIntegration(Integration):
    """Qualys organization integration (managed compliance evidence). Runs on the
    assigned customer node; authenticates via the vendor's admin OAuth / API token."""

    integration_type = "qualys"

    def spec(self) -> IntegrationSpec:
        return IntegrationSpec(
            integration_type=self.integration_type,
            display_name="Qualys",
            description=('Fold your Qualys VMDR program into Arkive compliance — scan coverage, open findings by severity and remediation SLA adherence.'),
            icon="shield",
            color="#ED2E26",
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
            docs_slug="integration-qualys",
            credential_fields=[],
        )
