"""Connector catalog + real OAuth / token account linking (sync-worker sources)."""

from __future__ import annotations

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
from ..db import get_db
from ..models import ConnectorAccount, Tenant

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
    return []


@router.get("/catalog")
def catalog(tenant: Tenant = Depends(security.get_tenant)):
    out = []
    for c in ALL_CONNECTORS:
        spec = c.oauth_spec()
        caps = c.capabilities()
        ctype = spec.connector_type
        mode = "oauth" if oauth.is_oauth(ctype) else "token"
        out.append({
            "type": ctype,
            "displayName": spec.display_name,
            "authType": spec.auth_type,
            "icon": spec.icon,
            "color": spec.color,
            "docTypes": spec.doc_types,
            "mode": mode,
            "configured": oauth.is_configured(ctype),
            "requiresAgent": caps.requires_agent,
            "setup": _setup_instructions(ctype),
            "capabilities": {
                "incremental": caps.incremental,
                "searchableFields": caps.searchable_fields,
                "facetFields": caps.facet_fields,
            },
        })
    return out


class ConnectRequest(BaseModel):
    account_label: str | None = None


@router.post("/{connector_type}/connect")
def connect(connector_type: str, body: ConnectRequest,
            principal: security.Principal = Depends(security.require_passkey),
            tenant: Tenant = Depends(security.get_tenant)):
    if not get_connector(connector_type):
        raise HTTPException(404, "unknown connector")
    if oauth.is_oauth(connector_type):
        if not oauth.is_configured(connector_type):
            raise HTTPException(400, {
                "error": "provider_not_configured",
                "setup": _setup_instructions(connector_type),
            })
        state = oauth.sign_state({
            "tid": tenant.id, "uid": principal.user_id,
            "type": connector_type, "label": body.account_label or "",
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
    try:
        tokens = oauth.exchange_code(connector_type, code)
    except Exception as exc:
        logger.error("OAuth token exchange failed for %s: %s", connector_type, exc)
        return RedirectResponse(f"{portal}/connectors?error=token_exchange")

    label = data.get("label") or _fetch_account_label(connector_type, tokens) \
        or f"{connector_type} account"
    account = ConnectorAccount(
        tenant_id=data["tid"],
        connector_type=connector_type,
        account_label=label,
        auth_status="linked",
        encrypted_credentials=credstore.encrypt(data["tid"], tokens),
        scopes=(tokens.get("scope") or "").split(),
    )
    db.add(account)
    db.commit()
    audit.record(db, actor=data["uid"], action="connector.linked",
                 tenant_id=data["tid"], resource=account.id,
                 detail={"type": connector_type})
    return RedirectResponse(f"{portal}/connectors?connected={connector_type}")


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
        connector_type=connector_type,
        account_label=body.account_label,
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
def list_accounts(tenant: Tenant = Depends(security.get_tenant),
                  db: Session = Depends(get_db)):
    accounts = db.query(ConnectorAccount).filter(
        ConnectorAccount.tenant_id == tenant.id).all()
    return [{"id": a.id, "connector_type": a.connector_type,
             "account_label": a.account_label, "auth_status": a.auth_status,
             "last_sync_at": a.last_sync_at.isoformat() if a.last_sync_at else None,
             "scopes": a.scopes} for a in accounts]


@router.delete("/accounts/{account_id}")
def unlink(account_id: str,
           principal: security.Principal = Depends(security.require_security_admin),
           tenant: Tenant = Depends(security.get_tenant),
           db: Session = Depends(get_db)):
    account = db.get(ConnectorAccount, account_id)
    if not account or account.tenant_id != tenant.id:
        raise HTTPException(404, "account not found")
    db.delete(account)
    db.commit()
    audit.record(db, actor=principal.user_id, action="connector.unlinked",
                 tenant_id=tenant.id, resource=account_id)
    return {"ok": True}


def _fetch_account_label(connector_type: str, tokens: dict) -> str | None:
    """Best-effort human label (email / name) from the provider."""
    import httpx

    at = tokens.get("access_token")
    if not at:
        return None
    headers = {"Authorization": f"Bearer {at}"}
    try:
        with httpx.Client(timeout=15) as client:
            if connector_type == "gmail":
                r = client.get("https://gmail.googleapis.com/gmail/v1/users/me/profile",
                               headers=headers)
                return r.json().get("emailAddress")
            if connector_type in ("outlook", "onedrive"):
                r = client.get("https://graph.microsoft.com/v1.0/me", headers=headers)
                d = r.json()
                return d.get("userPrincipalName") or d.get("mail")
            if connector_type == "dropbox":
                r = client.post("https://api.dropboxapi.com/2/users/get_current_account",
                                headers=headers)
                return r.json().get("email")
    except Exception:
        return None
    return None

