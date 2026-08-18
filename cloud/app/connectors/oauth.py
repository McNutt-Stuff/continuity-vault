"""
Real OAuth2 authorization-code flow for connector providers.

Each OAuth provider is configured with a client id/secret (see config). The flow:

  connect  -> build a provider consent URL (with a signed `state`)
  consent  -> the user authorizes at the provider
  callback -> exchange the code for access/refresh tokens (stored encrypted)
  sync     -> the sync worker uses the access token (refreshing as needed)

Providers without a simple OAuth model (1Password, iCloud) use a token / app-
password entry flow instead (see the API layer).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.parse import urlencode

import httpx
from itsdangerous import BadSignature, URLSafeTimedSerializer

from ..config import get_settings

settings = get_settings()
_state_signer = URLSafeTimedSerializer(settings.session_secret, salt="cv-oauth-state")


@dataclass
class ProviderSpec:
    connector_type: str
    authorize_url: str
    token_url: str
    scopes: List[str]
    client_id: Optional[str]
    client_secret: Optional[str]
    # Extra params appended to the authorize request (provider-specific).
    extra_auth_params: Dict[str, str]


def _providers() -> Dict[str, ProviderSpec]:
    s = settings
    ms_tenant = s.microsoft_tenant or "common"
    return {
        "gmail": ProviderSpec(
            connector_type="gmail",
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
            client_id=s.google_client_id,
            client_secret=s.google_client_secret,
            extra_auth_params={"access_type": "offline", "prompt": "consent"},
        ),
        "outlook": ProviderSpec(
            connector_type="outlook",
            authorize_url=f"https://login.microsoftonline.com/{ms_tenant}/oauth2/v2.0/authorize",
            token_url=f"https://login.microsoftonline.com/{ms_tenant}/oauth2/v2.0/token",
            scopes=["offline_access", "Mail.Read"],
            client_id=s.microsoft_client_id,
            client_secret=s.microsoft_client_secret,
            extra_auth_params={},
        ),
        "onedrive": ProviderSpec(
            connector_type="onedrive",
            authorize_url=f"https://login.microsoftonline.com/{ms_tenant}/oauth2/v2.0/authorize",
            token_url=f"https://login.microsoftonline.com/{ms_tenant}/oauth2/v2.0/token",
            scopes=["offline_access", "Files.Read.All"],
            client_id=s.microsoft_client_id,
            client_secret=s.microsoft_client_secret,
            extra_auth_params={},
        ),
        "dropbox": ProviderSpec(
            connector_type="dropbox",
            authorize_url="https://www.dropbox.com/oauth2/authorize",
            token_url="https://api.dropboxapi.com/oauth2/token",
            scopes=["files.metadata.read", "files.content.read", "account_info.read"],
            client_id=s.dropbox_client_id,
            client_secret=s.dropbox_client_secret,
            extra_auth_params={"token_access_type": "offline"},
        ),
    }


OAUTH_TYPES = set(_providers().keys())
# Providers that authorize with a manually-entered token / app password.
TOKEN_TYPES = {"onepassword", "icloud"}


def get_spec(connector_type: str) -> Optional[ProviderSpec]:
    return _providers().get(connector_type)


def is_oauth(connector_type: str) -> bool:
    return connector_type in OAUTH_TYPES


def is_configured(connector_type: str) -> bool:
    spec = get_spec(connector_type)
    if spec:
        return bool(spec.client_id and spec.client_secret)
    # Token-based providers are configured per account, so treat as available.
    return connector_type in TOKEN_TYPES


def redirect_uri() -> str:
    base = settings.oauth_redirect_base or f"https://{settings.domain}/api"
    return f"{base.rstrip('/')}/connectors/oauth/callback"


def sign_state(payload: dict) -> str:
    return _state_signer.dumps(payload)


def read_state(state: str, max_age: int = 900) -> Optional[dict]:
    try:
        return _state_signer.loads(state, max_age=max_age)
    except BadSignature:
        return None


def authorize_url(connector_type: str, state: str) -> str:
    spec = get_spec(connector_type)
    if not spec or not spec.client_id:
        raise ValueError(f"{connector_type} OAuth is not configured")
    params = {
        "client_id": spec.client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri(),
        "scope": " ".join(spec.scopes),
        "state": state,
        **spec.extra_auth_params,
    }
    return f"{spec.authorize_url}?{urlencode(params)}"


def exchange_code(connector_type: str, code: str) -> dict:
    spec = get_spec(connector_type)
    if not spec:
        raise ValueError(f"unknown OAuth provider {connector_type}")
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(),
        "client_id": spec.client_id,
        "client_secret": spec.client_secret,
    }
    with httpx.Client(timeout=30) as client:
        r = client.post(spec.token_url, data=data,
                        headers={"Accept": "application/json"})
        if r.status_code >= 400:
            raise RuntimeError(
                f"{connector_type} token exchange failed ({r.status_code}): {r.text}")
        tok = r.json()
    return _normalize(tok, spec)


def refresh_tokens(connector_type: str, refresh_token: str) -> dict:
    spec = get_spec(connector_type)
    if not spec:
        raise ValueError(f"unknown OAuth provider {connector_type}")
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": spec.client_id,
        "client_secret": spec.client_secret,
    }
    with httpx.Client(timeout=30) as client:
        r = client.post(spec.token_url, data=data,
                        headers={"Accept": "application/json"})
        r.raise_for_status()
        tok = r.json()
    normalized = _normalize(tok, spec)
    # Some providers omit a new refresh token; keep the existing one.
    if not normalized.get("refresh_token"):
        normalized["refresh_token"] = refresh_token
    return normalized


def _normalize(tok: dict, spec: ProviderSpec) -> dict:
    expires_in = int(tok.get("expires_in", 3600))
    return {
        "access_token": tok.get("access_token"),
        "refresh_token": tok.get("refresh_token"),
        "expires_at": time.time() + expires_in - 60,
        "scope": tok.get("scope", " ".join(spec.scopes)),
        "provider": spec.connector_type,
    }
