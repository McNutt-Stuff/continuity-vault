"""Integration base classes + registry (metadata only; collection logic for
appliance-run integrations lives in the appliance agent)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Type


@dataclass
class CredentialField:
    """One credential/config field prompted during easy setup."""

    name: str
    label: str
    type: str = "text"          # text | password | host | number
    placeholder: str = ""
    required: bool = True
    help: str = ""


@dataclass
class IntegrationSpec:
    integration_type: str
    display_name: str
    description: str
    icon: str
    color: str
    category: str               # network | ...
    runs_on: str = "appliance"  # appliance | cloud
    # True when the integration MUST run on a customer appliance (needs LAN access).
    needs_appliance: bool = True
    default_interval_minutes: int = 60
    credential_fields: List[CredentialField] = field(default_factory=list)
    # Data domains this integration produces (drives UI drill-downs / analytics).
    provides: List[str] = field(default_factory=lambda: ["clients", "apps"])
    # When set, setup will programmatically mint an API key from the supplied
    # login credentials (so the user doesn't do a multi-step key dance).
    auto_provision_key: bool = False


class Integration:
    integration_type: str = "base"

    def spec(self) -> IntegrationSpec:  # pragma: no cover - overridden
        raise NotImplementedError


INTEGRATION_REGISTRY: Dict[str, Integration] = {}


def register_integration(cls: Type[Integration]) -> Type[Integration]:
    inst = cls()
    INTEGRATION_REGISTRY[inst.integration_type] = inst
    return cls


def get_integration(integration_type: str) -> Optional[Integration]:
    return INTEGRATION_REGISTRY.get(integration_type)


def all_integrations() -> List[Integration]:
    return list(INTEGRATION_REGISTRY.values())
