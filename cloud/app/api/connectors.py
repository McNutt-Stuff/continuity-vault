"""Connector catalog + real OAuth / token account linking (sync-worker sources)."""

from __future__ import annotations

import base64
import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import audit, credstore, security
from ..config import get_settings
from ..connectors import ALL_CONNECTORS, get_connector
from ..connectors import oauth
from ..connectors import evernote_mcp
from ..db import get_db
from ..models import Collection, ConnectorAccount, Tenant

router = APIRouter(prefix="/connectors", tags=["connectors"])
settings = get_settings()
logger = logging.getLogger("cv.connectors")


def _setup_instructions(connector_type: str) -> list[str]:
    redirect = oauth.redirect_uri()
    if connector_type == "gmail":
        return [
            "In Google Cloud Console, create an OAuth 2.0 Client ID (type: Web application).",
            f"Add this Authorized redirect URI: {redirect}",
            "Enable the Gmail API for the project.",
            "Set CV_GOOGLE_CLIENT_ID and CV_GOOGLE_CLIENT_SECRET on the server, then restart.",
        ]
    if connector_type in ("outlook", "onedrive"):
        perm = "Mail.Read" if connector_type == "outlook" else "Files.Read.All"
        return [
            "In Microsoft Entra ID, register an application (Web).",
            f"Add this Redirect URI: {redirect}",
            f"Add delegated Microsoft Graph permissions: {perm} and offline_access.",
            "Set CV_MICROSOFT_CLIENT_ID and CV_MICROSOFT_CLIENT_SECRET, then restart.",
        ]
    if connector_type == "dropbox":
        return [
            "At dropbox.com/developers, create an app (scoped access, full dropbox).",
            f"Add this OAuth redirect URI: {redirect}",
            "Grant scopes: files.metadata.read, files.content.read, account_info.read.",
            "Set CV_DROPBOX_CLIENT_ID and CV_DROPBOX_CLIENT_SECRET, then restart.",
        ]
    if connector_type == "onepassword":
        return [
            "1Password is collected by a local Arkive desktop agent (not a cloud pull).",
            "Install the desktop agent on a Mac that has the 1Password app.",
            "Unlock 1Password and enable Settings → Developer → Integrate with 1Password CLI.",
            "The agent extracts items with the `op` CLI and pushes them encrypted.",
        ]
    if connector_type == "endpoint_files":
        return [
            "Endpoint files are collected by a local Arkive desktop agent (not a cloud pull).",
            "Add the source in the Data Map and pick the agent to collect from.",
            "Browse the agent's drives and choose which folders to back up, plus any file-type",
            "or size exclusions. The agent walks them and pushes each file client-encrypted.",
        ]
    if connector_type == "icloud":
        return [
            "At appleid.apple.com, generate an app-specific password.",
            "Install 'pyicloud' on the server (pip install pyicloud).",
            "Connect with your Apple ID and that app-specific password.",
            "Note: accounts requiring interactive 2FA can't be synced automatically.",
        ]
    if connector_type == "evernote":
        return [
            "Evernote uses its MCP server (mcp.evernote.com) over OAuth 2.0 — the",
            "legacy API is deprecated. No developer token is needed.",
            f"The client registers dynamically; set this OAuth redirect: {redirect}",
            "Click Connect and approve access on Evernote's consent screen.",
            "Notes and attachments are pulled, encrypted, versioned, and searchable.",
        ]
    if connector_type == "linkedin":
        return [
            "At linkedin.com/developers, create an app and add the 'Sign In with LinkedIn using OpenID Connect' product.",
            f"Add this Authorized redirect URL: {redirect}",
            "Request scopes: openid, profile, email. Backing up member posts also needs LinkedIn's Community Management API (partner access).",
            "Set CV_LINKEDIN_CLIENT_ID and CV_LINKEDIN_CLIENT_SECRET on the server, then restart.",
        ]
    return []


# Source families (who provides the account) and functional types, used to group
# the Sources page into sections as the catalog grows.
_SOURCE_FAMILY = {
    "gmail": "Google", "google_contacts": "Google", "google_calendar": "Google",
    "google_photos": "Google",
    "outlook": "Microsoft", "onedrive": "Microsoft",
    "icloud": "Apple",
    "dropbox": "Dropbox",
    "reddit": "Reddit", "facebook": "Meta", "instagram": "Meta",
    "linkedin": "LinkedIn",
    "evernote": "Evernote",
    "onepassword": "Endpoint Collected", "endpoint_files": "Endpoint Collected",
    "custom": "Custom",
}
_SOURCE_TYPE = {
    "gmail": "Email", "outlook": "Email",
    "onedrive": "Files & Storage", "dropbox": "Files & Storage",
    "icloud": "Files & Storage", "endpoint_files": "Files & Storage",
    "google_photos": "Photos",
    "google_contacts": "Contacts",
    "google_calendar": "Calendar",
    "onepassword": "Passwords",
    "reddit": "Social", "facebook": "Social", "instagram": "Social",
    "linkedin": "Social",
    "evernote": "Notes",
    "custom": "Other",
}


@router.get("/catalog")
def catalog(tenant: Tenant = Depends(security.get_tenant),
            db: Session = Depends(get_db)):
    from .. import platform_config
    from ..models import SourceConfig
    # Admin family overrides (Sources page) win over the built-in grouping.
    fam_override = {sc.connector_type: sc.family
                    for sc in db.query(SourceConfig).all() if sc.family}
    out = []
    for c in ALL_CONNECTORS:
        spec = c.oauth_spec()
        caps = c.capabilities()
        ctype = spec.connector_type
        is_ev = ctype == "evernote"
        is_oauth_mode = oauth.is_oauth(ctype) or is_ev
        mode = "oauth" if is_oauth_mode else "token"
        # Platform admins can disable an OAuth source for all tenants.
        if is_oauth_mode and not platform_config.source_enabled(ctype):
            continue
        out.append({
            "type": ctype,
            "displayName": spec.display_name,
            "authType": spec.auth_type,
            "icon": spec.icon,
            "color": spec.color,
            "family": fam_override.get(ctype, _SOURCE_FAMILY.get(ctype, "Other")),
            "category": _SOURCE_TYPE.get(ctype, "Other"),
            "docTypes": spec.doc_types,
            "mode": mode,
            "configured": (True if is_ev else oauth.is_configured(ctype)),
            "requiresAgent": caps.requires_agent,
            "setup": _setup_instructions(ctype),
            "capabilities": {
                "incremental": caps.incremental,
                "browsable": caps.browsable,
                "delta": caps.delta,
                "searchableFields": caps.searchable_fields,
                "facetFields": caps.facet_fields,
                "filterCategories": caps.filter_categories,
            },
        })
    return out


class ConnectRequest(BaseModel):
    account_label: str | None = None
    account_id: str | None = None  # set to re-authorize an existing source


@router.get("/accounts/{account_id}/folders")
def list_account_folders(account_id: str, path: str = "",
                         principal: security.Principal = Depends(security.get_principal),
                         tenant: Tenant = Depends(security.get_tenant),
                         db: Session = Depends(get_db)):
    """Immediate child folders of ``path`` for a browsable cloud source, powering
    the Data Map folder picker. Scoped to the caller's own linked source."""
    account = db.get(ConnectorAccount, account_id)
    if (not account or account.tenant_id != tenant.id
            or (account.owner_user_id and account.owner_user_id != principal.user_id)):
        raise HTTPException(404, "source not found")
    connector = get_connector(account.connector_type)
    if not connector or not connector.capabilities().browsable:
        raise HTTPException(400, "this source doesn't support folder browsing")
    from ..workers.sync_worker import access_token_for_account
    token = access_token_for_account(db, account)
    if not token:
        raise HTTPException(400, "reconnect this source to browse its folders")
    try:
        folders = connector.list_folders({"access_token": token}, path or "")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"couldn't list folders: {exc}")
    return {"folders": folders}


@router.post("/{connector_type}/connect")
def connect(connector_type: str, body: ConnectRequest,
            principal: security.Principal = Depends(security.require_passkey),
            tenant: Tenant = Depends(security.get_tenant)):
    if not get_connector(connector_type):
        raise HTTPException(404, "unknown connector")
    if connector_type == "evernote":
        # Evernote MCP: OAuth 2.1 + PKCE against its discovered auth server. The
        # PKCE verifier rides in the signed state so the callback can complete.
        if not evernote_mcp.is_configured():
            raise HTTPException(400, {
                "error": "provider_not_configured",
                "setup": _setup_instructions(connector_type),
            })
        verifier, challenge = evernote_mcp.make_pkce()
        state = oauth.sign_state({
            "tid": tenant.id, "uid": principal.user_id,
            "type": connector_type, "label": body.account_label or "",
            "aid": body.account_id or "",
            "pkce": verifier, "n": str(uuid.uuid4()),
        })
        try:
            url = evernote_mcp.authorize_url(state, challenge, oauth.redirect_uri())
        except Exception as exc:
            logger.error("Evernote MCP authorize failed: %s", exc)
            raise HTTPException(502, "could not start Evernote authorization")
        return {"mode": "oauth", "authorize_url": url}
    if oauth.is_oauth(connector_type):
        if not oauth.is_configured(connector_type):
            raise HTTPException(400, {
                "error": "provider_not_configured",
                "setup": _setup_instructions(connector_type),
            })
        state = oauth.sign_state({
            "tid": tenant.id, "uid": principal.user_id,
            "type": connector_type, "label": body.account_label or "",
            "aid": body.account_id or "",
            "n": str(uuid.uuid4()),
        })
        return {"mode": "oauth", "authorize_url": oauth.authorize_url(connector_type, state)}
    # Token-based provider (1Password service account, iCloud app password).
    return {"mode": "token", "instructions": _setup_instructions(connector_type)}


@router.get("/oauth/callback")
def oauth_callback(code: str | None = Query(default=None),
                   state: str | None = Query(default=None),
                   error: str | None = Query(default=None),
                   db: Session = Depends(get_db)):
    portal = settings.rp_origin.rstrip("/")
    if error or not code or not state:
        return RedirectResponse(f"{portal}/connectors?error={error or 'cancelled'}")
    data = oauth.read_state(state)
    if not data:
        return RedirectResponse(f"{portal}/connectors?error=invalid_state")

    connector_type = data["type"]
    if connector_type == "evernote":
        try:
            tokens = evernote_mcp.exchange_code(code, data.get("pkce", ""), oauth.redirect_uri())
        except Exception as exc:
            logger.error("Evernote MCP token exchange failed: %s", exc)
            return RedirectResponse(f"{portal}/connectors?error=token_exchange")
        _link_or_reauth(db, data, connector_type, tokens, "Evernote account")
        return RedirectResponse(f"{portal}/connectors?connected={connector_type}")
    try:
        tokens = oauth.exchange_code(connector_type, code)
    except Exception as exc:
        logger.error("OAuth token exchange failed for %s: %s", connector_type, exc)
        return RedirectResponse(f"{portal}/connectors?error=token_exchange")

    identity = _fetch_account_label(connector_type, tokens)
    default_label = identity or f"{connector_type} account"
    _link_or_reauth(db, data, connector_type, tokens, default_label, username=identity)
    return RedirectResponse(f"{portal}/connectors?connected={connector_type}")


def _link_or_reauth(db: Session, data: dict, connector_type: str,
                    tokens: dict, default_label: str,
                    username: str | None = None) -> ConnectorAccount:
    """Create a new linked source, or — when re-authorizing (state carries the
    account id) — refresh the existing one and clear its error/reauth state."""
    creds = credstore.encrypt(data["tid"], tokens)
    scopes = (tokens.get("scope") or "").split()
    aid = data.get("aid")
    existing = db.get(ConnectorAccount, aid) if aid else None
    if existing is not None and existing.tenant_id == data["tid"]:
        existing.encrypted_credentials = creds
        existing.scopes = scopes
        existing.auth_status = "linked"
        existing.last_error = None
        existing.last_error_at = None
        # Refresh the linked identity on every successful re-auth (backfills sources
        # linked before we captured it).
        if username:
            existing.account_username = username
        db.commit()
        audit.record(db, actor=data["uid"], action="connector.reauthorized",
                     tenant_id=data["tid"], resource=existing.id,
                     category="connector", detail={"type": connector_type})
        return existing
    account = ConnectorAccount(
        tenant_id=data["tid"], owner_user_id=data.get("uid"), connector_type=connector_type,
        account_label=data.get("label") or default_label, auth_status="linked",
        account_username=username,
        encrypted_credentials=creds, scopes=scopes)
    db.add(account)
    db.commit()
    audit.record(db, actor=data["uid"], action="connector.linked",
                 tenant_id=data["tid"], resource=account.id, detail={"type": connector_type})
    return account


class TokenLinkRequest(BaseModel):
    account_label: str
    token: str
    username: str | None = None  # e.g. Apple ID for iCloud
    host: str | None = None       # e.g. 1Password Connect server URL


@router.post("/{connector_type}/token")
def link_with_token(connector_type: str, body: TokenLinkRequest,
                    principal: security.Principal = Depends(security.require_passkey),
                    tenant: Tenant = Depends(security.get_tenant),
                    db: Session = Depends(get_db)):
    if connector_type not in oauth.TOKEN_TYPES:
        raise HTTPException(400, "this provider uses OAuth; use /connect")
    creds = {"token": body.token, "username": body.username, "host": body.host}
    account = ConnectorAccount(
        tenant_id=tenant.id,
        owner_user_id=principal.user_id,
        connector_type=connector_type,
        account_label=body.account_label,
        account_username=(body.username or None),
        auth_status="linked",
        encrypted_credentials=credstore.encrypt(tenant.id, creds),
        scopes=[],
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    audit.record(db, actor=principal.user_id, action="connector.linked",
                 tenant_id=tenant.id, resource=account.id,
                 detail={"type": connector_type})
    return {"id": account.id, "connector_type": connector_type,
            "account_label": account.account_label, "auth_status": account.auth_status}


@router.get("/accounts")
def list_accounts(principal: security.Principal = Depends(security.get_principal),
                  tenant: Tenant = Depends(security.get_tenant),
                  db: Session = Depends(get_db)):
    from ..models import SearchDocument
    # Data partitioning: a user only sees the sources they linked.
    accounts = db.query(ConnectorAccount).filter(
        ConnectorAccount.tenant_id == tenant.id,
        ConnectorAccount.owner_user_id == principal.user_id).all()
    # Protected data size per account: sum of the latest version of each object in
    # the account's collections (deduped by object_id so versions aren't double-counted).
    coll_to_acct = {cid: aid for cid, aid in db.query(
        Collection.id, Collection.connector_account_id).filter(
        Collection.tenant_id == tenant.id).all()}
    bytes_by_acct: dict[str, int] = {}
    if coll_to_acct:
        seen: set[tuple] = set()
        rows = (db.query(SearchDocument.collection_id, SearchDocument.object_id,
                         SearchDocument.size_bytes)
                .filter(SearchDocument.tenant_id == tenant.id,
                        SearchDocument.collection_id.in_(list(coll_to_acct.keys())))
                .order_by(SearchDocument.created_at.desc()).all())
        for cid, oid, sz in rows:
            aid = coll_to_acct.get(cid)
            if not aid or (aid, oid) in seen:
                continue
            seen.add((aid, oid))
            bytes_by_acct[aid] = bytes_by_acct.get(aid, 0) + int(sz or 0)
    return [{"id": a.id, "connector_type": a.connector_type,
             "account_label": a.account_label, "account_username": a.account_username,
             "auth_status": a.auth_status,
             "active": bool(a.active),
             "last_sync_at": a.last_sync_at.isoformat() if a.last_sync_at else None,
             "last_object_count": a.last_object_count,
             "protected_bytes": bytes_by_acct.get(a.id, 0),
             "last_error": a.last_error,
             "last_error_at": a.last_error_at.isoformat() if a.last_error_at else None,
             "needs_reauth": a.auth_status == "needs-reauth",
             "has_error": bool(a.last_error),
             "scopes": a.scopes} for a in accounts]


class AccountRename(BaseModel):
    account_label: str


@router.put("/accounts/{account_id}")
def rename_account(account_id: str, body: AccountRename,
                   principal: security.Principal = Depends(security.get_principal),
                   tenant: Tenant = Depends(security.get_tenant),
                   db: Session = Depends(get_db)):
    account = db.get(ConnectorAccount, account_id)
    if not account or account.tenant_id != tenant.id:
        raise HTTPException(404, "account not found")
    label = (body.account_label or "").strip()
    if not label:
        raise HTTPException(400, "name required")
    account.account_label = label
    db.commit()
    audit.record(db, actor=principal.user_id, action="connector.renamed",
                 tenant_id=tenant.id, resource=account_id, detail={"label": label})
    return {"id": account.id, "account_label": account.account_label,
            "account_username": account.account_username}


@router.delete("/accounts/{account_id}")
def unlink(account_id: str,
           principal: security.Principal = Depends(security.get_principal),
           tenant: Tenant = Depends(security.get_tenant),
           db: Session = Depends(get_db)):
    """Deactivate (unlink) a source. Once data is ingested a source is never
    deleted — it identifies that data — so this stops sync and keeps the data,
    with the option to re-link or purge. Truly removing data is a separate,
    permission-gated purge."""
    account = db.get(ConnectorAccount, account_id)
    if not account or account.tenant_id != tenant.id or account.owner_user_id != principal.user_id:
        raise HTTPException(404, "account not found")
    account.active = False
    account.auth_status = "unlinked"
    db.commit()
    audit.record(db, actor=principal.user_id, action="connector.deactivated",
                 tenant_id=tenant.id, resource=account_id)
    return {"ok": True, "active": False, "auth_status": "unlinked"}


@router.post("/accounts/{account_id}/reactivate")
def reactivate(account_id: str,
               principal: security.Principal = Depends(security.get_principal),
               tenant: Tenant = Depends(security.get_tenant),
               db: Session = Depends(get_db)):
    """Re-link a deactivated source so it resumes syncing into the same data."""
    account = db.get(ConnectorAccount, account_id)
    if not account or account.tenant_id != tenant.id or account.owner_user_id != principal.user_id:
        raise HTTPException(404, "account not found")
    account.active = True
    # Keep needs-reauth if creds are gone; otherwise resume as linked.
    account.auth_status = "linked" if account.encrypted_credentials else "needs-reauth"
    db.commit()
    audit.record(db, actor=principal.user_id, action="connector.reactivated",
                 tenant_id=tenant.id, resource=account_id)
    return {"ok": True, "active": True, "auth_status": account.auth_status}


def _purge_source_local(db: Session, account: ConnectorAccount) -> dict:
    """Delete a source's local index + recovery points + mappings + the account
    itself. Removing the index/receipts makes the ciphertext unfindable and
    undecryptable (its snapshot keys can no longer be derived) — irreversible.
    Physical ciphertext ages out under retention."""
    from ..models import SearchDocument, SnapshotReceipt
    coll_ids = [c.id for c in db.query(Collection)
                .filter(Collection.connector_account_id == account.id).all()]
    docs = receipts = 0
    if coll_ids:
        docs = db.query(SearchDocument).filter(
            SearchDocument.collection_id.in_(coll_ids)).delete(synchronize_session=False)
        receipts = db.query(SnapshotReceipt).filter(
            SnapshotReceipt.collection_id.in_(coll_ids)).delete(synchronize_session=False)
        db.query(Collection).filter(Collection.id.in_(coll_ids)).delete(synchronize_session=False)
    db.delete(account)
    return {"documents": int(docs or 0), "recovery_points": int(receipts or 0),
            "collections": len(coll_ids), "removed": True}


def _purge_destinations(db: Session, account: ConnectorAccount, dests: list[str]) -> dict:
    """Delete only the recovery points stored at the selected destinations. When
    no recovery points remain anywhere, the source's data is gone everywhere so
    its index + account are removed too."""
    from ..models import SearchDocument, SnapshotReceipt
    coll_ids = [c.id for c in db.query(Collection)
                .filter(Collection.connector_account_id == account.id).all()]
    if not coll_ids:
        db.delete(account)
        return {"documents": 0, "recovery_points": 0, "collections": 0, "removed": True}
    receipts = db.query(SnapshotReceipt).filter(
        SnapshotReceipt.collection_id.in_(coll_ids),
        SnapshotReceipt.destination.in_(dests)).delete(synchronize_session=False)
    remaining = db.query(SnapshotReceipt).filter(
        SnapshotReceipt.collection_id.in_(coll_ids)).count()
    if remaining == 0:
        docs = db.query(SearchDocument).filter(
            SearchDocument.collection_id.in_(coll_ids)).delete(synchronize_session=False)
        db.query(Collection).filter(Collection.id.in_(coll_ids)).delete(synchronize_session=False)
        db.delete(account)
        return {"documents": int(docs or 0), "recovery_points": int(receipts or 0),
                "collections": len(coll_ids), "removed": True}
    return {"documents": 0, "recovery_points": int(receipts or 0),
            "collections": 0, "removed": False}


def _node_purge(node, account_id: str, tenant_id: str,
                destinations: list[str] | None = None) -> dict | None:
    import httpx
    from .site import _fleet_secret
    url = (node.endpoint or "").rstrip("/") + "/nodes/sync/purge"
    try:
        with httpx.Client(timeout=30) as c:
            r = c.post(url, json={"account_id": account_id, "tenant_id": tenant_id,
                                  "destinations": destinations},
                       headers={"Authorization": f"Bearer {_fleet_secret()}"})
            r.raise_for_status()
            return r.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("node purge failed: %s", exc)
        return None


@router.get("/accounts/{account_id}/purge-targets")
def purge_targets(account_id: str,
                  principal: security.Principal = Depends(security.get_principal),
                  tenant: Tenant = Depends(security.get_tenant),
                  db: Session = Depends(get_db)):
    """Destinations this source's data is stored at (so the customer can choose
    where to purge from), plus whether its mapping must be disabled first."""
    from ..models import SnapshotReceipt
    from .search import _location_label, _store_label_map
    account = db.get(ConnectorAccount, account_id)
    if not account or account.tenant_id != tenant.id or account.owner_user_id != principal.user_id:
        raise HTTPException(404, "account not found")
    coll_ids = [c.id for c in db.query(Collection)
                .filter(Collection.connector_account_id == account.id).all()]
    store_labels = _store_label_map(db, tenant.id)
    agg: dict[str, dict] = {}
    if coll_ids:
        for rc in (db.query(SnapshotReceipt)
                   .filter(SnapshotReceipt.collection_id.in_(coll_ids)).all()):
            d = agg.setdefault(rc.destination, {"id": rc.destination,
                               "label": _location_label(rc.destination, store_labels),
                               "recovery_points": 0, "bytes": 0})
            d["recovery_points"] += 1
            d["bytes"] += int(rc.total_bytes or 0)
    # A source's mapping must be disabled (source deactivated) before purging.
    return {"active": bool(account.active),
            "destinations": sorted(agg.values(), key=lambda x: x["label"])}


class PurgeBody(BaseModel):
    destinations: list[str] | None = None  # subset of destination ids, or None/["all"] = everywhere


@router.post("/accounts/{account_id}/purge")
def purge(account_id: str, body: PurgeBody = PurgeBody(),
          principal: security.Principal = Depends(security.get_principal),
          tenant: Tenant = Depends(security.get_tenant),
          db: Session = Depends(get_db)):
    """Permanently delete data captured from a source, optionally only from chosen
    destinations. Irreversible. Requires the source's mapping to be disabled first
    and the ``purge_enabled`` capability flag (an admin clears it for a legal hold)."""
    from .. import features
    from ..models import Node, User
    account = db.get(ConnectorAccount, account_id)
    if not account or account.tenant_id != tenant.id or account.owner_user_id != principal.user_id:
        raise HTTPException(404, "account not found")
    user = db.get(User, principal.user_id)
    if not features.resolve(user, tenant, "purge_enabled"):
        raise HTTPException(403, "Data purge is disabled for this account (legal hold).")
    # The mapping must be disabled/removed first: a still-active source keeps
    # collecting, so purging underneath it would immediately re-accrue data.
    if account.active:
        raise HTTPException(409, "Deactivate this source (disable its data mapping) before purging.")
    dests = body.destinations
    all_mode = (not dests) or ("all" in dests)
    # Federated: purge the node's local data first, then the control-plane copies.
    node_result = None
    if get_settings().node_sync_scope and tenant.node_id:
        node = db.get(Node, tenant.node_id)
        if node and node.endpoint:
            node_result = _node_purge(node, account_id, tenant.id,
                                      None if all_mode else dests)
    label = account.account_label
    counts = _purge_source_local(db, account) if all_mode else _purge_destinations(db, account, dests)
    db.commit()
    audit.record(db, actor=principal.user_id, action="connector.purged",
                 tenant_id=tenant.id, resource=account_id, category="security",
                 severity="warning",
                 detail={"label": label, "destinations": "all" if all_mode else dests, **counts})
    return {"ok": True, **counts, "node": node_result}


def _identity_from_id_token(tokens: dict) -> str | None:
    """Extract email/username from an OIDC id_token (Google/Microsoft/LinkedIn).
    The token came from the provider's token endpoint over TLS, so we read the
    claims without re-verifying the signature."""
    idt = tokens.get("id_token")
    if not idt or idt.count(".") < 2:
        return None
    try:
        payload = idt.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # pad base64url
        claims = json.loads(base64.urlsafe_b64decode(payload).decode())
        return (claims.get("email") or claims.get("preferred_username")
                or claims.get("upn") or claims.get("name"))
    except Exception:
        return None


def _fetch_account_label(connector_type: str, tokens: dict) -> str | None:
    """Best-effort human label (email / username) from the provider. Prefers the
    OIDC id_token, then a provider identity API."""
    import httpx

    ident = _identity_from_id_token(tokens)
    if ident:
        return ident

    at = tokens.get("access_token")
    if not at:
        return None
    headers = {"Authorization": f"Bearer {at}"}

    def _json(client: "httpx.Client", method: str, url: str, **kw) -> dict:
        try:
            r = client.request(method, url, **kw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("identity fetch %s failed (%s): %s", connector_type, url, exc)
            return {}
        if r.status_code >= 400:
            logger.warning("identity fetch %s → HTTP %d (%s): %s",
                           connector_type, r.status_code, url, r.text[:200])
            return {}
        try:
            return r.json()
        except Exception:
            return {}

    try:
        with httpx.Client(timeout=15) as client:
            if connector_type == "gmail":
                d = _json(client, "GET", "https://gmail.googleapis.com/gmail/v1/users/me/profile",
                          headers=headers)
                return d.get("emailAddress")
            if connector_type in ("google_contacts", "google_calendar", "google_photos"):
                d = _json(client, "GET", "https://www.googleapis.com/oauth2/v3/userinfo",
                          headers=headers)
                return d.get("email")
            if connector_type in ("outlook", "onedrive"):
                d = _json(client, "GET", "https://graph.microsoft.com/v1.0/me", headers=headers)
                return d.get("userPrincipalName") or d.get("mail")
            if connector_type == "dropbox":
                d = _json(client, "POST", "https://api.dropboxapi.com/2/users/get_current_account",
                          headers=headers)
                email = d.get("email")
                if email:
                    return email
                nm = d.get("name") or {}
                return nm.get("display_name") if isinstance(nm, dict) else None
            if connector_type == "reddit":
                d = _json(client, "GET", "https://oauth.reddit.com/api/v1/me",
                          headers={**headers, "User-Agent": "web:life.arkive:v1 (Arkive backup)"})
                name = d.get("name")
                return f"u/{name}" if name else None
            if connector_type == "facebook":
                d = _json(client, "GET", "https://graph.facebook.com/v19.0/me",
                          params={"fields": "name,email", "access_token": at})
                return d.get("email") or d.get("name")
            if connector_type == "instagram":
                d = _json(client, "GET", "https://graph.instagram.com/me",
                          params={"fields": "username", "access_token": at})
                name = d.get("username")
                return f"@{name}" if name else None
            if connector_type == "linkedin":
                d = _json(client, "GET", "https://api.linkedin.com/v2/userinfo", headers=headers)
                return d.get("email") or d.get("name")
    except Exception as exc:  # noqa: BLE001
        logger.warning("identity fetch failed for %s: %s", connector_type, exc)
    return None

