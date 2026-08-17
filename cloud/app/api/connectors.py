"""Connector catalog + account linking (sync-worker sources)."""

from __future__ import annotations

import base64
import os

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from cv_crypto.provider import get_provider

from .. import audit, security
from ..connectors import ALL_CONNECTORS, get_connector
from ..db import get_db
from ..models import ConnectorAccount, Tenant

router = APIRouter(prefix="/connectors", tags=["connectors"])


@router.get("/catalog")
def catalog():
    out = []
    for c in ALL_CONNECTORS:
        spec = c.oauth_spec()
        caps = c.capabilities()
        out.append({
            "type": spec.connector_type,
            "displayName": spec.display_name,
            "authType": spec.auth_type,
            "authorizeUrl": spec.authorize_url,
            "scopes": spec.scopes,
            "icon": spec.icon,
            "color": spec.color,
            "docTypes": spec.doc_types,
            "capabilities": {
                "incremental": caps.incremental,
                "supportsPagination": caps.supports_pagination,
                "rateLimitPerMin": caps.rate_limit_per_min,
                "searchableFields": caps.searchable_fields,
                "facetFields": caps.facet_fields,
            },
        })
    return out


class LinkRequest(BaseModel):
    connector_type: str
    account_label: str
    # Prototype: authorization proof stands in for a completed OAuth flow.
    authorization_proof: str = "demo-oauth-grant"


@router.post("/link")
def link_account(body: LinkRequest,
                 principal: security.Principal = Depends(security.require_passkey),
                 tenant: Tenant = Depends(security.get_tenant),
                 db: Session = Depends(get_db)):
    connector = get_connector(body.connector_type)
    if not connector:
        return {"error": "unknown connector"}
    spec = connector.oauth_spec()
    # Encrypt the credential blob at rest (never plaintext).
    provider = get_provider()
    kek = provider.hkdf(
        (os.environ.get("CV_KEK_SECRET", "dev-kek") + tenant.id).encode(),
        b"connector-cred", 32,
    )
    nonce, ct = provider.aes_encrypt(kek, body.authorization_proof.encode(), b"cred")
    blob = base64.b64encode(nonce + ct).decode()

    account = ConnectorAccount(
        tenant_id=tenant.id,
        connector_type=body.connector_type,
        account_label=body.account_label,
        auth_status="linked",
        encrypted_credentials=blob,
        scopes=spec.scopes,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    audit.record(db, actor=principal.user_id, action="connector.linked",
                 tenant_id=tenant.id, resource=account.id,
                 detail={"type": body.connector_type})
    return {"id": account.id, "connector_type": account.connector_type,
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
