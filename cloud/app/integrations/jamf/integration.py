"""Jamf Pro — Arkive compliance integration (control-plane spec).

Assess your Apple endpoint fleet in Jamf Pro — device management enrollment, FileVault disk encryption and compliance-policy health.

Ships as a compliance shell (``status="coming_soon"``) until its collection backend
lands. Business/Enterprise; entitlement is enforced server-side by the plan list.
"""

from __future__ import annotations

from ..base import Integration, IntegrationSpec, register_integration

# Capability bundles this package will consent to during setup.
CAPABILITIES = ['inventory', 'filevault', 'compliance']


@register_integration
class JamfIntegration(Integration):
    """Jamf Pro organization integration (managed compliance evidence). Runs on the
    assigned customer node; authenticates via the vendor's admin OAuth / API token."""

    integration_type = "jamf"

    def spec(self) -> IntegrationSpec:
        return IntegrationSpec(
            integration_type=self.integration_type,
            display_name="Jamf Pro",
            description=('Assess your Apple endpoint fleet in Jamf Pro — device management enrollment, FileVault disk encryption and compliance-policy health.'),
            icon="server",
            color="#37AADC",
            category="endpoint",
            runs_on="node",
            needs_appliance=False,
            default_interval_minutes=360,
            auto_provision_key=False,
            provides=['devices', 'encryption'],
            version="0.1.0",
            status="coming_soon",
            plans=["business", "enterprise"],
            min_plan="business",
            capabilities=CAPABILITIES,
            ownership_models=["organization"],
            managed=True,
            workspace=False,
            docs_slug="integration-jamf",
            credential_fields=[],
        )
