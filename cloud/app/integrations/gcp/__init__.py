"""Google Cloud integration package (Phase 3 compliance shell).

Importing this package self-registers the integration spec (so it appears, gated,
in the Integrations catalog) and its compliance evidence provider. The OAuth /
collection backend lands in a later phase; the spec ships as ``status="coming_soon"``
until then, so setup is blocked with a clear message rather than a broken flow.
"""

from __future__ import annotations

from . import integration  # noqa: F401  triggers self-registration
from . import compliance_driver  # noqa: F401  register the compliance evidence provider

__all__ = ["integration"]
