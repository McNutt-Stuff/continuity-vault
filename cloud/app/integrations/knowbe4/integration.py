"""KnowBe4 — Arkive compliance integration (control-plane spec).

Fold your KnowBe4 security-awareness program into Arkive compliance — training completion rates and simulated-phishing results.

Ships as a compliance shell (``status="coming_soon"``) until its collection backend
lands. Business/Enterprise; entitlement is enforced server-side by the plan list.
"""

from __future__ import annotations

from ..base import Integration, IntegrationSpec, register_integration

# Capability bundles this package will consent to during setup.
CAPABILITIES = ['users', 'training', 'phishing']


@register_integration
class KnowBe4Integration(Integration):
    """KnowBe4 organization integration (managed compliance evidence). Runs on the
    assigned customer node; authenticates via the vendor's admin OAuth / API token."""

    integration_type = "knowbe4"

    def spec(self) -> IntegrationSpec:
        return IntegrationSpec(
            integration_type=self.integration_type,
            display_name="KnowBe4",
            description=('Fold your KnowBe4 security-awareness program into Arkive compliance — training completion rates and simulated-phishing results.'),
            icon="user",
            color="#F26D21",
            category="security",
            runs_on="node",
            needs_appliance=False,
            default_interval_minutes=360,
            auto_provision_key=False,
            provides=['training'],
            version="0.1.0",
            status="coming_soon",
            plans=["business", "enterprise"],
            min_plan="business",
            capabilities=CAPABILITIES,
            ownership_models=["organization"],
            managed=True,
            workspace=False,
            docs_slug="integration-knowbe4",
            credential_fields=[],
        )
