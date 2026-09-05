"""Tamper-evident, hash-chained audit ledger (spec 2.5, 14)."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

from sqlalchemy.orm import Session

from cv_crypto.provider import hexdigest

from .models import AuditEvent

# Per-request marker (a shared mutable dict, so it survives the sync threadpool):
# record() flips it True, and the universal activity middleware skips a generic
# entry when a rich audit entry was already written for this request.
_request_audited: ContextVar[Optional[dict]] = ContextVar("cv_request_audited", default=None)


# Action → (category, severity) classification. Anything not listed defaults to
# ("activity", "info"). Failures/anomalies are bumped to warning/critical so the
# audit log can surface abnormal usage and credential access at a glance.
_CLASSIFY: dict[str, tuple[str, str]] = {
    # Authentication & step-up
    "auth.login": ("security", "notice"),
    "auth.login_failed": ("security", "warning"),
    "auth.logout": ("security", "info"),
    "auth.passkey_registered": ("security", "notice"),
    "auth.stepup": ("security", "notice"),
    "auth.stepup_failed": ("security", "warning"),
    # Credential / secret access
    "search.retrieve": ("credential", "notice"),
    "connector.credentials_accessed": ("credential", "notice"),
    "restore.requested": ("credential", "notice"),
    "restore.approved": ("credential", "notice"),
    "restore.executed": ("credential", "warning"),
    # Connector / source lifecycle
    "connector.linked": ("activity", "notice"),
    "connector.unlinked": ("activity", "notice"),
    "connector.reauth_required": ("security", "warning"),
    # Admin / fleet
    "agent.command": ("admin", "notice"),
    "appliance.command": ("admin", "notice"),
    "appliance.quarantined": ("security", "critical"),
    "appliance.attestation_failed": ("security", "critical"),
    # Backups / sync
    "backup.completed": ("activity", "info"),
    "backup.failed": ("system", "warning"),
    "agent.ingest": ("activity", "info"),
}


def classify(action: str) -> tuple[str, str]:
    if action in _CLASSIFY:
        return _CLASSIFY[action]
    # Heuristics for actions not explicitly mapped.
    if action.endswith("_failed") or action.endswith(".failed"):
        return ("system", "warning")
    if action.startswith("auth.") or action.startswith("security."):
        return ("security", "notice")
    if action.startswith("admin.") or action.endswith(".command"):
        return ("admin", "notice")
    return ("activity", "info")


def record(db: Session, actor: str, action: str, tenant_id: Optional[str] = None,
           resource: str = "", detail: Optional[dict] = None,
           category: Optional[str] = None, severity: Optional[str] = None) -> AuditEvent:
    default_cat, default_sev = classify(action)
    category = category or default_cat
    severity = severity or default_sev
    last = (
        db.query(AuditEvent)
        .order_by(AuditEvent.created_at.desc())
        .first()
    )
    prev_hash = last.entry_hash if last else ""
    body = f"{prev_hash}|{actor}|{action}|{resource}|{detail}"
    entry_hash = hexdigest(body.encode())
    event = AuditEvent(
        tenant_id=tenant_id,
        actor=actor,
        action=action,
        resource=resource,
        detail=detail or {},
        category=category,
        severity=severity,
        prev_hash=prev_hash,
        entry_hash=entry_hash,
    )
    db.add(event)
    db.commit()
    flag = _request_audited.get()
    if flag is not None:
        flag["audited"] = True
    # Dual-write into the unified log store so user actions / auth / audits appear
    # in the one platform Logs view (never let a logging failure break the audit).
    try:
        from . import logsink
        _sev_lvl = {"info": "info", "notice": "info", "warning": "warning",
                    "critical": "critical", "error": "error"}
        src = ("auth" if action.startswith("auth.")
               else "audit" if category == "admin" else "activity")
        logsink.emit(level=_sev_lvl.get(severity, "info"), source=src,
                     logger_name=f"audit.{category}", message=f"{action} {resource}".strip(),
                     tenant_id=tenant_id, actor=actor, resource=resource,
                     meta={"action": action, "category": category})
    except Exception:  # noqa: BLE001
        pass
    return event


def verify_chain(db: Session) -> bool:
    prev = ""
    for event in db.query(AuditEvent).order_by(AuditEvent.created_at.asc()).all():
        body = f"{prev}|{event.actor}|{event.action}|{event.resource}|{event.detail}"
        if hexdigest(body.encode()) != event.entry_hash:
            return False
        prev = event.entry_hash
    return True
