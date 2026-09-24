"""Kandji — Arkive compliance integration (control-plane spec).

Assess your Apple endpoint fleet in Kandji — MDM enrollment, FileVault encryption and Blueprint compliance status.

Ships as a compliance shell (``status="coming_soon"``) until its collection backend
lands. Business/Enterprise; entitlement is enforced server-side by the plan list.
"""

from __future__ import annotations

from ..base import Integration, IntegrationSpec, register_integration

# Capability bundles this package will consent to during setup.
CAPABILITIES = ['inventory', 'filevault', 'blueprints']


@register_integration
class KandjiIntegration(Integration):
    """Kandji organization integration (managed compliance evidence). Runs on the
    assigned customer node; authenticates via the vendor's admin OAuth / API token."""

    integration_type = "kandji"

    def spec(self) -> IntegrationSpec:
        return IntegrationSpec(
            integration_type=self.integration_type,
            display_name="Kandji",
            description=('Assess your Apple endpoint fleet in Kandji — MDM enrollment, FileVault encryption and Blueprint compliance status.'),
            icon="server",
            color="#6634FF",
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
            docs_slug="integration-kandji",
            credential_fields=[],
        )
