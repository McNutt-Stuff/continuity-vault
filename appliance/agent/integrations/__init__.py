"""Appliance-side integration runners.

Integrations that must reach the customer's LAN (e.g. the UniFi controller) run
here, on the appliance. The worker polls each enabled integration on its own
interval, runs the matching runner, and ships the normalized report to the
node / control plane.
"""

from __future__ import annotations

from . import ubiquiti

# integration_type -> runner module exposing collect(config, credentials, log).
RUNNERS = {
    "ubiquiti": ubiquiti,
}


def get_runner(integration_type: str):
    return RUNNERS.get(integration_type)
