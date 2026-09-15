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


def register_provider(name: str, fn: Callable) -> None:
    """Register an evidence provider (idempotent). Integrations call this at import."""
    _PROVIDERS[name] = fn


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
    admins = int(db.query(func.count(User.id))
                 .filter(User.tenant_id == tid, User.role.in_(("admin", "owner"))).scalar() or 0)
    admins_pk = 0
    try:
        admins_pk = int(db.query(func.count(func.distinct(Passkey.user_id)))
                        .join(User, User.id == Passkey.user_id)
                        .filter(User.tenant_id == tid,
                                User.role.in_(("admin", "owner"))).scalar() or 0)
    except Exception:  # noqa: BLE001
        admins_pk = 0
    add("access_control", "met",
        "Cross-member access is gated by flag, dual-controlled and audited; least privilege by vault owner.")
    if admins:
        add("mfa", "met" if admins_pk >= admins else ("partial" if admins_pk else "unmet"),
            f"{admins_pk}/{admins} admin(s) enrolled in passkeys (phishing-resistant MFA)",
            admins=admins, admins_with_passkey=admins_pk)
    else:
        add("mfa", "unknown", "No org admins to assess")

    # Audit logging + monitoring — always on.
    add("audit_logging", "met", "Tamper-evident audit chain records access + admin actions (Platform Logs).")
    add("monitoring", "met", "Source failures + anomalies raise notifications and admin alerts.")
    add("incident_response", "met", "Failures surface as source-problem notifications with tenant attribution.")

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
    rules_on = features.resolve(db.get(User, tenant_admin_id(db, tid)), tenant, "rules_enabled") if tid else False
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
