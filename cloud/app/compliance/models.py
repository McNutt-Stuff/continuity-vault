"""Compliance engine — data model (platform-level, integration-driven).

All NEW tables, so SQLAlchemy's ``create_all`` provisions them (no migration) once
this module is imported — it's imported from ``db.init_db`` before ``create_all``.
Everything is tenant-scoped. Posture history (`compliance_snapshots`) and a change
ledger (`compliance_events`) give a trend over time + the details that drove each
change. Evidence rows record WHICH provider satisfied WHICH capability.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text

from ..db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CompliancePack(Base):
    """A framework a tenant has enabled the engine to assess against."""

    __tablename__ = "compliance_packs"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    framework = Column(String, nullable=False, index=True)  # nist_csf|cis|hipaa|…
    version = Column(String, default="")
    enabled = Column(Boolean, default=True)
    config = Column(JSON, default=dict)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class ComplianceControl(Base):
    """A tenant's live state for one framework control."""

    __tablename__ = "compliance_controls"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    pack_id = Column(String, index=True, nullable=False)
    framework = Column(String, index=True, default="")
    control_id = Column(String, default="", index=True)
    title = Column(String, default="")
    family = Column(String, default="")
    # not_assessed|not_applicable|planned|partially_implemented|implemented|operating|exception|failed
    state = Column(String, default="not_assessed", index=True)
    score = Column(Integer, default=0)              # 0..100 for this control
    owner = Column(String, nullable=True)
    auto = Column(Boolean, default=True)            # engine-assessed vs. manual override
    capabilities = Column(JSON, default=list)       # capability keys this control needs
    last_evaluated_at = Column(DateTime, nullable=True)
    meta = Column(JSON, default=dict)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class ComplianceEvidence(Base):
    """A provider's assessment of one capability for one control (why a control
    is met / partial / unmet). Regenerated each evaluation."""

    __tablename__ = "compliance_evidence"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    control_id = Column(String, index=True, nullable=False)  # -> ComplianceControl.id
    capability = Column(String, default="", index=True)
    provider = Column(String, default="", index=True)        # arkive|microsoft365|…
    status = Column(String, default="unknown")               # met|partial|unmet|not_applicable|unknown
    summary = Column(String, default="")
    detail = Column(JSON, default=dict)                       # non-secret signal detail
    observed_at = Column(DateTime, default=_now)


class ComplianceException(Base):
    """A time-boxed, audited exception for a control."""

    __tablename__ = "compliance_exceptions"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    control_id = Column(String, index=True, nullable=False)  # -> ComplianceControl.id
    reason = Column(Text, default="")
    approved_by = Column(String, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now)


class ComplianceSnapshot(Base):
    """Point-in-time posture per framework — powers the trend / improvement view."""

    __tablename__ = "compliance_snapshots"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    framework = Column(String, index=True, default="")
    score = Column(Integer, default=0)
    controls_total = Column(Integer, default=0)
    controls_met = Column(Integer, default=0)
    exceptions = Column(Integer, default=0)
    # The scoring algorithm version that produced this score — so a calculation
    # change is a labeled boundary in the trend, never a silent rewrite of history.
    scoring_version = Column(String, default="")
    # Framework-level coverage rollup (sum over controls' in-scope populations).
    coverage_expected = Column(Integer, default=0)
    coverage_covered = Column(Integer, default=0)
    coverage_failed = Column(Integer, default=0)
    taken_at = Column(DateTime, default=_now, index=True)


class ComplianceEvent(Base):
    """Change ledger — WHAT changed + the detail that drove it (posture history)."""

    __tablename__ = "compliance_events"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    framework = Column(String, index=True, default="")
    control_id = Column(String, index=True, default="")      # framework control id (human), optional
    kind = Column(String, default="")   # pack_enabled|pack_disabled|control_state|exception|reassessed|evidence_change
    actor = Column(String, default="")
    summary = Column(String, default="")
    detail = Column(JSON, default=dict)
    created_at = Column(DateTime, default=_now, index=True)


class ComplianceSignal(Base):
    """A durable, integration/source-recorded posture SIGNAL that maps to a
    capability (e.g. Microsoft 365 MFA/conditional-access/DLP/external-sharing/
    residency). ANY integration or source records signals via ``compliance.signals``;
    the engine's ``_integration_signals`` provider surfaces them as evidence, so a
    new integration contributes to compliance without touching the framework math.

    Keyed by (tenant, provider, capability, scope) — one live signal per slot,
    upserted on each refresh."""

    __tablename__ = "compliance_signals"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    provider = Column(String, default="", index=True)   # microsoft365|ubiquiti|desktop|…
    capability = Column(String, default="", index=True)  # -> registry.CAPABILITIES key
    scope = Column(String, default="")                   # slot key (e.g. scope_type:scope_id or a workload/site)
    status = Column(String, default="unknown")           # met|partial|unmet|not_applicable|unknown
    summary = Column(String, default="")
    detail = Column(JSON, default=dict)                  # non-secret signal detail
    observed_at = Column(DateTime, default=_now, index=True)
    updated_at = Column(DateTime, default=_now, onupdate=_now)
    # --- Scoped evidence contract (spec §3.2). All additive + nullable so existing
    # signals keep working; a driver fills what it can measure. NEVER secrets/PII. ---
    integration_instance_id = Column(String, default="", index=True)  # which M365/etc. instance
    scope_type = Column(String, default="")   # organization|workload|user|mailbox|site|drive|team|agent|destination
    scope_id = Column(String, default="")     # stable id within scope_type (entra_object_id, site id, …)
    # Coverage: evaluate EVERY required in-scope entity (not best-status-wins).
    expected_population = Column(Integer, default=0)   # in-scope entities that SHOULD be covered
    covered_population = Column(Integer, default=0)    # of those, actually covered/met
    failed_population = Column(Integer, default=0)     # of those, failing/unprotected
    # Evidence provenance + confidence — configuration ≠ observed ≠ verified_test ≠ manual.
    evidence_level = Column(String, default="observed")  # configuration|observed|verified_test|manual
    policy_version = Column(String, default="")          # org policy version this was compared against
    evidence_ref = Column(String, default="")            # bounded reference/hash of the underlying evidence
    # Freshness: unknown/expired evidence NEVER counts as met.
    expires_at = Column(DateTime, nullable=True)         # stale after this; NULL = no explicit deadline
    entities = Column(JSON, default=list)                # affected entities [{kind,label,status,note}] (non-secret)
    remediation = Column(String, default="")             # remediation link/text


class ComplianceAttestation(Base):
    """A self-attestation for a capability the platform can't automatically evidence
    (a written policy / procedure / program). Admin-answered, audited, with a review
    cadence and an optional link to proof (a policy document). One row per
    (tenant, capability); the ``attestation`` provider turns it into manual evidence.
    An attestation past its ``review_due_at`` is treated as stale (unknown)."""

    __tablename__ = "compliance_attestations"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    capability = Column(String, nullable=False, index=True)  # -> registry.CAPABILITIES (attestable) key
    status = Column(String, default="unmet")     # met|partial|unmet|not_applicable
    note = Column(Text, default="")              # how it's satisfied / scope (non-secret)
    evidence_url = Column(String, default="")    # link to the policy/proof (doc upload lands in 1b)
    attested_by = Column(String, default="")     # user email/id who attested
    attested_at = Column(DateTime, default=_now)
    review_due_at = Column(DateTime, nullable=True)  # attestation goes stale (unknown) after this
    updated_at = Column(DateTime, default=_now, onupdate=_now)

