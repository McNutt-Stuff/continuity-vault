"""Arkive Signal Platform — provider framework.

A SignalProvider REUSES data an existing collector already retrieved and emits
normalized Signals — it never makes a second vendor API call. Providers declare
machine-readable capabilities so the UI/coverage can reason about them, and report
their own health so stale telemetry is visible. Future providers (Fortinet, SWG,
Cloud, EDR) plug in by subclassing + register().
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

logger = logging.getLogger("cv.signals.provider")


class SignalProvider:
    provider: str = "base"
    # Machine-readable capability strings, e.g. "network.devices", "identity.mfa".
    _capabilities: tuple[str, ...] = ()
    collector_version: str = "1"

    def capabilities(self) -> list[str]:
        return list(self._capabilities)

    def collect(self, db: Session, tenant) -> dict:
        """Emit signals for one tenant from ALREADY-collected data. Return
        {"signals": int, "objects": int, "errors": int, "last_error": str}.
        Subclasses override; must never raise (the sweep wraps it, but be safe)."""
        return {"signals": 0, "objects": 0, "errors": 0, "last_error": ""}


_REGISTRY: dict[str, SignalProvider] = {}


def register(p: SignalProvider) -> None:
    _REGISTRY[p.provider] = p


def all_providers() -> list[SignalProvider]:
    return list(_REGISTRY.values())


def get(provider: str) -> SignalProvider | None:
    return _REGISTRY.get(provider)
