"""Compliance evidence providers.

A *provider* inspects live platform (or integration) state and reports a status
per capability. The engine aggregates every registered provider's evidence, then
each control's state is derived from the capabilities it needs.

Provider contract:
    provider(db, tenant, scope) -> list[CapabilityEvidence]
where scope is an opaque dict (reserved). ``arkive`` is always registered;
integrations register their own driver via ``register_provider`` at import time
(see ``integrations/microsoft365/compliance_driver.py``).

Never return secrets in ``detail``. Status is one of:
    "met" | "partial" | "unmet" | "not_applicable" | "unknown".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from sqlalchemy.orm import Session

logger = logging.getLogger("cv.compliance")


@dataclass
class CapabilityEvidence:
    capability: str
    status: str = "unknown"          # met|partial|unmet|not_applicable|unknown
    summary: str = ""
    provider: str = "arkive"
    detail: dict = field(default_factory=dict)


# provider name -> callable(db, tenant, scope) -> list[CapabilityEvidence]
_PROVIDERS: dict[str, Callable] = {}
# refresher name -> callable(db, tenant) that COLLECTS posture into ComplianceSignal
# rows (e.g. an integration fetching MFA/DLP/sharing/residency). Run before evidence
# collection so the generic signals provider surfaces fresh values.
_REFRESHERS: dict[str, Callable] = {}


def register_provider(name: str, fn: Callable) -> None:
    """Register an evidence provider (idempotent). Integrations call this at import."""
    _PROVIDERS[name] = fn


def register_refresher(name: str, fn: Callable) -> None:
    """Register a posture refresher: collects live signals into ComplianceSignal.
    Integrations register one to contribute MFA/DLP/sharing/residency/etc."""
    _REFRESHERS[name] = fn


def refresh_all(db: Session, tenant) -> None:
    """Run every registered refresher (best-effort) so signals are current."""
    for name, fn in _REFRESHERS.items():
        try:
            fn(db, tenant)
        except Exception:  # noqa: BLE001 — a refresher must never break evaluation
            logger.exception("compliance refresher %s failed (tenant=%s)", name, getattr(tenant, "id", "?"))


def providers() -> dict[str, Callable]:
    return dict(_PROVIDERS)


# --------------------------------------------------------------------------- #
# Arkive core provider — platform-wide evidence every tenant gets.            #
# --------------------------------------------------------------------------- #
def _arkive_core(db: Session, tenant, scope: dict) -> list[CapabilityEvidence]:
    from ..models import (Collection, ConnectorAccount, SnapshotReceipt,
                          SearchDocument, User, Passkey)
    from sqlalchemy import func

    tid = tenant.id
    ev: list[CapabilityEvidence] = []

    def add(cap, status, summary, **detail):
        ev.append(CapabilityEvidence(capability=cap, status=status, summary=summary,
                                     provider="arkive", detail=detail))

    # Inventory — sources/accounts known.
    sources = int(db.query(func.count(ConnectorAccount.id))
                  .filter(ConnectorAccount.tenant_id == tid).scalar() or 0)
    try:
        from ..integrations.microsoft365 import models as _m365m
        sources += int(db.query(func.count(_m365m.ManagedSource.id))
                       .filter(_m365m.ManagedSource.tenant_id == tid).scalar() or 0)
    except Exception:  # noqa: BLE001
        pass
    add("inventory", "met" if sources else "unmet",
        f"{sources} protected source(s) inventoried" if sources else "No sources connected",
        sources=sources)

    # Backup coverage + recovery — receipts / indexed objects exist.
    objects = int(db.query(func.count(SearchDocument.id))
                  .filter(SearchDocument.tenant_id == tid).scalar() or 0)
    receipts = int(db.query(func.count(SnapshotReceipt.id))
                   .filter(SnapshotReceipt.tenant_id == tid).scalar() or 0)
    add("backup_coverage", "met" if objects else ("partial" if sources else "unmet"),
        f"{objects:,} objects protected across {sources} source(s)" if objects
        else ("Sources connected; first backup pending" if sources else "No backups yet"),
        objects=objects, receipts=receipts)
    recoverable = int(db.query(func.count(SnapshotReceipt.id))
                      .filter(SnapshotReceipt.tenant_id == tid,
                              SnapshotReceipt.recoverable.is_(True)).scalar() or 0)
    add("recovery", "met" if recoverable else ("partial" if objects else "unmet"),
        f"{recoverable:,} recoverable recovery point(s)" if recoverable
        else "No confirmed recovery points yet", recoverable=recoverable)

    # Encryption — always on (quantum-safe client/server encryption).
    add("encryption_at_rest", "met",
        "Protected data is encrypted at rest with Arkive's quantum-safe cipher (ciphertext only in storage).")
    add("encryption_in_transit", "met", "All transfers use TLS; agents push client-encrypted.")
    add("immutability", "met",
        "Snapshots are sealed with a signed manifest; the audit ledger is a tamper-evident hash chain.")

    # Access control + MFA (passkeys) — gating is a platform invariant; MFA measured.
    admin_users = (db.query(User)
                   .filter(User.tenant_id == tid, User.role.in_(("admin", "owner"))).all())
    admins = len(admin_users)
    pk_user_ids: set = set()
    try:
        for (uid,) in (db.query(func.distinct(Passkey.user_id))
                       .join(User, User.id == Passkey.user_id)
                       .filter(User.tenant_id == tid,
                               User.role.in_(("admin", "owner"))).all()):
            pk_user_ids.add(uid)
    except Exception:  # noqa: BLE001
        pk_user_ids = set()
    admins_pk = len(pk_user_ids)
    add("access_control", "met",
        "Cross-member access is gated by flag, dual-controlled and audited; least privilege by vault owner.")
    if admins:
        # Name the specific admin accounts still missing phishing-resistant MFA so the
        # framework drill-down can point at exactly who to enrol.
        no_pk = [{"kind": "account", "label": (u.email or u.display_name or u.id),
                  "status": "unmet", "note": "No passkey enrolled"}
                 for u in admin_users if u.id not in pk_user_ids]
        add("mfa", "met" if admins_pk >= admins else ("partial" if admins_pk else "unmet"),
            f"{admins_pk}/{admins} admin(s) enrolled in passkeys (phishing-resistant MFA)",
            admins=admins, admins_with_passkey=admins_pk, entities=no_pk)
    else:
        add("mfa", "unknown", "No org admins to assess")

    # Audit logging + monitoring — always on.
    add("audit_logging", "met", "Tamper-evident audit chain records access + admin actions (Platform Logs).")
    # Surface sources currently failing so the drill-down names the troubled systems.
    failing = []
    try:
        for a in (db.query(ConnectorAccount)
                  .filter(ConnectorAccount.tenant_id == tid).all()):
            err = (getattr(a, "last_error", "") or "").strip()
            fails = int(getattr(a, "fail_count", 0) or 0)
            if err or fails:
                failing.append({"kind": "source",
                                "label": f"{getattr(a, 'account', '') or a.connector_type} ({a.connector_type})",
                                "status": "unmet" if fails >= 3 else "partial",
                                "note": (err[:120] or f"{fails} recent failure(s)")})
    except Exception:  # noqa: BLE001
        failing = []
    add("monitoring", "partial" if failing else "met",
        (f"{len(failing)} source(s) need attention" if failing
         else "Source failures + anomalies raise notifications and admin alerts."),
        failing=len(failing), entities=failing)
    add("incident_response", "met", "Failures surface as source-problem notifications with tenant attribution.",
        entities=failing)

    # Resilience — version history, offsite redundancy, air-gapped offline copy (3-2-1).
    from ..models import ObjectVersion, Appliance
    versions = 0
    try:
        versions = int(db.query(func.count(ObjectVersion.id))
                       .filter(ObjectVersion.tenant_id == tid).scalar() or 0)
    except Exception:  # noqa: BLE001
        versions = 0
    add("versioning", "met" if objects else "unmet",
        ("Every object is versioned (content-addressed) for point-in-time restore"
         + (f" — {versions:,} version(s) tracked" if versions else "")) if objects
        else "No protected objects yet", versions=versions)

    # An on-prem appliance provides an offline, immutable tier.
    appliances = 0
    try:
        appliances = int(db.query(func.count(Appliance.id))
                         .filter(Appliance.tenant_id == tid).scalar() or 0)
    except Exception:  # noqa: BLE001
        appliances = 0

    # Offsite / redundant copies — distinct storage destinations across collections
    # (cv-cloud, customer S3/BYOS, appliance). Two+ independent locations = offsite.
    dests: set = set()
    try:
        for c in db.query(Collection).filter(Collection.tenant_id == tid).all():
            for d in (c.destinations or []):
                dests.add(str(d))
    except Exception:  # noqa: BLE001
        pass
    if appliances:
        dests.add("appliance")
    if not dests and objects:
        dests.add("cv-cloud")  # default managed destination
    n_dest = len(dests)
    add("offsite_copy", "met" if n_dest >= 2 else ("partial" if n_dest == 1 else "unmet"),
        (f"Protected data is copied to {n_dest} independent destination(s): {', '.join(sorted(dests))}"
         if n_dest else "No storage destinations configured"),
        destinations=sorted(dests))

    # Air-gapped/offline copy — an on-prem appliance. A bonus control:
    # not_applicable (doesn't penalize) when no appliance is deployed.
    if appliances:
        add("air_gapped_copy", "met",
            f"{appliances} on-prem appliance(s) provide an offline, immutable copy.",
            appliances=appliances)
    else:
        add("air_gapped_copy", "not_applicable",
            "No on-prem appliance deployed (add one for an air-gapped offline copy).")

    # Retention + legal hold — legal hold via purge flag; retention if any schedule set.
    schedules = 0
    try:
        schedules = int(db.query(func.count(Collection.id))
                        .filter(Collection.tenant_id == tid,
                                Collection.backup_interval_minutes.isnot(None)).scalar() or 0)
    except Exception:  # noqa: BLE001
        schedules = 0
    add("retention", "partial" if schedules else "unmet",
        f"{schedules} source(s) on a defined backup schedule" if schedules
        else "No explicit retention schedule set", schedules=schedules)
    add("legal_hold", "met",
        "Legal hold is available: clearing purge for a user/tenant blocks deletion of protected data.")

    # Data classification / minimization — driven by the governance rules engine.
    from .. import features
    from ..models import Rule
    rules_on = features.resolve(db.get(User, tenant_admin_id(db, tid)), tenant, "rules_enabled", db) if tid else False
    label_rules = restrict_rules = 0
    if rules_on:
        try:
            for r in db.query(Rule).filter(Rule.tenant_id == tid, Rule.enabled.is_(True)).all():
                acts = {a.get("type") for a in (r.actions or [])}
                if acts & {"label", "tag"}:
                    label_rules += 1
                if acts & {"restrict", "obfuscate", "discard", "no_index"}:
                    restrict_rules += 1
        except Exception:  # noqa: BLE001
            pass
    add("data_classification", "met" if label_rules else ("partial" if rules_on else "unmet"),
        f"{label_rules} labelling rule(s) active" if label_rules
        else ("Rules engine enabled; no labelling rules yet" if rules_on else "Governance rules not enabled"),
        rules_enabled=rules_on, label_rules=label_rules)
    add("data_minimization", "met" if restrict_rules else ("partial" if rules_on else "unmet"),
        f"{restrict_rules} restrict/obfuscate/discard rule(s) active" if restrict_rules
        else ("Rules engine enabled; no minimization rules yet" if rules_on else "Governance rules not enabled"),
        rules_enabled=rules_on, minimization_rules=restrict_rules)
    return ev


def tenant_admin_id(db: Session, tid: str):
    from ..models import User
    u = (db.query(User).filter(User.tenant_id == tid,
                               User.role.in_(("admin", "owner"))).first()
         or db.query(User).filter(User.tenant_id == tid).first())
    return u.id if u else None


register_provider("arkive", _arkive_core)


def _integration_signals(db: Session, tenant, scope: dict) -> list[CapabilityEvidence]:
    """Surface every posture signal integrations/sources have recorded (MFA,
    conditional access, DLP, external sharing, residency, …) as capability evidence.
    This is how ANY integration contributes without editing framework math."""
    from . import signals
    out: list[CapabilityEvidence] = []
    for s in signals.for_tenant(db, tenant.id):
        out.append(CapabilityEvidence(capability=s.capability, status=s.status,
                                      summary=s.summary, provider=s.provider,
                                      detail=s.detail or {}))
    return out


register_provider("integration_signals", _integration_signals)


def collect_evidence(db: Session, tenant, scope: dict | None = None) -> dict[str, list[CapabilityEvidence]]:
    """Run every provider and group evidence by capability. Best status wins per
    capability, but every provider's row is retained for the control drill-down."""
    scope = scope or {}
    by_cap: dict[str, list[CapabilityEvidence]] = {}
    for name, fn in _PROVIDERS.items():
        try:
            for e in fn(db, tenant, scope) or []:
                e.provider = e.provider or name
                by_cap.setdefault(e.capability, []).append(e)
        except Exception:  # noqa: BLE001 — one bad provider must not break the engine
            logger.exception("compliance provider %s failed (tenant=%s)", name, getattr(tenant, "id", "?"))
    return by_cap
