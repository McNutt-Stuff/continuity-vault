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
    # Reddit requires HTTP Basic auth (id:secret) + a descriptive User-Agent on
    # the token endpoint instead of client creds in the body.
    basic_auth: bool = False
    user_agent: Optional[str] = None


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
        "google_contacts": ProviderSpec(
            connector_type="google_contacts",
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",
            scopes=["openid", "https://www.googleapis.com/auth/userinfo.email",
                    "https://www.googleapis.com/auth/contacts.readonly"],
            client_id=s.google_client_id,
            client_secret=s.google_client_secret,
            extra_auth_params={"access_type": "offline", "prompt": "consent"},
        ),
        "google_calendar": ProviderSpec(
            connector_type="google_calendar",
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",
            scopes=["openid", "https://www.googleapis.com/auth/userinfo.email",
                    "https://www.googleapis.com/auth/calendar.readonly"],
            client_id=s.google_client_id,
            client_secret=s.google_client_secret,
            extra_auth_params={"access_type": "offline", "prompt": "consent"},
        ),
        "google_photos": ProviderSpec(
            connector_type="google_photos",
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",
            scopes=["openid", "https://www.googleapis.com/auth/userinfo.email",
                    "https://www.googleapis.com/auth/photospicker.mediaitems.readonly"],
            client_id=s.google_client_id,
            client_secret=s.google_client_secret,
            extra_auth_params={"access_type": "offline", "prompt": "consent"},
        ),
        "reddit": ProviderSpec(
            connector_type="reddit",
            authorize_url="https://www.reddit.com/api/v1/authorize",
            token_url="https://www.reddit.com/api/v1/access_token",
            scopes=["identity", "history", "read", "privatemessages"],
            client_id=s.reddit_client_id,
            client_secret=s.reddit_client_secret,
            extra_auth_params={"duration": "permanent"},
            basic_auth=True,
            user_agent="web:life.arkive:v1 (Arkive backup)",
        ),
        "facebook": ProviderSpec(
            connector_type="facebook",
            authorize_url="https://www.facebook.com/v19.0/dialog/oauth",
            token_url="https://graph.facebook.com/v19.0/oauth/access_token",
            scopes=["public_profile", "user_posts", "user_photos"],
            client_id=s.facebook_client_id,
            client_secret=s.facebook_client_secret,
            extra_auth_params={},
        ),
        "instagram": ProviderSpec(
            connector_type="instagram",
            authorize_url="https://api.instagram.com/oauth/authorize",
            token_url="https://api.instagram.com/oauth/access_token",
            scopes=["user_profile", "user_media"],
            client_id=s.instagram_client_id,
            client_secret=s.instagram_client_secret,
            extra_auth_params={},
        ),
        "linkedin": ProviderSpec(
            connector_type="linkedin",
            authorize_url="https://www.linkedin.com/oauth/v2/authorization",
            token_url="https://www.linkedin.com/oauth/v2/accessToken",
            scopes=(s.linkedin_scopes or "openid profile email").split(),
            client_id=s.linkedin_client_id,
            client_secret=s.linkedin_client_secret,
            extra_auth_params={},
        ),
    }


OAUTH_TYPES = set(_providers().keys())
# Providers that authorize with a manually-entered token / app password.
TOKEN_TYPES = {"onepassword", "icloud"}


def get_spec(connector_type: str) -> Optional[ProviderSpec]:
    spec = _providers().get(connector_type)
    if spec:
        # Admin-managed Config Objects override env client id/secret when set.
        try:
            from ..platform_config import source_values
            ov = source_values(connector_type)
            if ov.get("client_id"):
                spec.client_id = ov["client_id"]
            if ov.get("client_secret"):
                spec.client_secret = ov["client_secret"]
        except Exception:
            pass
    return spec


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
    }
    headers = {"Accept": "application/json"}
    if spec.user_agent:
        headers["User-Agent"] = spec.user_agent
    auth = None
    if spec.basic_auth:
        auth = (spec.client_id or "", spec.client_secret or "")
    else:
        data["client_id"] = spec.client_id
        data["client_secret"] = spec.client_secret
    with httpx.Client(timeout=30) as client:
        r = client.post(spec.token_url, data=data, headers=headers, auth=auth)
        if r.status_code >= 400:
            raise RuntimeError(
                f"{connector_type} token exchange failed ({r.status_code}): {r.text}")
        tok = r.json()
    return _normalize(tok, spec)


def refresh_tokens(connector_type: str, refresh_token: str) -> dict:
    # Evernote uses its MCP OAuth server (discovered endpoints), not a static spec.
    if connector_type == "evernote":
        from . import evernote_mcp
        return evernote_mcp.refresh(refresh_token, redirect_uri())
    spec = get_spec(connector_type)
    if not spec:
        raise ValueError(f"unknown OAuth provider {connector_type}")
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    headers = {"Accept": "application/json"}
    if spec.user_agent:
        headers["User-Agent"] = spec.user_agent
    auth = None
    if spec.basic_auth:
        auth = (spec.client_id or "", spec.client_secret or "")
    else:
        data["client_id"] = spec.client_id
        data["client_secret"] = spec.client_secret
    with httpx.Client(timeout=30) as client:
        r = client.post(spec.token_url, data=data, headers=headers, auth=auth)
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
