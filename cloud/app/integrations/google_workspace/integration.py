"""Google Workspace — Arkive managed integration (control-plane spec).

Admin-governed protection of Google Workspace, mirroring Microsoft 365: a service
account with domain-wide delegation turns the Workspace directory into the source
for Arkive org users and centrally protects Gmail, Drive, Calendar and Contacts,
while also evidencing identity/collaboration compliance posture.

Discovery + collection backends land in phases; the spec ships as
``status="coming_soon"`` until then, so setup is blocked with a clear message
rather than a broken flow. Business/Enterprise; entitlement is enforced server-side.
"""

from __future__ import annotations

from ..base import Integration, IntegrationSpec, register_integration

# Capability bundles this package provides (per-workload + governance).
CAPABILITIES = [
    "directory",
    "gmail",
    "google_drive",
    "google_calendar",
    "google_contacts",
    "google_photos",
    "shared_drives",
    "managed_sources",
    "compliance_evidence",
]


@register_integration
class GoogleWorkspaceIntegration(Integration):
    """Google Workspace organization integration (managed). Runs on the assigned
    customer node; authenticates via a domain-wide-delegation service account (an
    admin authorizes read-only scopes once — no per-employee sign-in)."""

    integration_type = "google_workspace"

    def spec(self) -> IntegrationSpec:
        return IntegrationSpec(
            integration_type=self.integration_type,
            display_name="Google Workspace",
            description=("Turn Google Workspace into the source for your Arkive org users, and "
                         "centrally protect Gmail, Drive, Calendar and Contacts — administrator-"
                         "governed via a domain-wide-delegation service account, no per-employee "
                         "sign-in required."),
            icon="cloud",
            color="#1A73E8",
            category="productivity",
            runs_on="node",
            needs_appliance=False,
            default_interval_minutes=360,
            auto_provision_key=False,
            provides=["users", "email", "files", "calendar", "contacts"],
            version="0.2.0",
            status="preview",                  # connect + directory discovery
            plans=["business", "enterprise"],
            min_plan="business",
            capabilities=CAPABILITIES,
            ownership_models=["managed_user", "organization"],
            managed=True,
            workspace=True,
            docs_slug="integration-google-workspace",
            credential_fields=[],
        )
