"""Ubiquiti UniFi integration package (self-contained).

Importing this package self-registers the integration. Its collection logic runs
on the customer appliance (appliance/agent/integrations/ubiquiti.py); this package
holds the control-plane metadata/spec only.
"""

from __future__ import annotations

from . import integration  # noqa: F401  triggers self-registration

__all__ = ["integration"]
