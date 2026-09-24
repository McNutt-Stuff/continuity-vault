"""Shared helper for beyond-Microsoft compliance integration shells (Phase 3).

Each Phase-3 integration (Google Workspace, Okta, Jamf, Kandji, AWS, Azure, GCP,
Qualys, Tenable, KnowBe4, Proofpoint) ships FIRST as a catalog + compliance shell:
it declares the compliance capabilities it will evidence once its collection
backend lands, and registers a compliance evidence provider so the wiring
(auto-discovery -> provider registry -> engine) is already in place.

Until a live instance can be connected (the spec ships ``status="coming_soon"``),
the provider contributes NO evidence — Arkive never fabricates posture it hasn't
observed. When a connected instance later exists AND records posture signals, those
flow through the standard ``integration_signals`` provider automatically, so a shell
becomes a real driver by adding a collector, not by re-plumbing the engine.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..compliance.providers import CapabilityEvidence, register_provider


def register_shell_driver(integration_type: str, capabilities: dict[str, str]):
    """Register a no-op compliance provider for a Phase-3 integration shell.

    ``capabilities`` maps each Arkive compliance capability key (from
    ``compliance.registry.CAPABILITIES``) to a short description of how this
    integration will evidence it — surfaced in the catalog, docs and roadmap.
    """

    def _evidence(db: Session, tenant, scope: dict) -> list[CapabilityEvidence]:
        from ...models import IntegrationInstance

        connected = (db.query(IntegrationInstance)
                     .filter(IntegrationInstance.tenant_id == tenant.id,
                             IntegrationInstance.integration_type == integration_type)
                     .count())
        if not connected:
            # Not connected (collection backend not shipped) — contribute nothing.
            return []
        # A connected instance's live posture is surfaced by the standard
        # integration_signals provider once a collector records signals; the shell
        # itself still asserts nothing on its own (never a fabricated pass).
        return []

    _evidence.__doc__ = f"{integration_type} compliance shell (no evidence until connected)."
    register_provider(integration_type, _evidence)
    return _evidence
