"""Google Workspace — Arkive compliance integration (control-plane spec).

Assess your Google Workspace identity and collaboration posture — 2-step verification, strong-authentication enrollment, admin footprint, external Drive sharing and data region.

Ships as a compliance shell (``status="coming_soon"``) until its collection backend
lands. Business/Enterprise; entitlement is enforced server-side by the plan list.
"""

from __future__ import annotations

from ..base import Integration, IntegrationSpec, register_integration

# Capability bundles this package will consent to during setup.
CAPABILITIES = ['directory', 'authentication', 'drive_sharing', 'audit']


@register_integration
class GoogleWorkspaceIntegration(Integration):
    """Google Workspace organization integration (managed compliance evidence). Runs on the
    assigned customer node; authenticates via the vendor's admin OAuth / API token."""

    integration_type = "google_workspace"

    def spec(self) -> IntegrationSpec:
        return IntegrationSpec(
            integration_type=self.integration_type,
            display_name="Google Workspace",
            description=('Assess your Google Workspace identity and collaboration posture — 2-step verification, strong-authentication enrollment, admin footprint, external Drive sharing and data region.'),
            icon="cloud",
            color="#1A73E8",
            category="identity",
            runs_on="node",
            needs_appliance=False,
            default_interval_minutes=360,
            auto_provision_key=False,
            provides=['identity', 'access_policy', 'sharing'],
            version="0.1.0",
            status="coming_soon",
            plans=["business", "enterprise"],
            min_plan="business",
            capabilities=CAPABILITIES,
            ownership_models=["organization"],
            managed=True,
            workspace=False,
            docs_slug="integration-google-workspace",
            credential_fields=[],
        )
