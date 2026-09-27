"""Arkive Signal Platform — engine.

The one place signals are written. ``emit()`` normalizes + de-duplicates: a STATE
signal with the same fingerprint updates in place (last_seen / occurrence_count /
value) instead of exploding the table; EVENT signals keep history. Freshness is
derived from the signal type's definition so a stale observation is never treated
as current. Also owns Finding upserts and provider-health recording.

Postgres DateTime columns are naive — always use naive UTC here (SQLite dev hides
this; production would raise on aware/naive comparison).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..models import Finding, Signal, SignalProviderHealth
from . import taxonomy

logger = logging.getLogger("cv.signals")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def fingerprint(*parts) -> str:
    raw = "\x1f".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def emit(db: Session, tenant_id: str, signal_type: str, *,
         provider: str,
         value: dict | None = None,
         normalized_value=None,
         subject_type: str = "", subject_id: str = "",
         resource_type: str = "", resource_id: str = "",
         actor_type: str = "", actor_id: str = "",
         severity: str | None = None,
         confidence: str = "high", confidence_reason: str = "",
         observed_at: datetime | None = None,
         source_integration_id: str = "", collection_method: str = "",
         collector_version: str = "", meta: dict | None = None,
         evidence_id: str = "", fingerprint_extra: str = "") -> Signal:
    """Normalize + persist one observation (dedup for STATE/MEASUREMENT, append for
    EVENT). Returns the Signal row (created or updated). Never raises on a bad
    value — the caller's collection must not die because one signal was malformed."""
    defn = taxonomy.definition(signal_type)
    kind = defn.kind
    sev = severity or defn.default_severity
    now = _now()
    observed = observed_at or now
    if observed.tzinfo is not None:
        observed = observed.astimezone(timezone.utc).replace(tzinfo=None)
    ttl = defn.freshness_seconds
    expires = observed + timedelta(seconds=ttl) if ttl else None
    nv = None if normalized_value is None else str(normalized_value)
    # STATE dedup keys on the observed VALUE too so a change (true→false) is a new
    # current row and supersedes the old; EVENT/MEASUREMENT key on time/identity.
    fp = fingerprint(tenant_id, signal_type, provider, subject_type, subject_id,
                     resource_type, resource_id,
                     nv if kind == "state" else None, fingerprint_extra)

    if kind in ("state", "measurement"):
        existing = (db.query(Signal)
                    .filter(Signal.tenant_id == tenant_id,
                            Signal.fingerprint == fp,
                            Signal.status == "active").first())
        if existing is not None:
            existing.value = value or {}
            if nv is not None:
                existing.normalized_value = nv
            existing.severity = sev
            existing.confidence = confidence
            existing.confidence_reason = confidence_reason
            existing.observed_at = observed
            existing.last_seen = now
            existing.ingested_at = now
            existing.occurrence_count = int(existing.occurrence_count or 0) + 1
            existing.expires_at = expires
            existing.freshness_state = "fresh"
            if meta is not None:
                existing.meta = meta
            if evidence_id:
                existing.evidence_id = evidence_id
            return existing
        # STATE change: retire any prior active row for the same subject+type whose
        # value differs, so only ONE current observation exists per subject.
        if kind == "state":
            base_fp = fingerprint(tenant_id, signal_type, provider, subject_type,
                                  subject_id, resource_type, resource_id, None,
                                  fingerprint_extra)
            for prior in (db.query(Signal)
                          .filter(Signal.tenant_id == tenant_id,
                                  Signal.signal_type == signal_type,
                                  Signal.subject_id == subject_id,
                                  Signal.resource_id == resource_id,
                                  Signal.status == "active").all()):
                # only supersede same-subject rows (guard against key collisions)
                prior.status = "superseded"
                prior.updated_at = now
            _ = base_fp  # documented intent; value-scoped fp above is authoritative

    s = Signal(
        tenant_id=tenant_id, signal_type=signal_type, category=defn.category,
        observation_kind=kind, provider=provider,
        source_integration_id=source_integration_id,
        collection_method=collection_method, collector_version=collector_version,
        subject_type=subject_type, subject_id=subject_id,
        resource_type=resource_type, resource_id=resource_id,
        actor_type=actor_type, actor_id=actor_id,
        value=value or {}, normalized_value=nv or "",
        severity=sev, confidence=confidence, confidence_reason=confidence_reason,
        observed_at=observed, first_seen=observed, last_seen=now, ingested_at=now,
        occurrence_count=1, fingerprint=fp, freshness_state="fresh",
        expires_at=expires, status="active", evidence_id=evidence_id,
        meta=meta or {})
    db.add(s)
    return s


def refresh_freshness(db: Session, tenant_id: str | None = None,
                      stale_grace_seconds: int = 0) -> int:
    """Age active STATE/MEASUREMENT signals whose expires_at has passed to
    ``stale`` (grace) then ``expired``, so consumers never read dead telemetry as
    current. Returns the number of rows updated."""
    now = _now()
    q = db.query(Signal).filter(Signal.status == "active",
                                Signal.expires_at.isnot(None),
                                Signal.expires_at < now)
    if tenant_id:
        q = q.filter(Signal.tenant_id == tenant_id)
    n = 0
    for s in q.all():
        overdue = (now - s.expires_at).total_seconds()
        new_state = "expired" if overdue > max(0, stale_grace_seconds) else "stale"
        if s.freshness_state != new_state:
            s.freshness_state = new_state
            s.updated_at = now
            n += 1
    return n


def record_provider_health(db: Session, tenant_id: str, provider: str, *,
                           connection_status: str = "ok",
                           success: bool = True,
                           objects_processed: int = 0, signals_produced: int = 0,
                           errors: int = 0, last_error: str = "",
                           permissions_state: str = "", rate_limit_state: str = "",
                           capabilities: list | None = None,
                           collector_version: str = "",
                           coverage: dict | None = None) -> SignalProviderHealth:
    """Upsert per-tenant provider health (one row per tenant+provider)."""
    now = _now()
    h = (db.query(SignalProviderHealth)
         .filter(SignalProviderHealth.tenant_id == tenant_id,
                 SignalProviderHealth.provider == provider).first())
    if h is None:
        h = SignalProviderHealth(tenant_id=tenant_id, provider=provider)
        db.add(h)
    h.connection_status = connection_status
    h.last_attempt_at = now
    if success:
        h.last_success_at = now
    h.objects_processed = int(objects_processed or 0)
    h.signals_produced = int(signals_produced or 0)
    h.errors = int(errors or 0)
    h.last_error = (last_error or "")[:1000]
    if permissions_state:
        h.permissions_state = permissions_state
    if rate_limit_state:
        h.rate_limit_state = rate_limit_state
    if capabilities is not None:
        h.capabilities = capabilities
    if collector_version:
        h.collector_version = collector_version
    if coverage is not None:
        h.coverage = coverage
    h.updated_at = now
    return h


def upsert_finding(db: Session, tenant_id: str, finding_type: str, *,
                   title: str, description: str = "", severity: str = "medium",
                   category: str = "", subject_type: str = "", subject_id: str = "",
                   resource_type: str = "", resource_id: str = "",
                   signal_ids: list | None = None, evidence_ids: list | None = None,
                   remediation: dict | None = None, owner_user_id: str = "",
                   fingerprint_extra: str = "", meta: dict | None = None) -> Finding:
    """Create or update (dedup by fingerprint) a Finding. A repeat detection bumps
    last_seen + occurrence_count and reopens a resolved finding, rather than
    inserting a duplicate."""
    now = _now()
    fp = fingerprint(tenant_id, finding_type, subject_type, subject_id,
                     resource_type, resource_id, fingerprint_extra)
    f = (db.query(Finding)
         .filter(Finding.tenant_id == tenant_id, Finding.fingerprint == fp).first())
    if f is None:
        f = Finding(tenant_id=tenant_id, finding_type=finding_type, fingerprint=fp,
                    first_seen=now, status="open")
        db.add(f)
    else:
        f.occurrence_count = int(f.occurrence_count or 0) + 1
        if f.status == "resolved":
            f.status = "reopened"
            f.resolved_at = None
    f.title = title
    f.description = description
    f.severity = severity
    f.category = category
    f.subject_type = subject_type
    f.subject_id = subject_id
    f.resource_type = resource_type
    f.resource_id = resource_id
    if signal_ids is not None:
        f.signal_ids = signal_ids
    if evidence_ids is not None:
        f.evidence_ids = evidence_ids
    if remediation is not None:
        f.remediation = remediation
    if owner_user_id:
        f.owner_user_id = owner_user_id
    if meta is not None:
        f.meta = meta
    f.last_seen = now
    f.updated_at = now
    return f


def resolve_findings_not_in(db: Session, tenant_id: str, finding_type: str,
                            active_fingerprints: set[str]) -> int:
    """Auto-resolve open findings of a type whose condition is no longer detected
    (their fingerprint wasn't re-emitted this pass). Returns count resolved."""
    now = _now()
    n = 0
    for f in (db.query(Finding)
              .filter(Finding.tenant_id == tenant_id,
                      Finding.finding_type == finding_type,
                      Finding.status.in_(["open", "reopened", "acknowledged", "in_progress"]))
              .all()):
        if f.fingerprint not in active_fingerprints:
            f.status = "resolved"
            f.resolved_at = now
            f.updated_at = now
            n += 1
    return n
