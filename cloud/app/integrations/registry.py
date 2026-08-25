"""Concrete integrations (metadata + config schema)."""

from __future__ import annotations

from .base import (
    CredentialField,
    Integration,
    IntegrationSpec,
    register_integration,
)


@register_integration
class UbiquitiIntegration(Integration):
    """UniFi (Dream Machine / UniFi OS) controller integration.

    Runs on the customer appliance because it must reach the local gateway. It
    queries the controller for the applications / cloud services in use, which
    clients (computers/people) are using them, and how much traffic each moves —
    feeding shadow-app detection, analytics and insights.
    """

    integration_type = "ubiquiti"

    def spec(self) -> IntegrationSpec:
        return IntegrationSpec(
            integration_type=self.integration_type,
            display_name="Ubiquiti UniFi",
            description=("Discover the apps and cloud services your network uses, who's "
                         "using them, and how much data they move — straight from your "
                         "UniFi Dream Machine. Powers shadow-app detection and insights."),
            icon="activity",
            color="#0559c9",
            category="network",
            runs_on="appliance",
            needs_appliance=True,
            default_interval_minutes=60,
            auto_provision_key=True,
            provides=["clients", "apps", "traffic"],
            credential_fields=[
                CredentialField("host", "Controller address", type="host",
                                placeholder="192.168.1.1",
                                help="Your UniFi gateway's local IP or hostname."),
                CredentialField("username", "Admin username", type="text",
                                placeholder="admin"),
                CredentialField("password", "Admin password", type="password",
                                help="Used once to mint a scoped API key, then discarded."),
            ],
        )
