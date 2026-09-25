"""Google Workspace — directory discovery via domain-wide delegation.

Discovers the organization's Workspace users into ``GwExternalIdentity`` rows so an
identity-admin can map them to Arkive users (mirrors the M365 Entra discovery).
Runs where the integration instance lives: the control plane for CP-hosted tenants,
or the assigned customer node for federated tenants (discovered identities then
replicate up to the CP for the portal).

Auth: a service account with domain-wide delegation. The reusable key is stored
encrypted (credstore) on the box that owns the instance and NEVER in the model
tables; the Admin SDK client impersonates ``subject_admin`` for directory reads.
All Google libraries are imported lazily so a missing dependency or an
unconfigured instance degrades cleanly instead of breaking the app.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import models as m

logger = logging.getLogger("cv.integrations.google_workspace.discovery")

INTEGRATION_TYPE = "google_workspace"

# Read-only scopes the delegated service account needs. Directory is required for
# discovery; the per-workload scopes are used later by collection.
DIRECTORY_SCOPE = "https://www.googleapis.com/auth/admin.directory.user.readonly"
WORKLOAD_SCOPES = {
    "gmail": "https://www.googleapis.com/auth/gmail.readonly",
    "google_drive": "https://www.googleapis.com/auth/drive.readonly",
    "google_calendar": "https://www.googleapis.com/auth/calendar.readonly",
    "google_contacts": "https://www.googleapis.com/auth/contacts.readonly",
    "google_photos": "https://www.googleapis.com/auth/photoslibrary.readonly",
}


class DirectoryError(RuntimeError):
    """Actionable directory error (status + reason preserved for logging)."""

    def __init__(self, message: str, *, status: int = 0, reason: str = ""):
        super().__init__(message)
        self.status = status
        self.reason = reason


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def load_service_account(db: Session, inst) -> dict | None:
    """Load the decrypted service-account key JSON for an instance, or None when
    the integration isn't configured yet (so callers can degrade cleanly). The key
    lives in the instance's encrypted ``credentials`` blob (credstore), never in a
    model table."""
    if not getattr(inst, "credentials", None):
        return None
    try:
        from ... import credstore
        creds = credstore.decrypt(inst.tenant_id, inst.credentials)
    except Exception:  # noqa: BLE001 — a bad/rotated KEK must not crash discovery
        logger.warning("gw: could not decrypt credentials for instance=%s", inst.id)
        return None
    raw = creds.get("service_account_json")
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("gw: service-account key for instance=%s is not valid JSON", inst.id)
        return None


def build_directory_service(sa_info: dict, subject: str):
    """Build an Admin SDK Directory client impersonating ``subject`` (an admin).
    Google libraries are imported lazily."""
    try:
        from google.oauth2 import service_account  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise DirectoryError(f"google client libraries unavailable: {exc}") from exc
    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=[DIRECTORY_SCOPE], subject=subject)
    return build("admin", "directory_v1", credentials=creds, cache_discovery=False)


def _in_scope(ident: "m.GwExternalIdentity", rules: dict) -> tuple[bool, str]:
    """Deterministic scope decision. Precedence: explicit exclude > guest gate >
    explicit include list > OU allow-list > domain allow-list > default (active
    members). Mirrors the M365 scope precedence."""
    rules = rules or {}
    email = (ident.primary_email or "").lower()
    uid = (ident.google_user_id or "").lower()
    excludes = {str(x).lower() for x in (rules.get("excludes") or [])}
    if email in excludes or uid in excludes:
        return False, "excluded"
    if (ident.user_type or "member").lower() == "guest" and not rules.get("include_guests"):
        return False, "guest"
    includes = [str(x).lower() for x in (rules.get("includes") or [])]
    if includes:
        hit = email in includes or uid in includes
        return hit, "included" if hit else "not_in_include_list"
    ous = [str(o).lower() for o in (rules.get("org_units") or [])]
    if ous:
        path = (ident.org_unit_path or "").lower()
        hit = any(path == o or path.startswith(o.rstrip("/") + "/") for o in ous)
        return hit, "org_unit" if hit else "not_in_org_unit"
    domains = [str(d).lower().lstrip("@") for d in (rules.get("domains") or [])]
    if domains:
        dom = email.split("@")[-1] if "@" in email else ""
        hit = dom in domains
        return hit, "domain" if hit else "not_in_domain"
    if ident.suspended:
        return False, "suspended"
    return True, "active_member"


def run_discovery(db: Session, inst) -> int:
    """Discover Workspace directory users into GwExternalIdentity rows. Returns the
    number of identities upserted. Degrades cleanly (returns 0, records a needs-
    config note) when the service account isn't configured."""
    sa_info = load_service_account(db, inst)
    cred = (db.query(m.GwManagedCredential)
            .filter(m.GwManagedCredential.integration_instance_id == inst.id).first())
    subject = (cred.subject_admin if cred else "") or ""
    if not sa_info or not subject:
        logger.info("gw discovery skipped (instance=%s): service account / subject admin not configured", inst.id)
        return 0

    rules = {}
    pol = (db.query(m.GwScopePolicy)
           .filter(m.GwScopePolicy.integration_instance_id == inst.id).first())
    if pol:
        rules = pol.rules or {}
    customer_id = (cred.customer_id if cred else "") or "my_customer"

    try:
        svc = build_directory_service(sa_info, subject)
    except DirectoryError as exc:
        logger.warning("gw discovery: cannot build directory client (instance=%s): %s", inst.id, exc)
        return 0

    seen = 0
    page_token = None
    try:
        while True:
            resp = (svc.users().list(customer=customer_id, maxResults=200,
                                     orderBy="email", pageToken=page_token,
                                     projection="basic", viewType="admin_view").execute())
            for u in resp.get("users", []):
                seen += _upsert_identity(db, inst, cred, u, rules)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        db.commit()
    except Exception as exc:  # noqa: BLE001 — surface as actionable, never silent
        status = getattr(getattr(exc, "resp", None), "status", 0) or 0
        logger.exception("gw discovery failed (instance=%s, status=%s)", inst.id, status)
        raise DirectoryError(f"directory list failed: {exc}", status=int(status)) from exc

    logger.info("gw discovery (instance=%s): %d identit(y/ies) upserted", inst.id, seen)
    return seen


def _upsert_identity(db: Session, inst, cred, u: dict, rules: dict) -> int:
    customer_id = (cred.customer_id if cred else "") or (u.get("customerId") or "")
    gid = str(u.get("id") or "")
    if not gid:
        return 0
    row = (db.query(m.GwExternalIdentity)
           .filter(m.GwExternalIdentity.tenant_id == inst.tenant_id,
                   m.GwExternalIdentity.customer_id == customer_id,
                   m.GwExternalIdentity.google_user_id == gid).first())
    if row is None:
        row = m.GwExternalIdentity(tenant_id=inst.tenant_id,
                                   integration_instance_id=inst.id,
                                   customer_id=customer_id, google_user_id=gid)
        db.add(row)
    row.primary_email = u.get("primaryEmail") or row.primary_email
    name = u.get("name") or {}
    row.display_name = name.get("fullName") or row.display_name
    row.suspended = bool(u.get("suspended"))
    row.is_admin = bool(u.get("isAdmin") or u.get("isDelegatedAdmin"))
    row.org_unit_path = u.get("orgUnitPath") or ""
    # A user in another domain of the account is treated as external/guest.
    row.user_type = "guest" if not u.get("isEnrolledIn2Sv") and u.get("kind") == "" else row.user_type
    in_scope, reason = _in_scope(row, rules)
    row.in_scope = in_scope
    row.scope_reason = reason
    row.last_seen = _now()
    return 1
