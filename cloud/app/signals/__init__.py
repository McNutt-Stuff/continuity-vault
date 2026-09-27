"""Arkive Signal Platform — shared signal infrastructure.

Collect once, normalize once, correlate once, reuse everywhere. See
docs/adr/0001-signals-are-shared-infrastructure.md.
"""

from __future__ import annotations

from . import engine, taxonomy  # noqa: F401
from .provider_base import (  # noqa: F401
    SignalProvider,
    all_providers,
    get,
    register,
)
from . import providers  # noqa: F401,E402  — importing registers the built-in providers
