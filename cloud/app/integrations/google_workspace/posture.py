"""Google Workspace — compliance posture collector (identity/collaboration).

Mirrors the Microsoft 365 posture collector: reads read-only directory posture via
the domain-wide-delegation service account and records scope-aware ComplianceSignal
rows so the platform compliance engine folds Google Workspace in as evidence —
exactly like Microsoft 365, and IN ADDITION to it (the engine takes the best status
per capability across providers).

Only capabilities we can assess reliably app-only from the Admin SDK Directory API
are emitted; nothing is fabricated. Runs on the box that owns the instance (the CP
for a CP-hosted tenant; the assigned node otherwise).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ...compliance import signals
from . import directory
from . import models as m

logger = logging.getLogger("cv.integrations.google_workspace.posture")

_PROVIDER = "google_workspace"
_MIN_REFRESH = timedelta(hours=6)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _srec(db, inst, cap, status, summary, *, expected=0, covered=0, failed=0,
          evidence_level="observed", detail=None, remediation="", expires_at=None):
    """Record a scope-aware posture signal (populations drive coverage scoring)."""
    signals.record(db, inst.tenant_id, _PROVIDER, cap, status=status, summary=summary,
                   scope_type="integration", scope_id=inst.id, integration_instance_id=inst.id,
                   expected=int(expected or 0), covered=int(covered or 0), failed=int(failed or 0),
                   evidence_level=evidence_level, detail=detail or {}, remediation=remediation,
                   expires_at=expires_at)


def _credential(db: Session, inst) -> "m.GwManagedCredential | None":
    return (db.query(m.GwManagedCredential)
            .filter(m.GwManagedCredential.integration_instance_id == inst.id).first())


def refresh(db: Session, inst, *, force: bool = False) -> int:
    """Collect Google Workspace directory posture into compliance signals. Returns
    the number recorded. Degrades cleanly (returns 0) when the instance isn't
    configured — never fabricates posture."""
    cred = _credential(db, inst)
    sa_info = directory.load_service_account(db, inst)
    subject = (cred.subject_admin if cred else "") or ""
    if not sa_info or not subject:
        return 0
    # Throttle: skip if we assessed recently (unless forced).
    last = signals.latest_at(db, inst.tenant_id, _PROVIDER, capability="mfa")
    if not force and last and (_now() - last) < _MIN_REFRESH:
        return 0
    try:
        svc = directory.build_directory_service(sa_info, subject)
    except directory.DirectoryError as exc:
        logger.warning("gw posture: cannot build directory client (instance=%s): %s", inst.id, exc)
        return 0

    customer = (cred.customer_id if cred else "") or "my_customer"
    total = active = admins = two_sv = 0
    page_token = None
    try:
        while True:
            resp = (svc.users().list(customer=customer, maxResults=200,
                                     pageToken=page_token, projection="full",
                                     viewType="admin_view").execute())
            for u in resp.get("users", []):
                total += 1
                if not u.get("suspended"):
                    active += 1
                if u.get("isAdmin") or u.get("isDelegatedAdmin"):
                    admins += 1
                if u.get("isEnrolledIn2Sv"):
                    two_sv += 1
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    except Exception as exc:  # noqa: BLE001 — never fabricate; log + bail
        logger.warning("gw posture: directory scan failed (instance=%s): %s", inst.id, exc)
        return 0

    n = 0
    # 2-Step Verification enrollment → mfa.
    if active:
        ratio = two_sv / active
        _srec(db, inst, "mfa",
              "met" if ratio >= 0.95 else ("partial" if ratio >= 0.5 else "unmet"),
              f"{two_sv}/{active} active user(s) enrolled in 2-Step Verification",
              expected=active, covered=two_sv, failed=active - two_sv,
              remediation="Enforce 2-Step Verification for all users in the Google Admin console.")
        n += 1
    # Privileged (admin) footprint → privileged_access_review.
    if admins:
        _srec(db, inst, "privileged_access_review",
              "met" if admins <= 5 else ("partial" if admins <= 15 else "unmet"),
              f"{admins} account(s) hold a Google Workspace admin role",
              expected=admins, covered=(admins if admins <= 5 else 0),
              failed=(admins if admins > 15 else 0),
              remediation="Keep super-admin roles to a minimum; use scoped admin roles.")
        n += 1
    # Admin + login audit logging is always on in Workspace.
    _srec(db, inst, "audit_logging", "met",
          "Google Workspace records admin and login audit logs.",
          evidence_level="configuration")
    n += 1

    db.commit()
    logger.info("gw posture refreshed (instance=%s): %d signal(s)", inst.id, n)
    return n


def refresh_for_tenant(db: Session, tenant) -> None:
    """Refresher entry point registered with the compliance engine — refresh every
    connected Google Workspace instance for this tenant."""
    from ...models import IntegrationInstance
    for inst in (db.query(IntegrationInstance)
                 .filter(IntegrationInstance.tenant_id == tenant.id,
                         IntegrationInstance.integration_type == "google_workspace").all()):
        try:
            refresh(db, inst)
        except Exception:  # noqa: BLE001
            logger.exception("gw posture refresh failed (instance=%s)", inst.id)
