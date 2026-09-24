"""Google Cloud — Arkive compliance integration (control-plane spec).

Assess your Google Cloud security configuration — CMEK encryption, Cloud Audit Logs, Security Command Center monitoring, MFA and region posture.

Ships as a compliance shell (``status="coming_soon"``) until its collection backend
lands. Business/Enterprise; entitlement is enforced server-side by the plan list.
"""

from __future__ import annotations

from ..base import Integration, IntegrationSpec, register_integration

# Capability bundles this package will consent to during setup.
CAPABILITIES = ['scc', 'audit_logs', 'iam', 'cmek']


@register_integration
class GcpIntegration(Integration):
    """Google Cloud organization integration (managed compliance evidence). Runs on the
    assigned customer node; authenticates via the vendor's admin OAuth / API token."""

    integration_type = "gcp"

    def spec(self) -> IntegrationSpec:
        return IntegrationSpec(
            integration_type=self.integration_type,
            display_name="Google Cloud",
            description=('Assess your Google Cloud security configuration — CMEK encryption, Cloud Audit Logs, Security Command Center monitoring, MFA and region posture.'),
            icon="cloud",
            color="#4285F4",
            category="cloud",
            runs_on="node",
            needs_appliance=False,
            default_interval_minutes=360,
            auto_provision_key=False,
            provides=['cloud_config', 'encryption', 'logging'],
            version="0.1.0",
            status="coming_soon",
            plans=["business", "enterprise"],
            min_plan="business",
            capabilities=CAPABILITIES,
            ownership_models=["organization"],
            managed=True,
            workspace=False,
            docs_slug="integration-gcp",
            credential_fields=[],
        )
