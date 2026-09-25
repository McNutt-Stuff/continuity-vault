"""Google Workspace — managed content collection via domain-wide delegation.

Delivers admin-provided managed sources that REUSE the existing Google connectors
(gmail, google_drive, google_calendar, google_contacts) but authenticate with a
per-user domain-wide-delegation token minted from the org's service account — no
per-employee sign-in, exactly like the Microsoft 365 managed integration.

Each in-scope mapped user gets a ``GwManagedSource`` + a managed ``Collection``
(source_type = the connector type) in that user's vault, so a managed Google source
looks / feels / acts like any other source everywhere in the portal. Only the
credential acquisition differs: ``run_backup`` mints a fresh delegated token per run
(see ``mint_token_for_collection``) instead of using a personal OAuth account.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import directory
from . import models as m

logger = logging.getLogger("cv.integrations.google_workspace.collect")

# workload -> (connector source_type, label, delegated OAuth scope). All per-user.
_WORKLOADS = {
    "gmail": {"source_type": "gmail", "label": "Gmail",
              "scope": directory.WORKLOAD_SCOPES["gmail"]},
    "google_drive": {"source_type": "google_drive", "label": "Google Drive",
                     "scope": directory.WORKLOAD_SCOPES["google_drive"]},
    "google_calendar": {"source_type": "google_calendar", "label": "Google Calendar",
                        "scope": directory.WORKLOAD_SCOPES["google_calendar"]},
    "google_contacts": {"source_type": "google_contacts", "label": "Google Contacts",
                        "scope": directory.WORKLOAD_SCOPES["google_contacts"]},
}
_DEFAULT_WORKLOADS = ("gmail", "google_drive")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def profile(inst) -> dict:
    """The org's managed-protection profile (which workloads, where, how often)."""
    cfg = inst.config or {}
    prof = dict(cfg.get("managed_profile") or {})
    workloads = [w for w in (prof.get("workloads") or []) if w in _WORKLOADS] or list(_DEFAULT_WORKLOADS)
    dests = [str(d) for d in (prof.get("destinations") or []) if d] or ["cv-cloud"]
    iv = prof.get("backup_interval_minutes")
    try:
        interval = int(iv) if iv not in (None, "") else None
    except (TypeError, ValueError):
        interval = None
    return {"workloads": workloads, "destinations": dests, "backup_interval_minutes": interval}


def _resolve_vault_for(db: Session, tenant_id: str, owner_user_id):
    from ...models import Vault
    vault = None
    if owner_user_id:
        vault = db.query(Vault).filter(Vault.owner_user_id == owner_user_id).first()
    if vault is None:
        vault = db.query(Vault).filter(Vault.tenant_id == tenant_id).first()
    return vault


def _ensure_managed_collection(db: Session, inst, source, vault, prof: dict, user_email: str):
    """Create/refresh the Data Map Collection that protects a managed source on the
    user's behalf — flagged ``managed`` + ``gw_workload`` so the scheduler routes it
    through the shared job path and ``run_backup`` mints a delegated token."""
    from ...models import Collection
    st = _WORKLOADS[source.workload]["source_type"]
    coll = None
    for c in (db.query(Collection)
              .filter(Collection.tenant_id == source.tenant_id,
                      Collection.source_type == st).all()):
        if (c.config or {}).get("gw_source_id") == source.id:
            coll = c
            break
    dests = prof.get("destinations") or ["cv-cloud"]
    interval = prof.get("backup_interval_minutes")
    if coll is None:
        coll = Collection(
            tenant_id=source.tenant_id, vault_id=vault.id, name=source.name,
            source_type=st, destinations=list(dests), backup_interval_minutes=interval,
            config={"gw_source_id": source.id, "managed": True,
                    "gw_workload": source.workload, "gw_instance_id": inst.id,
                    "gw_user_email": user_email})
        db.add(coll)
        db.flush()
    else:
        coll.destinations = list(dests)
        coll.backup_interval_minutes = interval
        coll.name = source.name
        cfg = dict(coll.config or {})
        cfg["gw_user_email"] = user_email
        coll.config = cfg
        if vault is not None and coll.vault_id != vault.id:
            coll.vault_id = vault.id
    return coll


def provision_sources(db: Session, inst) -> int:
    """Establish managed sources + protecting Collections for every in-scope mapped
    identity × selected workload. Idempotent — also pauses sources whose identity or
    workload is no longer in scope/selected (never deletes protected data)."""
    prof = profile(inst)
    workloads = [w for w in prof["workloads"] if w in _WORKLOADS]
    bindings = (db.query(m.GwIdentityBinding)
                .filter(m.GwIdentityBinding.integration_instance_id == inst.id,
                        m.GwIdentityBinding.status.in_(("mapped", "protected_only"))).all())
    created = 0
    kept: set = set()
    for b in bindings:
        ident = db.get(m.GwExternalIdentity, b.external_identity_id)
        if ident is None or not ident.in_scope:
            continue
        vault = _resolve_vault_for(db, inst.tenant_id, b.user_id)
        for w in workloads:
            src = (db.query(m.GwManagedSource)
                   .filter(m.GwManagedSource.integration_instance_id == inst.id,
                           m.GwManagedSource.workload == w,
                           m.GwManagedSource.source_key == ident.google_user_id).first())
            label = f"{_WORKLOADS[w]['label']} — {ident.display_name or ident.primary_email}"
            if src is None:
                src = m.GwManagedSource(
                    tenant_id=inst.tenant_id, integration_instance_id=inst.id,
                    workload=w, ownership_type="managed_user", owner_user_id=b.user_id,
                    source_key=ident.google_user_id, name=label, state="active",
                    assigned_node_id=inst.node_id)
                db.add(src)
                db.flush()
                created += 1
            else:
                src.owner_user_id = b.user_id
                src.name = label
                if src.state in ("planned", "disconnected", "paused_by_admin"):
                    src.state = "active"
            scfg = dict(src.config or {})
            scfg["interval_minutes"] = prof["backup_interval_minutes"]
            scfg["user_email"] = ident.primary_email
            src.config = scfg
            kept.add(src.id)
            if vault is not None:
                _ensure_managed_collection(db, inst, src, vault, prof, ident.primary_email)
    # Pause sources no longer selected/mapped/in-scope (stops collection, keeps data).
    for src in (db.query(m.GwManagedSource)
                .filter(m.GwManagedSource.integration_instance_id == inst.id).all()):
        if src.id not in kept and src.state not in ("decommissioned",):
            src.state = "paused_by_admin"
    db.commit()
    logger.info("gw sources provisioned (instance=%s): +%d (%d active)",
                inst.id, created, len(kept))
    return created


def mint_token_for_collection(db: Session, collection) -> str:
    """Mint a fresh domain-wide-delegation access token for a managed Google
    collection's owner + workload scope. Called by ``run_backup`` right before the
    standard connector fetch, so the connector authenticates as the admin-delegated
    user. Raises on misconfiguration (never returns an empty/None token silently)."""
    from ...models import IntegrationInstance
    cfg = collection.config or {}
    iid = cfg.get("gw_instance_id")
    email = cfg.get("gw_user_email")
    workload = cfg.get("gw_workload")
    if not (iid and email and workload):
        raise RuntimeError("managed Google collection is missing gw_instance_id/user/workload")
    inst = db.get(IntegrationInstance, iid)
    if inst is None:
        raise RuntimeError("Google Workspace instance not found for managed collection")
    sa_info = directory.load_service_account(db, inst)
    if not sa_info:
        raise RuntimeError("Google Workspace service account is not configured")
    scope = _WORKLOADS.get(workload, {}).get("scope")
    if not scope:
        raise RuntimeError(f"unknown Google Workspace workload {workload!r}")
    return directory.impersonated_token(sa_info, email, [scope])
