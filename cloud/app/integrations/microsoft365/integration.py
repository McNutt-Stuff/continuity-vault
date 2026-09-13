"""Microsoft 365 Managed Integration — control-plane spec.

Business/Enterprise, admin-governed. Entitlement is enforced server-side by the
plan list + the ``m365_managed_integration`` feature flag; the create path is
guarded until the collection backend ships (status="coming_soon").
"""

from __future__ import annotations

from ..base import (
    Integration,
    IntegrationSpec,
    register_integration,
)

# Capability bundles this package provides (consented per-bundle during setup).
CAPABILITIES = [
    "entra_directory",
    "exchange",
    "onedrive",
    "sharepoint",
    "teams",
    "managed_sources",
    "managed_mappings",
    "managed_rules",
    "compliance_evidence",
]


@register_integration
class Microsoft365Integration(Integration):
    """Microsoft 365 organization integration (managed). Runs on the assigned
    customer node (no appliance). Authenticates via Microsoft admin-consent OAuth
    rather than manual credentials."""

    integration_type = "microsoft365"

    def spec(self) -> IntegrationSpec:
        return IntegrationSpec(
            integration_type=self.integration_type,
            display_name="Microsoft 365",
            description=("Turn Microsoft Entra ID into the source for your Arkive org users, "
                         "and centrally protect Exchange, OneDrive, SharePoint and Teams — "
                         "administrator-governed, no per-employee sign-in required."),
            icon="cloud",
            color="#0364B8",
            category="productivity",
            runs_on="node",
            needs_appliance=False,
            default_interval_minutes=360,
            auto_provision_key=False,
            provides=["users", "email", "files", "sites", "teams"],
            version="0.1.0",
            status="coming_soon",              # gated until the backend ships
            plans=["business", "enterprise"],  # server-enforced entitlement
            min_plan="business",
            capabilities=CAPABILITIES,
            ownership_models=["managed_user", "organization"],
            managed=True,
            workspace=True,
            feature_flag="m365_managed_integration",
            docs_slug="integration-microsoft365",
            credential_fields=[],              # OAuth admin-consent, no manual creds
        )
