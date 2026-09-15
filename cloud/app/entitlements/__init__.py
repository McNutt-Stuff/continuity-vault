"""Entitlements — the layer between the commercial model (plans/add-ons/contracts)
and enforcement (feature flags, seat limits). See ``registry`` (definitions),
``engine`` (derivation + service API) and ``models`` (admin overrides)."""

from __future__ import annotations

from . import models  # noqa: F401  register tables for create_all
from . import registry  # noqa: F401
from . import engine  # noqa: F401

# Re-export the service API so callers do `from ..entitlements import has_entitlement`.
from .engine import (  # noqa: E402,F401
    derive, has_entitlement, get_limit, get_usage, can_consume,
    require_seat, enforcement_on, view,
)
