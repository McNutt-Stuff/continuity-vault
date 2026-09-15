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

# workload -> (source_type used for the managed Collection, index facet/searchable,
# scope: "user" = per mapped identity, "org" = per discovered site/team).
_WORKLOADS = {
    "exchange": {"source_type": "outlook", "label": "Exchange Online", "scope": "user",
                 "facet": ["from", "folder"], "search": ["from", "subject", "folder"]},
    "onedrive": {"source_type": "onedrive", "label": "OneDrive", "scope": "user",
                 "facet": ["mime"], "search": ["mime", "path"]},
    "sharepoint": {"source_type": "sharepoint", "label": "SharePoint", "scope": "org",
                   "facet": ["site", "mime"], "search": ["site", "path", "mime"]},
    "teams": {"source_type": "teams", "label": "Teams channels", "scope": "org",
              "facet": ["team", "channel"], "search": ["from", "team", "channel"]},
    "teams_chat": {"source_type": "teams", "label": "Teams chats", "scope": "user",
                   "facet": ["chat"], "search": ["from", "chat"]},
    "copilot": {"source_type": "copilot", "label": "Microsoft 365 Copilot", "scope": "user",
                "facet": ["app", "interactionType"], "search": ["app", "from", "interactionType"]},
    "calendar": {"source_type": "calendar", "label": "Exchange Calendar", "scope": "user",
                 "facet": ["organizer", "location"], "search": ["organizer", "location"]},
    "contacts": {"source_type": "contacts", "label": "Exchange Contacts", "scope": "user",
                 "facet": ["company"], "search": ["emails", "company", "jobTitle"]},
    "onenote": {"source_type": "onenote", "label": "OneNote", "scope": "user",
                "facet": ["section"], "search": ["section"]},
}
_DEFAULT_WORKLOADS = ("exchange", "onedrive")
_USER_WORKLOADS = frozenset(w for w, v in _WORKLOADS.items() if v["scope"] == "user")
_ORG_WORKLOADS = frozenset(w for w, v in _WORKLOADS.items() if v["scope"] == "org")
_ORG_RESOURCE_CAP = 500  # bound org site/team fan-out per instance


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def audit_cycle(db: Session, inst, *, objects: int, sources: int, trigger: str,
                actor: str = "m365") -> None:
    """Record a managed-collection result to the audit ledger + Platform Logs (and
    the customer Activity trail) so every M365 backup is observable, not silent."""
    from ... import audit
    try:
        audit.record(db, actor=actor, action="m365.collected",
                     tenant_id=inst.tenant_id, resource=inst.id,
                     category="activity",
                     severity="notice" if objects else "info",
                     detail={"type": "microsoft365", "trigger": trigger,
                             "objects": objects, "sources": sources})
    except Exception:  # noqa: BLE001 — observability must never break collection
        logger.exception("m365 audit_cycle failed (instance=%s)", inst.id)


def _record_source_activity(db: Session, inst, source, *, objects: int,
                            error: str | None) -> None:
    """Record a per-source collection result so each managed source shows in the
    customer Activity trail + Platform Logs exactly like a regular source poll —
    with its object count and status, never a silent run."""
    from ... import audit
    try:
        audit.record(
            db, actor="m365", action="m365.source.collected",
            tenant_id=source.tenant_id, resource=source.id,
            category="activity",
            severity="warning" if error else ("notice" if objects else "info"),
            detail={"type": "microsoft365", "workload": source.workload,
                    "source": source.name, "state": source.state,
                    "objects": objects, "error": error})
    except Exception:  # noqa: BLE001 — observability must never break collection
        logger.exception("m365 source activity record failed (source=%s)", source.id)


def profile(inst) -> dict:
    """The org-leader's managed-protection profile (which workloads to protect,
    where to route them, and how often) — the data-map profile applied to every
    protected user. Normalized with sensible fallbacks."""
    cfg = inst.config or {}
    prof = dict(cfg.get("managed_profile") or {})
    workloads = [w for w in (prof.get("workloads") or []) if w in _WORKLOADS]
    if not workloads:
        caps = [c for c in (cfg.get("capabilities") or []) if c in _WORKLOADS]
        workloads = caps or list(_DEFAULT_WORKLOADS)
    dests = [str(d) for d in (prof.get("destinations") or []) if d] or ["cv-cloud"]
    iv = prof.get("backup_interval_minutes")
    try:
        interval = int(iv) if iv not in (None, "") else None
    except (TypeError, ValueError):
        interval = None
    return {"workloads": workloads, "destinations": dests,
            "backup_interval_minutes": interval}


def _ensure_managed_collection(db: Session, inst, source, vault, prof: dict):
    """Create/refresh the Data Map Collection that protects a managed source on
    the user's behalf — routed + scheduled per the org profile, owned by the
    user's vault, flagged managed (so the standard connector scheduler skips it;
    the M365 worker collects it via the admin app-only token)."""
    from ...models import Collection
    st = _WORKLOADS[source.workload]["source_type"]
    coll = None
    for c in (db.query(Collection)
              .filter(Collection.tenant_id == source.tenant_id,
                      Collection.source_type == st).all()):
        if (c.config or {}).get("m365_source_id") == source.id:
            coll = c
            break
    dests = prof.get("destinations") or ["cv-cloud"]
    interval = prof.get("backup_interval_minutes")
    if coll is None:
        coll = Collection(
            tenant_id=source.tenant_id, vault_id=vault.id, name=source.name,
            source_type=st, destinations=list(dests),
            backup_interval_minutes=interval,
            config={"m365_source_id": source.id, "managed": True,
                    "m365_workload": source.workload,
                    "m365_instance_id": inst.id})
        db.add(coll)
        db.flush()
    else:
        # Keep the data-map profile in sync with the org settings + mapping.
        coll.destinations = list(dests)
        coll.backup_interval_minutes = interval
        coll.name = source.name
        if vault is not None and coll.vault_id != vault.id:
            coll.vault_id = vault.id
    return coll


def _resolve_vault_for(db: Session, tenant_id: str, owner_user_id):
    from ...models import Vault
    vault = None
    if owner_user_id:
        vault = db.query(Vault).filter(Vault.owner_user_id == owner_user_id).first()
    if vault is None:
        vault = db.query(Vault).filter(Vault.tenant_id == tenant_id).first()
    return vault


def provision_sources(db: Session, inst) -> int:
    """Establish admin-level managed sources (Exchange + OneDrive per the profile)
    for every mapped / protected-only in-scope identity, AND a Data Map profile
    (Collection) that protects each on the user's behalf. Idempotent — also pauses
    sources whose identity/workload is no longer in scope/selected."""
    prof = profile(inst)
    workloads = [w for w in prof["workloads"] if w in _USER_WORKLOADS]
    bindings = (db.query(m.ExternalIdentityBinding)
                .filter(m.ExternalIdentityBinding.integration_instance_id == inst.id,
                        m.ExternalIdentityBinding.status.in_(("mapped", "protected_only"))).all())
    created = 0
    kept: set = set()
    for b in bindings:
        ident = db.get(m.ExternalIdentity, b.external_identity_id)
        if ident is None or not ident.in_scope:
            continue
        vault = _resolve_vault_for(db, inst.tenant_id, b.user_id)
        for w in workloads:
            src = (db.query(m.ManagedSource)
                   .filter(m.ManagedSource.integration_instance_id == inst.id,
                           m.ManagedSource.workload == w,
                           m.ManagedSource.source_key == ident.entra_object_id).first())
            label = f"{_WORKLOADS[w]['label']} — {ident.display_name or ident.upn or ident.entra_object_id}"
            if src is None:
                src = m.ManagedSource(
                    tenant_id=inst.tenant_id, integration_instance_id=inst.id,
                    workload=w, ownership_type="managed_user", owner_user_id=b.user_id,
                    source_key=ident.entra_object_id, name=label, state="active",
                    assigned_node_id=inst.node_id)
                db.add(src)
                db.flush()
                created += 1
            else:
                src.owner_user_id = b.user_id
                src.name = label
                if src.state in ("planned", "disconnected", "paused_by_admin"):
                    src.state = "active"
            # Denormalize the profile cadence so the worker's due-check is cheap.
            scfg = dict(src.config or {})
            scfg["interval_minutes"] = prof["backup_interval_minutes"]
            src.config = scfg
            kept.add(src.id)
            if vault is not None:
                _ensure_managed_collection(db, inst, src, vault, prof)
    # Pause USER sources no longer selected (workload dropped) or whose identity
    # fell out of scope/mapping — stops collection without deleting protected data.
    # (Org sources are reconciled separately in provision_org_sources, which has
    # the Graph token to discover sites/teams.)
    for src in (db.query(m.ManagedSource)
                .filter(m.ManagedSource.integration_instance_id == inst.id,
                        m.ManagedSource.workload.in_(list(_USER_WORKLOADS))).all()):
        if src.id not in kept and src.state not in ("decommissioned",):
            src.state = "paused_by_admin"
    db.commit()
    return created


def provision_org_sources(db: Session, inst, token: str) -> int:
    """Discover org resources (SharePoint sites, Teams) with the app-only token and
    establish an organization managed source + Data Map profile per resource. Needs
    the token, so it runs in the worker/collection path (not plain provision)."""
    from . import graph
    prof = profile(inst)
    org_workloads = [w for w in prof["workloads"] if w in _ORG_WORKLOADS]
    if not org_workloads:
        logger.info("m365 org provisioning: no org workloads (SharePoint/Teams) in the "
                    "managed profile (instance=%s) — profile has %s; nothing to discover",
                    inst.id, prof["workloads"])
    vault = _resolve_vault_for(db, inst.tenant_id, None)  # tenant/org vault
    created = 0
    kept: set = set()
    for w in org_workloads:
        try:
            if w == "sharepoint":
                resources = [(s.get("id"), s.get("displayName") or s.get("name")
                              or s.get("webUrl") or s.get("id"))
                             for s in graph.list_sites(token, cap=_ORG_RESOURCE_CAP) if s.get("id")]
            elif w == "teams":
                resources = [(g.get("id"), g.get("displayName") or g.get("id"))
                             for g in graph.list_teams(token, cap=_ORG_RESOURCE_CAP) if g.get("id")]
            else:
                continue
        except graph.GraphError as e:
            logger.warning("m365 org discovery failed (workload=%s instance=%s): %s",
                           w, inst.id, e)
            continue
        logger.info("m365 org discovery (instance=%s workload=%s): %d resource(s) found",
                    inst.id, w, len(resources))
        for rid, rname in resources:
            src = (db.query(m.ManagedSource)
                   .filter(m.ManagedSource.integration_instance_id == inst.id,
                           m.ManagedSource.workload == w,
                           m.ManagedSource.source_key == rid).first())
            label = f"{_WORKLOADS[w]['label']} — {rname}"
            if src is None:
                src = m.ManagedSource(
                    tenant_id=inst.tenant_id, integration_instance_id=inst.id,
                    workload=w, ownership_type="organization", owner_user_id=None,
                    source_key=rid, name=label, state="active", assigned_node_id=inst.node_id)
                db.add(src)
                db.flush()
                created += 1
            else:
                src.name = label
                if src.state in ("planned", "disconnected", "paused_by_admin"):
                    src.state = "active"
            scfg = dict(src.config or {})
            scfg["interval_minutes"] = prof["backup_interval_minutes"]
            src.config = scfg
            kept.add(src.id)
            if vault is not None:
                _ensure_managed_collection(db, inst, src, vault, prof)
    # Pause org sources for de-selected workloads or resources that vanished.
    for src in (db.query(m.ManagedSource)
                .filter(m.ManagedSource.integration_instance_id == inst.id,
                        m.ManagedSource.workload.in_(list(_ORG_WORKLOADS))).all()):
        if src.id not in kept and src.state not in ("decommissioned",):
            src.state = "paused_by_admin"
    db.commit()
    if created:
        logger.info("m365 org sources provisioned (instance=%s): +%d", inst.id, created)
    return created



def _managed_collection(db: Session, inst, source, vault):
    """Find or create the Collection that routes a managed source into the vault
    (delegates to the profile-aware builder so routing/schedule stay consistent)."""
    return _ensure_managed_collection(db, inst, source, vault, profile(inst))


def _resolve_vault(db: Session, source):
    return _resolve_vault_for(db, source.tenant_id, source.owner_user_id)


def _app_token_for_instance(db: Session, inst) -> str:
    """Mint the app-only Graph token for an instance's connected org, or "" if the
    integration isn't configured/consented on this box."""
    from ... import platform_config
    from . import graph, models as m
    vals = platform_config.integration_values("microsoft365")
    client_id = (vals.get("client_id") or "").strip()
    client_secret = (vals.get("client_secret") or "").strip()
    cred = (db.query(m.ManagedCredentialRef)
            .filter(m.ManagedCredentialRef.integration_instance_id == inst.id).first())
    if not (client_id and client_secret and cred and cred.consent_state == "granted"
            and cred.microsoft_tenant_id):
        return ""
    try:
        return graph.app_token(client_id, client_secret, cred.microsoft_tenant_id)
    except graph.GraphError as e:
        logger.warning("m365 app token failed (instance=%s): %s", inst.id, e)
        return ""


def run_managed_collection(db: Session, collection, destinations=None, progress=None):
    """Collect a managed M365 Collection via the SHARED backup path.

    Called by ``sync_worker.run_backup`` when a Collection is a managed M365 source
    so managed sources run through the standard scheduler / SyncJob / activity
    pipeline exactly like every other source — only the credential (app-only Graph
    token + per-user/site resource) is integration-specific. Returns the
    SnapshotReceipt of the ingested chunk (or None when nothing was collected)."""
    from ...models import IntegrationInstance
    from . import models as m
    cfg = collection.config or {}
    inst = db.get(IntegrationInstance, cfg.get("m365_instance_id"))
    source = db.get(m.ManagedSource, cfg.get("m365_source_id"))
    if inst is None or source is None:
        logger.warning("m365 managed collection %s missing instance/source", collection.id)
        collection.config = {**cfg, "m365_has_more": False}
        return None
    token = _app_token_for_instance(db, inst)
    if not token:
        raise RuntimeError("Microsoft 365 admin consent required (no app token)")
    if progress:
        progress(0, 0, f"Backing up {source.name}…")
    res = collect_source(db, inst, source, token)
    # Signal the shared job loop to keep chunking while the source still has more.
    db.refresh(collection)
    collection.config = {**(collection.config or {}), "m365_has_more": bool(res.get("has_more"))}
    db.commit()
    if progress:
        progress(int(res.get("objects") or 0), int(res.get("objects") or 0),
                 f"{source.name}: {int(res.get('objects') or 0)} item(s)")
    if not res.get("ok"):
        raise RuntimeError(res.get("error") or "managed collection failed")
    return res.get("receipt")


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
    key = source.source_key
    cfg = dict(source.config or {})
    meta = _WORKLOADS[source.workload]
    state: dict = {}
    try:
        if source.workload == "exchange":
            resource = f"users/{key}"
            phase = cfg.get("phase") or "backfill"
            if phase == "backfill":
                objs = list(live.stream_outlook(
                    app_token, cursor=cfg.get("backfill_cursor"), content_cap=cap,
                    options={}, state=state, mode="backfill", resource=resource))
            else:
                objs = list(live.stream_outlook(
                    app_token, cursor=cfg.get("recent_cursor"), content_cap=cap,
                    state=state, mode="recent", resource=resource))
        elif source.workload == "onedrive":  # per-user OneDrive (delta)
            objs = list(live.stream_onedrive(
                app_token, cursor=cfg.get("cursor"), content_cap=cap,
                state=state, resource=f"users/{key}"))
        elif source.workload == "sharepoint":  # org site document library (delta)
            objs = list(live.stream_onedrive(
                app_token, cursor=cfg.get("cursor"), content_cap=cap,
                state=state, resource=f"sites/{key}"))
        elif source.workload == "teams":  # org team channel conversations
            objs = list(live.stream_teams(
                app_token, cursor=cfg.get("cursor"), content_cap=cap,
                state=state, resource=f"teams/{key}"))
        elif source.workload == "copilot":  # a user's Microsoft 365 Copilot interactions
            objs = list(live.stream_copilot(
                app_token, cursor=cfg.get("cursor"), content_cap=cap,
                state=state, resource=f"users/{key}"))
        elif source.workload == "calendar":  # a user's Exchange calendar
            objs = list(live.stream_calendar(
                app_token, cursor=cfg.get("cursor"), content_cap=cap,
                state=state, resource=f"users/{key}"))
        elif source.workload == "contacts":  # a user's Exchange contacts
            objs = list(live.stream_contacts(
                app_token, cursor=cfg.get("cursor"), content_cap=cap,
                state=state, resource=f"users/{key}"))
        elif source.workload == "onenote":  # a user's OneNote notebooks
            objs = list(live.stream_onenote(
                app_token, cursor=cfg.get("cursor"), content_cap=cap,
                state=state, resource=f"users/{key}"))
        else:  # teams_chat — a user's 1:1/group chats
            objs = list(live.stream_teams(
                app_token, cursor=cfg.get("cursor"), content_cap=cap,
                state=state, resource=f"users/{key}/chats"))
    except Exception as e:  # noqa: BLE001
        is_auth = "401" in str(e) or "403" in str(e)
        source.state = "permission_required" if is_auth else "delayed"
        cfg["last_result"] = {"at": _now().isoformat(), "objects": 0,
                              "error": str(e)[:200], "ok": False}
        source.config = cfg
        source.last_collected_at = _now()
        db.commit()
        logger.warning("m365 collect failed (source=%s key=%s state=%s): %s",
                       source.workload, source.source_key, source.state, str(e)[:200])
        _record_source_activity(db, inst, source, objects=0, error=str(e)[:200])
        return {"ok": False, "error": str(e)[:200]}

    if objs:
        dests = coll.destinations or ["cv-cloud"]
        receipt = sync_worker.ingest_objects(db, coll, objs, destinations=dests,
                                             searchable_fields=meta["search"], facet_fields=meta["facet"],
                                             actor="m365")
    else:
        receipt = None
    # Persist resumable cursor + advance the two-track phase for mail.
    new_cursor = state.get("cursor")
    backfilling = False
    if source.workload == "exchange":
        if (cfg.get("phase") or "backfill") == "backfill":
            cfg["backfill_cursor"] = new_cursor
            if state.get("done") or (isinstance(new_cursor, dict) and new_cursor.get("done")):
                cfg["phase"] = "recent"  # history captured — switch to new-mail track
            else:
                backfilling = True
        else:
            cfg["recent_cursor"] = new_cursor
    else:
        cfg["cursor"] = new_cursor
    # More to pull this run? (mail still backfilling, or a streaming delta reports
    # has_more) — lets the shared job loop keep chunking until the source is drained.
    has_more = backfilling or bool(isinstance(new_cursor, dict) and new_cursor.get("has_more"))
    total = int(cfg.get("objects_total") or 0) + len(objs)
    cfg["objects_total"] = total
    cfg["last_result"] = {"at": _now().isoformat(), "objects": len(objs),
                          "error": None, "ok": True}
    source.config = cfg
    source.last_collected_at = _now()
    # A source that has never yielded a single object across its lifetime is almost
    # certainly a permission/consent gap (Graph 200-but-empty) or an empty mailbox —
    # surface that as an actionable state instead of a silent "active/0".
    if total == 0 and not backfilling:
        source.state = "empty"
        logger.info("m365 collected (instance=%s workload=%s key=%s): 0 object(s) — "
                    "Graph returned no items (verify the workload's Application "
                    "permission is consented and the mailbox/drive/site has data)",
                    inst.id, source.workload, source.source_key)
    else:
        source.state = "active"
        logger.info("m365 collected (instance=%s workload=%s key=%s): %d object(s) "
                    "(lifetime %d)", inst.id, source.workload, source.source_key,
                    len(objs), total)
    db.commit()
    _record_source_activity(db, inst, source, objects=len(objs), error=None)
    return {"ok": True, "objects": len(objs), "workload": source.workload,
            "has_more": has_more, "receipt": receipt}
