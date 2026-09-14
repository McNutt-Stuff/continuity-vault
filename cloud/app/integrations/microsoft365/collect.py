"""Microsoft 365 — managed content collection (phase 3).

Admin-level (app-only) collection of each mapped user's Exchange Online mailbox
and OneDrive, REUSING the existing Outlook/OneDrive Graph fetchers
(``connectors.live.stream_outlook`` / ``stream_onedrive``) with a per-user
``resource="users/<id>"`` base. No per-employee sign-in — the organization's
admin-consented app token reads each in-scope user's data into that user's
Arkive vault through the standard ingest pipeline.

Runs where the instance lives (CP for CP-hosted tenants; the node worker is ready
for node-hosted tenants once desired-state federation lands).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import models as m

logger = logging.getLogger("cv.integrations.m365.collect")

# workload -> (source_type used for the managed Collection, index facet/searchable)
_WORKLOADS = {
    "exchange": {"source_type": "outlook", "label": "Exchange Online",
                 "facet": ["from", "folder"], "search": ["from", "subject", "folder"]},
    "onedrive": {"source_type": "onedrive", "label": "OneDrive",
                 "facet": ["mime"], "search": ["mime", "path"]},
}
_DEFAULT_WORKLOADS = ("exchange", "onedrive")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def provision_sources(db: Session, inst) -> int:
    """Establish admin-level managed sources (Exchange + OneDrive) for every
    mapped / protected-only in-scope identity. Idempotent."""
    bindings = (db.query(m.ExternalIdentityBinding)
                .filter(m.ExternalIdentityBinding.integration_instance_id == inst.id,
                        m.ExternalIdentityBinding.status.in_(("mapped", "protected_only"))).all())
    caps = [c for c in ((inst.config or {}).get("capabilities") or []) if c in _WORKLOADS]
    workloads = caps or list(_DEFAULT_WORKLOADS)
    created = 0
    for b in bindings:
        ident = db.get(m.ExternalIdentity, b.external_identity_id)
        if ident is None or not ident.in_scope:
            continue
        for w in workloads:
            src = (db.query(m.ManagedSource)
                   .filter(m.ManagedSource.integration_instance_id == inst.id,
                           m.ManagedSource.workload == w,
                           m.ManagedSource.source_key == ident.entra_object_id).first())
            label = f"{_WORKLOADS[w]['label']} — {ident.display_name or ident.upn or ident.entra_object_id}"
            if src is None:
                db.add(m.ManagedSource(
                    tenant_id=inst.tenant_id, integration_instance_id=inst.id,
                    workload=w, ownership_type="managed_user", owner_user_id=b.user_id,
                    source_key=ident.entra_object_id, name=label, state="active",
                    assigned_node_id=inst.node_id))
                created += 1
            else:
                src.owner_user_id = b.user_id
                src.name = label
                if src.state in ("planned", "disconnected"):
                    src.state = "active"
    db.commit()
    return created


def _managed_collection(db: Session, inst, source, vault):
    """Find or create the Collection that routes a managed source into the vault."""
    from ...models import Collection
    st = _WORKLOADS[source.workload]["source_type"]
    coll = (db.query(Collection)
            .filter(Collection.tenant_id == source.tenant_id,
                    Collection.vault_id == vault.id,
                    Collection.source_type == st,
                    Collection.config["m365_source_id"].astext == source.id).first()
            if db.bind.dialect.name == "postgresql" else None)
    if coll is None:
        # Portable fallback: match on our config marker in Python.
        for c in (db.query(Collection)
                  .filter(Collection.tenant_id == source.tenant_id,
                          Collection.source_type == st).all()):
            if (c.config or {}).get("m365_source_id") == source.id:
                coll = c
                break
    if coll is None:
        coll = Collection(
            tenant_id=source.tenant_id, vault_id=vault.id, name=source.name,
            source_type=st, destinations=["cv-cloud"],
            config={"m365_source_id": source.id, "managed": True})
        db.add(coll)
        db.commit()
    return coll


def _resolve_vault(db: Session, source):
    from ...models import Vault
    vault = None
    if source.owner_user_id:
        vault = (db.query(Vault)
                 .filter(Vault.owner_user_id == source.owner_user_id).first())
    if vault is None:
        vault = db.query(Vault).filter(Vault.tenant_id == source.tenant_id).first()
    return vault


def collect_source(db: Session, inst, source, app_token: str) -> dict:
    """Collect one managed source (a user's Exchange or OneDrive) via the reused
    Graph fetchers with the admin app-only token. Ingests a bounded, resumable
    chunk into the user's vault; the worker loops across cycles."""
    from ...config import get_settings
    from ...connectors import live
    from ...workers import sync_worker

    vault = _resolve_vault(db, source)
    if vault is None:
        source.state = "delayed"
        db.commit()
        return {"ok": False, "error": "no vault for owner"}
    coll = _managed_collection(db, inst, source, vault)
    cap = get_settings().content_max_bytes
    resource = f"users/{source.source_key}"
    cfg = dict(source.config or {})
    meta = _WORKLOADS[source.workload]
    state: dict = {}
    try:
        if source.workload == "exchange":
            phase = cfg.get("phase") or "backfill"
            if phase == "backfill":
                objs = list(live.stream_outlook(
                    app_token, cursor=cfg.get("backfill_cursor"), content_cap=cap,
                    options={}, state=state, mode="backfill", resource=resource))
            else:
                objs = list(live.stream_outlook(
                    app_token, cursor=cfg.get("recent_cursor"), content_cap=cap,
                    state=state, mode="recent", resource=resource))
        else:  # onedrive — delta covers history + changes in one cursor
            objs = list(live.stream_onedrive(
                app_token, cursor=cfg.get("cursor"), content_cap=cap,
                state=state, resource=resource))
    except Exception as e:  # noqa: BLE001
        source.state = "credential_error" if "401" in str(e) or "403" in str(e) else "delayed"
        db.commit()
        logger.warning("m365 collect failed (source=%s user=%s): %s",
                       source.workload, source.source_key, str(e)[:200])
        return {"ok": False, "error": str(e)[:200]}

    if objs:
        sync_worker.ingest_objects(db, coll, objs, destinations=["cv-cloud"],
                                   searchable_fields=meta["search"], facet_fields=meta["facet"],
                                   actor="m365")
    # Persist resumable cursor + advance the two-track phase for mail.
    new_cursor = state.get("cursor")
    if source.workload == "exchange":
        if (cfg.get("phase") or "backfill") == "backfill":
            cfg["backfill_cursor"] = new_cursor
            if state.get("done") or (isinstance(new_cursor, dict) and new_cursor.get("done")):
                cfg["phase"] = "recent"  # history captured — switch to new-mail track
        else:
            cfg["recent_cursor"] = new_cursor
    else:
        cfg["cursor"] = new_cursor
    source.config = cfg
    source.last_collected_at = _now()
    source.state = "active"
    db.commit()
    return {"ok": True, "objects": len(objs), "workload": source.workload}
