"""Amazon Web Services — Arkive compliance integration (control-plane spec).

Assess your AWS security configuration — default encryption, CloudTrail audit logging, GuardDuty monitoring, IAM MFA and account region posture.

Ships as a compliance shell (``status="coming_soon"``) until its collection backend
lands. Business/Enterprise; entitlement is enforced server-side by the plan list.
"""

from __future__ import annotations

from ..base import Integration, IntegrationSpec, register_integration

# Capability bundles this package will consent to during setup.
CAPABILITIES = ['config', 'cloudtrail', 'iam', 'guardduty']


@register_integration
class AwsIntegration(Integration):
    """Amazon Web Services organization integration (managed compliance evidence). Runs on the
    assigned customer node; authenticates via the vendor's admin OAuth / API token."""

    integration_type = "aws"

    def spec(self) -> IntegrationSpec:
        return IntegrationSpec(
            integration_type=self.integration_type,
            display_name="Amazon Web Services",
            description=('Assess your AWS security configuration — default encryption, CloudTrail audit logging, GuardDuty monitoring, IAM MFA and account region posture.'),
            icon="cloud",
            color="#FF9900",
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
            docs_slug="integration-aws",
            credential_fields=[],
        )
