"""Integrations framework.

Integrations unlock *auxiliary intelligence* about a customer's environment
(e.g. what apps/cloud services their network is using) rather than backing data
into the vault. They run either on the customer's appliance (LAN-local sources
like a router/controller) or on a cloud node, per the integration's declared
``runs_on``. Each integration self-registers so adding one is a single class.
"""

from __future__ import annotations

from .base import (
    CredentialField,
    Integration,
    IntegrationSpec,
    all_integrations,
    get_integration,
    register_integration,
)
from . import registry  # noqa: F401  ensure concrete integrations self-register

__all__ = [
    "CredentialField",
    "Integration",
    "IntegrationSpec",
    "all_integrations",
    "get_integration",
    "register_integration",
]
