"""Okta — Arkive compliance integration (control-plane spec).

Assess your Okta identity posture — MFA/authenticator policies, phishing-resistant factors, privileged-admin footprint and password policy — as authoritative access-control evidence.

Ships as a compliance shell (``status="coming_soon"``) until its collection backend
lands. Business/Enterprise; entitlement is enforced server-side by the plan list.
"""

from __future__ import annotations

from ..base import Integration, IntegrationSpec, register_integration

# Capability bundles this package will consent to during setup.
CAPABILITIES = ['users', 'factors', 'policies', 'admin_roles']


@register_integration
class OktaIntegration(Integration):
    """Okta organization integration (managed compliance evidence). Runs on the
    assigned customer node; authenticates via the vendor's admin OAuth / API token."""

    integration_type = "okta"

    def spec(self) -> IntegrationSpec:
        return IntegrationSpec(
            integration_type=self.integration_type,
            display_name="Okta",
            description=('Assess your Okta identity posture — MFA/authenticator policies, phishing-resistant factors, privileged-admin footprint and password policy — as authoritative access-control evidence.'),
            icon="shield",
            color="#007DC1",
            category="identity",
            runs_on="node",
            needs_appliance=False,
            default_interval_minutes=360,
            auto_provision_key=False,
            provides=['identity', 'access_policy'],
            version="0.1.0",
            status="coming_soon",
            plans=["business", "enterprise"],
            min_plan="business",
            capabilities=CAPABILITIES,
            ownership_models=["organization"],
            managed=True,
            workspace=False,
            docs_slug="integration-okta",
            credential_fields=[],
        )
