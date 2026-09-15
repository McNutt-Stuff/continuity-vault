"""Compliance signals — the reusable way any integration or source contributes
posture evidence.

An integration records a signal per capability (e.g. Microsoft 365 MFA coverage,
conditional-access, DLP, external-sharing, residency). The engine's
``providers._integration_signals`` surfaces every recorded signal as evidence, so
a new integration lights up compliance WITHOUT touching the framework math — just
call ``record`` from its refresher (see compliance.instructions.md).

Signals are upserted by (tenant, provider, capability, scope): one live value per
slot. Never store secrets in ``detail``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import models as m


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def record(db: Session, tenant_id: str, provider: str, capability: str, *,
           status: str, summary: str = "", detail: dict | None = None,
           scope: str = "") -> m.ComplianceSignal:
    """Upsert one posture signal. ``status`` ∈ met|partial|unmet|not_applicable|unknown."""
    row = (db.query(m.ComplianceSignal)
           .filter(m.ComplianceSignal.tenant_id == tenant_id,
                   m.ComplianceSignal.provider == provider,
                   m.ComplianceSignal.capability == capability,
                   m.ComplianceSignal.scope == (scope or "")).first())
    if row is None:
        row = m.ComplianceSignal(tenant_id=tenant_id, provider=provider,
                                 capability=capability, scope=scope or "")
        db.add(row)
    row.status = status
    row.summary = (summary or "")[:400]
    row.detail = detail or {}
    row.observed_at = _now()
    return row


def latest_at(db: Session, tenant_id: str, provider: str) -> datetime | None:
    """Most recent signal timestamp for a provider (used to throttle refreshes)."""
    row = (db.query(m.ComplianceSignal)
           .filter(m.ComplianceSignal.tenant_id == tenant_id,
                   m.ComplianceSignal.provider == provider)
           .order_by(m.ComplianceSignal.observed_at.desc()).first())
    return row.observed_at if row else None


def for_tenant(db: Session, tenant_id: str) -> list[m.ComplianceSignal]:
    return (db.query(m.ComplianceSignal)
            .filter(m.ComplianceSignal.tenant_id == tenant_id).all())
