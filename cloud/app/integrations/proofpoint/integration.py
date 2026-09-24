"""Proofpoint — Arkive compliance integration (control-plane spec).

Fold your Proofpoint Security Awareness Training into Arkive compliance — assignment completion and phishing-simulation performance.

Ships as a compliance shell (``status="coming_soon"``) until its collection backend
lands. Business/Enterprise; entitlement is enforced server-side by the plan list.
"""

from __future__ import annotations

from ..base import Integration, IntegrationSpec, register_integration

# Capability bundles this package will consent to during setup.
CAPABILITIES = ['users', 'training', 'phishing']


@register_integration
class ProofpointIntegration(Integration):
    """Proofpoint organization integration (managed compliance evidence). Runs on the
    assigned customer node; authenticates via the vendor's admin OAuth / API token."""

    integration_type = "proofpoint"

    def spec(self) -> IntegrationSpec:
        return IntegrationSpec(
            integration_type=self.integration_type,
            display_name="Proofpoint",
            description=('Fold your Proofpoint Security Awareness Training into Arkive compliance — assignment completion and phishing-simulation performance.'),
            icon="user",
            color="#0AA5C6",
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
            docs_slug="integration-proofpoint",
            credential_fields=[],
        )
