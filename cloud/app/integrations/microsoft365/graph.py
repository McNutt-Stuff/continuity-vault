"""Microsoft Graph — app-only (client-credentials) client.

Used by the node-side managed collector. No reusable secret lives in the control
plane: the box running collection reads the Arkive Entra app's client_id /
client_secret from the linked ConfigObject (``platform_config.integration_values``)
and mints a per-Microsoft-tenant app-only token here, on demand.

Only read scopes are ever exercised (User.Read.All etc., app-only via
admin-consent). Errors preserve the HTTP status + the provider's error code and a
bounded response snippet so failures are triageable from Platform Logs.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger("cv.integrations.m365.graph")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_AUTHORITY = "https://login.microsoftonline.com"

# microsoft_tenant_id -> (access_token, expires_at_epoch)
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


class GraphError(Exception):
    """A Graph/token call failed. Carries the HTTP status, the provider error
    code/reason and a bounded body snippet (never tokens/secrets)."""

    def __init__(self, status: int, reason: str, snippet: str = ""):
        self.status = status
        self.reason = reason or ""
        self.snippet = (snippet or "")[:200]
        super().__init__(f"graph {status} {self.reason}: {self.snippet}".strip())


def _error(resp) -> GraphError:
    reason = ""
    try:
        j = resp.json()
        err = j.get("error") if isinstance(j, dict) else None
        if isinstance(err, dict):
            reason = err.get("code") or err.get("message") or ""
        elif isinstance(j, dict):
            reason = j.get("error_description") or j.get("error") or ""
    except Exception:  # noqa: BLE001
        reason = ""
    body = ""
    try:
        body = resp.text[:200]
    except Exception:  # noqa: BLE001
        body = ""
    return GraphError(resp.status_code, reason, body)


def app_token(client_id: str, client_secret: str, microsoft_tenant_id: str,
              *, force: bool = False) -> str:
    """Mint (and briefly cache) an app-only Graph token via client-credentials.
    ``force`` bypasses the cache so a token minted before admin consent took
    effect isn't reused (that would keep returning 403 until it expired)."""
    now = time.time()
    if not force:
        cached = _TOKEN_CACHE.get(microsoft_tenant_id)
        if cached and cached[1] - 60 > now:
            return cached[0]
    import httpx
    url = f"{_AUTHORITY}/{microsoft_tenant_id}/oauth2/v2.0/token"
    data = {"client_id": client_id, "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials"}
    with httpx.Client(timeout=30) as c:
        r = c.post(url, data=data)
        if r.status_code != 200:
            raise _error(r)
        j = r.json()
    tok = j.get("access_token") or ""
    if not tok:
        raise GraphError(r.status_code, "no access_token in token response")
    _TOKEN_CACHE[microsoft_tenant_id] = (tok, now + int(j.get("expires_in", 3600)))
    return tok


def token_claims(token: str) -> dict:
    """Decode a JWT payload WITHOUT verifying it — diagnostics only (no secret).
    Lets a 403 report the granted ``roles`` and issuing tenant (``tid``)."""
    try:
        import base64
        import json
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return {}



def get_paged(token: str, path: str, params: dict | None = None, cap: int = 200000):
    """Yield items across ``@odata.nextLink`` pages for a Graph collection GET."""
    import httpx
    url = path if path.startswith("http") else GRAPH_BASE + path
    n = 0
    with httpx.Client(timeout=60) as c:
        while url:
            r = c.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
            params = None  # a nextLink already encodes the query
            if r.status_code == 429 or r.status_code >= 500:
                wait = min(30, int(r.headers.get("Retry-After", "5") or 5))
                logger.warning("graph throttled/5xx (%s) on %s — waiting %ds",
                               r.status_code, path, wait)
                time.sleep(wait)
                continue
            if r.status_code != 200:
                raise _error(r)
            j = r.json()
            for it in j.get("value", []):
                yield it
                n += 1
                if n >= cap:
                    return
            url = j.get("@odata.nextLink")


def list_sites(token: str, cap: int = 500):
    """Organization SharePoint sites the app can see (Sites.Read.All, app-only)."""
    return get_paged(token, "/sites", params={"search": "*", "$top": "100"}, cap=cap)


def list_teams(token: str, cap: int = 500):
    """Microsoft 365 groups that are Teams (app-only). Team id == group id, so the
    channel/message endpoints use the same id."""
    return get_paged(token, "/groups", cap=cap, params={
        "$filter": "resourceProvisioningOptions/Any(x:x eq 'Team')",
        "$select": "id,displayName", "$top": "100"})

