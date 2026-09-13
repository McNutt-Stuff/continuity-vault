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
    category: str               # network | productivity | security | ...
    runs_on: str = "appliance"  # appliance | cloud | node
    # True when the integration MUST run on a customer appliance (needs LAN access).
    needs_appliance: bool = True
    default_interval_minutes: int = 60
    credential_fields: List[CredentialField] = field(default_factory=list)
    # Data domains this integration produces (drives UI drill-downs / analytics).
    provides: List[str] = field(default_factory=lambda: ["clients", "apps"])
    # When set, setup will programmatically mint an API key from the supplied
    # login credentials (so the user doesn't do a multi-step key dance).
    auto_provision_key: bool = False
    # --- Packaged-integration metadata (each integration is a self-contained
    # "mini app": it declares its own entitlement, capabilities and lifecycle so
    # nothing about it is hard-coded into core/platform files) ---
    version: str = "1.0.0"
    status: str = "ga"          # ga | preview | coming_soon
    # Plans this integration is available on ([] = every plan).
    plans: List[str] = field(default_factory=list)
    min_plan: str = ""          # informational badge, e.g. "business"
    # Capability bundles the package provides (managed integrations declare these).
    capabilities: List[str] = field(default_factory=list)
    # Ownership models supported: user | organization | managed_user.
    ownership_models: List[str] = field(default_factory=lambda: ["user"])
    # Managed = org-admin-governed (org-level) integration, not a personal source.
    managed: bool = False
    # Has its own multi-view workspace UI (vs a single detail page).
    workspace: bool = False
    # Extra entitlement flag required beyond the plan (resolved per user/tenant).
    feature_flag: str = ""
    # Help Center slug for this integration's docs.
    docs_slug: str = ""


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
