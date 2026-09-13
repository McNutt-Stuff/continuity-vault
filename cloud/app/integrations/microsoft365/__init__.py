"""Microsoft 365 Managed Integration package (self-contained).

A managed, organization-level integration (Business/Enterprise) that turns
Microsoft Entra ID into the discovery + lifecycle source for Arkive org users and
centrally governs protection of Microsoft 365 data (Exchange, OneDrive, SharePoint,
Teams). See docs/roadmaps/m365-managed-integration.md for the phased build plan.

Importing this package self-registers the integration spec so it appears (gated)
in the Integrations catalog. Collection/OAuth/discovery workers are added in later
phases; the spec ships as ``status="coming_soon"`` until then so setup is blocked
with a clear message rather than a broken flow.
"""

from __future__ import annotations

from . import models  # noqa: F401  register the package's ORM tables (create_all)
from . import integration  # noqa: F401  triggers self-registration

__all__ = ["integration", "models"]
