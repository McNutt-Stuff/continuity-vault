"""Fleet software versions: the production builds the control plane serves to
each device class, plus the control plane's own running version. Used by the
admin + customer UIs to show current vs. available versions and flag out-of-date
devices."""

from __future__ import annotations


def control_plane_version() -> str:
    """The platform version the control plane itself is running (git short SHA)."""
    try:
        with open("/etc/arkive/version") as fh:
            return fh.read().strip()
    except Exception:
        return ""


def node_production_version() -> str:
    """The node/platform bundle version the control plane currently serves."""
    from .api.site import _node_bundle_version
    return _node_bundle_version()


def appliance_production_version() -> str:
    from .api.appliances import _appliance_bundle_version
    return _appliance_bundle_version()


def agent_production_version() -> str:
    from .api.agents import _agent_bundle_version
    return _agent_bundle_version()


def all_versions() -> dict:
    return {
        "control_plane": control_plane_version(),
        "platform": node_production_version(),
        "appliance": appliance_production_version(),
        "agent": agent_production_version(),
    }
