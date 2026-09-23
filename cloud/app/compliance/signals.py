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
           scope: str = "",
           # --- scoped evidence contract (spec §3.2), all optional/back-compat ---
           scope_type: str = "", scope_id: str = "",
           integration_instance_id: str = "",
           expected: int = 0, covered: int = 0, failed: int = 0,
           evidence_level: str = "observed", policy_version: str = "",
           evidence_ref: str = "", expires_at: datetime | None = None,
           entities: list | None = None, remediation: str = "") -> m.ComplianceSignal:
    """Upsert one posture signal. ``status`` ∈ met|partial|unmet|not_applicable|unknown.

    The slot key is (tenant, provider, capability, scope) — pass an explicit
    ``scope`` OR a structured ``scope_type``/``scope_id`` (the slot derives from
    ``scope_type:scope_id``). Populations describe the in-scope coverage so the
    engine can aggregate by scope instead of best-status-wins. ``evidence_level``
    keeps configuration/observed/verified_test/manual distinct. ``expires_at`` marks
    the freshness deadline — an expired signal is treated as ``unknown`` by readers.
    NEVER put secrets/PII/payloads in ``detail``/``entities``/``remediation``."""
    slot = scope or (f"{scope_type}:{scope_id}" if (scope_type or scope_id) else "")
    row = (db.query(m.ComplianceSignal)
           .filter(m.ComplianceSignal.tenant_id == tenant_id,
                   m.ComplianceSignal.provider == provider,
                   m.ComplianceSignal.capability == capability,
                   m.ComplianceSignal.scope == slot).first())
    if row is None:
        row = m.ComplianceSignal(tenant_id=tenant_id, provider=provider,
                                 capability=capability, scope=slot)
        db.add(row)
    row.status = status
    row.summary = (summary or "")[:400]
    row.detail = detail or {}
    row.observed_at = _now()
    row.scope_type = scope_type or ""
    row.scope_id = scope_id or ""
    row.integration_instance_id = integration_instance_id or ""
    row.expected_population = int(expected or 0)
    row.covered_population = int(covered or 0)
    row.failed_population = int(failed or 0)
    row.evidence_level = evidence_level or "observed"
    row.policy_version = policy_version or ""
    row.evidence_ref = (evidence_ref or "")[:200]
    row.expires_at = expires_at
    row.entities = entities or []
    row.remediation = (remediation or "")[:400]
    return row


def is_expired(sig: m.ComplianceSignal, now: datetime | None = None) -> bool:
    """True when the signal's freshness deadline has passed (readers treat it as
    ``unknown`` — spec: expired evidence never counts as met)."""
    exp = getattr(sig, "expires_at", None)
    return bool(exp and exp < (now or _now()))


def effective_status(sig: m.ComplianceSignal, now: datetime | None = None) -> str:
    """The status a reader should use — the stored status, downgraded to
    ``unknown`` once the evidence is expired/stale."""
    return "unknown" if is_expired(sig, now) else (sig.status or "unknown")


def latest_at(db: Session, tenant_id: str, provider: str,
              capability: str | None = None, scope: str | None = None) -> datetime | None:
    """Most recent signal timestamp for a provider — OPTIONALLY narrowed to a
    capability and scope. Throttle per (provider, capability, scope), NEVER on a
    single latest timestamp across all capabilities/instances (spec §3.2)."""
    q = (db.query(m.ComplianceSignal)
         .filter(m.ComplianceSignal.tenant_id == tenant_id,
                 m.ComplianceSignal.provider == provider))
    if capability is not None:
        q = q.filter(m.ComplianceSignal.capability == capability)
    if scope is not None:
        q = q.filter(m.ComplianceSignal.scope == scope)
    row = q.order_by(m.ComplianceSignal.observed_at.desc()).first()
    return row.observed_at if row else None


def for_tenant(db: Session, tenant_id: str) -> list[m.ComplianceSignal]:
    return (db.query(m.ComplianceSignal)
            .filter(m.ComplianceSignal.tenant_id == tenant_id).all())
