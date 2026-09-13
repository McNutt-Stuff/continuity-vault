"""Integrations framework.

Integrations are self-contained, extensible packages ("mini apps") that plug into
the platform without core code needing to know about any specific one. Each lives
in its own sub-package under ``cloud/app/integrations/<name>/`` and self-registers
on import. Adding an integration is therefore a new directory — no edits to core
files, routers, or this module.

Two kinds ship today:
- auxiliary-intelligence integrations (e.g. UniFi) that run on the customer's
  appliance and report network/app telemetry;
- managed, organization-level integrations (e.g. Microsoft 365) that are governed
  by an org admin and run on the assigned customer node.
"""

from __future__ import annotations

import importlib
import logging
import os
import pkgutil

from .base import (
    CredentialField,
    Integration,
    IntegrationSpec,
    all_integrations,
    get_integration,
    register_integration,
)

logger = logging.getLogger("cv.integrations")


def _discover() -> None:
    """Import every integration sub-package so it self-registers. A package is any
    child directory with an ``__init__.py`` that imports its ``integration`` module."""
    pkg_dir = os.path.dirname(__file__)
    for mod in pkgutil.iter_modules([pkg_dir]):
        if not mod.ispkg or mod.name.startswith("_"):
            continue
        try:
            importlib.import_module(f"{__name__}.{mod.name}")
        except Exception:  # noqa: BLE001 — one bad package must not break the others
            logger.exception("integration package failed to load: %s", mod.name)


_discover()

__all__ = [
    "CredentialField",
    "Integration",
    "IntegrationSpec",
    "all_integrations",
    "get_integration",
    "register_integration",
]
