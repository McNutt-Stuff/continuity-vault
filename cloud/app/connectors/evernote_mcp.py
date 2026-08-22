"""
Evernote via its MCP server (https://mcp.evernote.com/mcp).

Evernote's legacy OAuth 1.0a / Thrift API is deprecated; the supported path is
their remote MCP server, a standard Streamable-HTTP MCP endpoint secured with
OAuth 2.0. This module implements exactly what a *server-side backup client*
needs:

  * OAuth 2.1 discovery — RFC 9728 protected-resource metadata → RFC 8414
    authorization-server metadata (authorize / token / registration endpoints).
  * Dynamic Client Registration (RFC 7591), cached to disk so the registered
    client is stable/auditable after the first run (Evernote offers no static
    app registration). A static client (CV_EVERNOTE_CLIENT_ID/SECRET) is used
    instead when configured.
  * Authorization-code + PKCE flow (authorize URL, code exchange, refresh).
  * A minimal MCP JSON-RPC client (initialize + tools/call) that enumerates
    notes and attachments via the server's tools (search_notes, get_note,
    get_attachment) and yields normalized ``SourceObject`` records.

Everything is heavily guarded and optional: any misconfiguration or beta change
degrades to "no objects" so the platform is never broken. NOTE: the Evernote MCP
server is in beta — tool argument/response shapes may change and this needs live
validation against a real account.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from typing import Iterable, Optional, Tuple

import httpx

from .base import SourceObject
from ..config import get_settings
from ..taxonomy import classify_file

logger = logging.getLogger("cv.connectors.evernote_mcp")

MCP_PROTOCOL_VERSION = "2025-06-18"
_DEFAULT_CAP = 268435456  # 256 MiB

_discovery_cache: Optional[dict] = None
_client_cache: Optional[dict] = None


# --------------------------------------------------------------------------- #
# OAuth 2.1 discovery + dynamic client registration                           #
# --------------------------------------------------------------------------- #

def _mcp_url() -> str:
    return get_settings().evernote_mcp_url.rstrip("/")


def _well_known(base: str, suffix: str) -> str:
    # RFC 8414 / 9728: /.well-known/<name> is inserted after the origin.
    from urllib.parse import urlsplit
    p = urlsplit(base)
    return f"{p.scheme}://{p.netloc}/.well-known/{suffix}"


def discover() -> dict:
    """Resolve the authorization/token/registration endpoints for the MCP server.
    Cached for the process. Returns {} if discovery fails."""
    global _discovery_cache
    if _discovery_cache is not None:
        return _discovery_cache
    mcp = _mcp_url()
    auth_server = None
    with httpx.Client(timeout=20, follow_redirects=True) as c:
        # 1) Protected-resource metadata → which authorization server to use.
        for url in (_well_known(mcp, "oauth-protected-resource"),
                    mcp + "/.well-known/oauth-protected-resource"):
            try:
                r = c.get(url)
                if r.status_code < 400:
                    servers = r.json().get("authorization_servers") or []
                    if servers:
                        auth_server = servers[0].rstrip("/")
                        break
            except Exception:
                continue
        # Evernote documents accounts.evernote.com as the consent origin.
        if not auth_server:
            auth_server = "https://accounts.evernote.com"
        # 2) Authorization-server metadata → concrete endpoints.
        meta = {}
        for url in (_well_known(auth_server, "oauth-authorization-server"),
                    _well_known(auth_server, "openid-configuration")):
            try:
                r = c.get(url)
                if r.status_code < 400:
                    meta = r.json()
                    break
            except Exception:
                continue
    if not meta:
        logger.warning("Evernote MCP: could not discover OAuth metadata")
        return {}
    _discovery_cache = {
        "authorization_endpoint": meta.get("authorization_endpoint"),
        "token_endpoint": meta.get("token_endpoint"),
        "registration_endpoint": meta.get("registration_endpoint"),
        "userinfo_endpoint": meta.get("userinfo_endpoint"),
        "scopes_supported": meta.get("scopes_supported") or [],
        "resource": mcp,
    }
    return _discovery_cache


def fetch_identity(tokens: dict) -> Optional[str]:
    """Resolve the linked Evernote account's email/username: prefer the OIDC
    id_token claims, then the discovered userinfo endpoint. None if unavailable."""
    idt = tokens.get("id_token")
    if idt and idt.count(".") >= 2:
        try:
            payload = idt.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload).decode())
            ident = (claims.get("email") or claims.get("preferred_username")
                     or claims.get("name"))
            if ident:
                return ident
        except Exception:
            pass
    at = tokens.get("access_token")
    ui = (discover() or {}).get("userinfo_endpoint")
    if at and ui:
        try:
            with httpx.Client(timeout=15) as c:
                r = c.get(ui, headers={"Authorization": f"Bearer {at}"})
                if r.status_code < 400:
                    d = r.json()
                    return (d.get("email") or d.get("preferred_username")
                            or d.get("name") or d.get("username"))
                logger.warning("Evernote MCP: userinfo → HTTP %d", r.status_code)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Evernote MCP: userinfo failed: %s", exc)
    return None


def _client_cache_path() -> str:
    base = os.environ.get("CV_KEY_STORE") or "."
    return os.path.join(base, "evernote_mcp_client.json")


def client_credentials(redirect_uri: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (client_id, client_secret). Prefers a configured static client,
    else a cached dynamic registration, else registers dynamically (RFC 7591)."""
    global _client_cache
    s = get_settings()
    if s.evernote_client_id:
        return s.evernote_client_id, s.evernote_client_secret
    if _client_cache:
        return _client_cache.get("client_id"), _client_cache.get("client_secret")
    path = _client_cache_path()
    try:
        if os.path.exists(path):
            _client_cache = json.load(open(path))
            return _client_cache.get("client_id"), _client_cache.get("client_secret")
    except Exception:
        pass
    disc = discover()
    reg = disc.get("registration_endpoint")
    if not reg:
        logger.warning("Evernote MCP: no registration endpoint; set CV_EVERNOTE_CLIENT_ID")
        return None, None
    # MCP clients register as public clients that authenticate with PKCE (no
    # client secret) — that's what Evernote's DCR expects.
    body = {
        "client_name": "Arkive Continuity Vault",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "application_type": "web",
    }
    scope = " ".join(disc.get("scopes_supported") or [])
    if scope:
        body["scope"] = scope
    try:
        with httpx.Client(timeout=20) as c:
            r = c.post(reg, json=body, headers={"Accept": "application/json"})
            if r.status_code >= 400:
                logger.warning("Evernote MCP DCR rejected (%s) at %s: %s",
                               r.status_code, reg, r.text[:600])
                return None, None
            data = r.json()
        _client_cache = {"client_id": data.get("client_id"),
                         "client_secret": data.get("client_secret")}
        try:
            with open(path, "w") as fh:
                json.dump(_client_cache, fh)
            os.chmod(path, 0o600)
        except Exception:
            pass
        return _client_cache.get("client_id"), _client_cache.get("client_secret")
    except Exception as exc:
        logger.warning("Evernote MCP: dynamic client registration failed: %s", exc)
        return None, None


def is_configured() -> bool:
    """True when we can obtain a client (static config or a reachable registration
    endpoint) — used by the catalog to show Evernote as connectable."""
    if get_settings().evernote_client_id:
        return True
    return bool(discover().get("registration_endpoint") or discover().get("authorization_endpoint"))


# --------------------------------------------------------------------------- #
# Authorization-code + PKCE                                                    #
# --------------------------------------------------------------------------- #

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def make_pkce() -> Tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def authorize_url(state: str, code_challenge: str, redirect_uri: str) -> str:
    from urllib.parse import urlencode
    disc = discover()
    cid, _ = client_credentials(redirect_uri)
    if not disc.get("authorization_endpoint") or not cid:
        raise ValueError("Evernote MCP OAuth is not available")
    params = {
        "response_type": "code",
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "scope": " ".join(disc.get("scopes_supported") or []),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "resource": disc["resource"],  # RFC 8707 — bind the token to the MCP server
    }
    return f"{disc['authorization_endpoint']}?{urlencode(params)}"


def _token_request(data: dict, redirect_uri: str) -> dict:
    disc = discover()
    cid, secret = client_credentials(redirect_uri)
    data = {**data, "client_id": cid, "resource": disc["resource"]}
    auth = (cid, secret) if secret else None
    with httpx.Client(timeout=30) as c:
        r = c.post(disc["token_endpoint"], data=data, auth=auth)
        r.raise_for_status()
        tokens = r.json()
    if tokens.get("expires_in"):
        tokens["expires_at"] = int(time.time()) + int(tokens["expires_in"]) - 60
    return tokens


def exchange_code(code: str, code_verifier: str, redirect_uri: str) -> dict:
    return _token_request({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }, redirect_uri)


def refresh(refresh_token: str, redirect_uri: str) -> dict:
    return _token_request({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }, redirect_uri)


# --------------------------------------------------------------------------- #
# Minimal MCP client (Streamable HTTP, JSON-RPC)                              #
# --------------------------------------------------------------------------- #

class _McpSession:
    def __init__(self, access_token: str):
        self._url = _mcp_url()
        self._token = access_token
        self._id = 0
        self._session_id: Optional[str] = None
        self._client = httpx.Client(timeout=60)

    def _headers(self) -> dict:
        h = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    def _parse(self, r: httpx.Response) -> dict:
        # Streamable HTTP returns either a JSON body or an SSE stream; take the
        # last JSON-RPC payload either way.
        ctype = r.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            payload = {}
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    try:
                        payload = json.loads(line[5:].strip())
                    except Exception:
                        continue
            return payload
        return r.json()

    def _call(self, method: str, params: Optional[dict] = None) -> dict:
        self._id += 1
        req = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            req["params"] = params
        r = self._client.post(self._url, headers=self._headers(), json=req)
        sid = r.headers.get("mcp-session-id") or r.headers.get("Mcp-Session-Id")
        if sid:
            self._session_id = sid
        r.raise_for_status()
        data = self._parse(r)
        if data.get("error"):
            raise RuntimeError(data["error"].get("message", "mcp error"))
        return data.get("result", {})

    def initialize(self) -> None:
        self._call("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "arkive-continuity-vault", "version": "1.0"},
        })
        # Best-effort initialized notification (no id).
        try:
            self._client.post(self._url, headers=self._headers(),
                              json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:
            pass

    def tool(self, name: str, arguments: dict) -> dict:
        return self._call("tools/call", {"name": name, "arguments": arguments})

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


def _tool_json(result: dict) -> object:
    """Extract structured data from an MCP tool result (structuredContent, or a
    JSON string in the first text content block, else the raw content)."""
    if not isinstance(result, dict):
        return result
    if isinstance(result.get("structuredContent"), (dict, list)):
        return result["structuredContent"]
    for block in result.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            try:
                return json.loads(block.get("text", ""))
            except Exception:
                return block.get("text", "")
    return result.get("content")


def _as_list(data: object, *keys: str) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


def _capped(raw: bytes, cap: int) -> Tuple[bytes, bool]:
    if len(raw) <= cap:
        return raw, True
    return json.dumps({"_arkive": "content_exceeds_cap", "bytes": len(raw)}).encode(), False


def _name(v, default: str = "") -> str:
    """Coerce a possibly-object value (tag/notebook) to a plain string name."""
    if isinstance(v, dict):
        return v.get("name") or v.get("title") or v.get("label") or default
    return str(v) if v not in (None, "") else default


def _strip_enml(enml: str) -> str:
    """ENML/HTML note body → plain, searchable text."""
    import html
    import re
    if not enml or "<" not in enml:
        return enml or ""
    text = re.sub(r"(?is)<(script|style|en-media)[^>]*>.*?</\1>", " ", enml)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _en_media(enml: str) -> list:
    """Extract (hash, mime) for every <en-media> attachment reference in ENML."""
    import re
    out = []
    for tag in re.findall(r"<en-media\b[^>]*?>", enml or ""):
        h = re.search(r'hash="([0-9a-f]{32})"', tag)
        if not h:
            continue
        t = re.search(r'type="([^"]+)"', tag)
        out.append((h.group(1), t.group(1) if t else "application/octet-stream"))
    return out


def _attachment_bytes(res: dict) -> bytes:
    """Pull raw bytes from a get_attachment result — either an MCP resource/image
    blob (base64) or a base64 field in the structured payload."""
    for block in (res.get("content") or []) if isinstance(res, dict) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "resource":
            r = block.get("resource") or {}
            b = r.get("blob") or r.get("data")
            if b:
                try:
                    return base64.b64decode(b)
                except Exception:
                    pass
        if block.get("type") in ("image", "audio", "blob") and block.get("data"):
            try:
                return base64.b64decode(block["data"])
            except Exception:
                pass
    data = _tool_json(res)
    if isinstance(data, dict):
        for k in ("data", "base64", "content", "bytes"):
            v = data.get(k)
            if isinstance(v, str):
                try:
                    return base64.b64decode(v)
                except Exception:
                    pass
    return b""


# --------------------------------------------------------------------------- #
# Fetch                                                                        #
# --------------------------------------------------------------------------- #

def fetch(access_token: str, content_cap: int = _DEFAULT_CAP,
          options: Optional[dict] = None) -> Iterable[SourceObject]:
    """Enumerate notes (and, when enabled, attachments) via the MCP tools and
    yield normalized objects. Best-effort against the beta server: any failure
    stops cleanly rather than raising."""
    options = options or {}

    def want(cat: str) -> bool:
        inc = options.get("includeCategories") or []
        return not inc or cat in inc

    if not access_token:
        return
    logger.warning("Evernote MCP: fetch starting (token len=%d)", len(access_token))
    session = _McpSession(access_token)
    try:
        session.initialize()
    except Exception as exc:
        logger.warning("Evernote MCP: initialize failed: %s", exc)
        session.close()
        # Surface the failure (e.g. a 401) so the sync worker records a real error
        # / needs-reauth instead of a misleading 0-object success.
        raise

    # Beta bring-up: the server's tool arg/response schemas aren't documented, so
    # log what it actually exposes and returns to guide the field mapping.
    try:
        listed = session._call("tools/list")
        names = [t.get("name") for t in (listed.get("tools") or [])]
        logger.warning("Evernote MCP: tools/list → %s", names)
        for t in (listed.get("tools") or []):
            if t.get("name") == "search_notes":
                logger.warning("Evernote MCP: search_notes inputSchema=%s",
                               json.dumps(t.get("inputSchema"))[:2500])
    except Exception as exc:
        logger.warning("Evernote MCP: tools/list failed: %s", exc)

    def _iso(s):
        try:
            from datetime import datetime as _d
            return _d.fromisoformat(str(s).replace("Z", "+00:00"))
        except Exception:
            return None

    try:
        first_note, start, seen_ids = True, 0, set()
        while True:
            args = {"query": ""}
            if start:
                args["startIndex"] = start  # server paginates ~10 at a time
            try:
                res = session.tool("search_notes", args)
            except Exception as exc:
                logger.warning("Evernote MCP: search_notes failed at start=%d: %s", start, exc)
                if start == 0:
                    raise  # first page failed → a real error, not an empty result
                break
            if start == 0:
                logger.warning("Evernote MCP: search_notes raw → %s", json.dumps(res)[:1000])
            data = _tool_json(res)
            hits = _as_list(data, "hits", "notes", "results", "items", "matches")
            logger.warning("Evernote MCP: %d hit(s) at startIndex=%d", len(hits), start)
            new = 0
            for nm in hits:
                if not isinstance(nm, dict):
                    continue
                note_id = nm.get("noteId") or nm.get("guid") or nm.get("id")
                if not note_id or note_id in seen_ids:
                    continue
                seen_ids.add(note_id)
                new += 1
                try:
                    full = _tool_json(session.tool("get_note", {"noteId": note_id}))
                except Exception as exc:
                    logger.warning("Evernote MCP: get_note failed for %s: %s", note_id, exc)
                    full = {}
                if first_note:
                    logger.warning("Evernote MCP: get_note raw → %s", json.dumps(full)[:1200])
                    first_note = False
                note = full if isinstance(full, dict) else {}
                title = note.get("title") or nm.get("title") or "Untitled note"
                notebook = _name(note.get("notebook") or note.get("notebookName")
                                 or nm.get("notebook"), "Notebook")
                raw_tags = note.get("tags") or nm.get("tags") or []
                tags = [t for t in (_name(x) for x in raw_tags) if t]
                enml = (note.get("content") or note.get("enml") or note.get("body")
                        or note.get("contentEnml") or "")
                text = (_strip_enml(enml) if isinstance(enml, str) else str(enml)) or nm.get("snippet") or ""
                modified = _iso(note.get("updatedAt") or nm.get("updatedAt"))

                if want("notes"):
                    body = json.dumps({"title": title, "notebook": notebook,
                                       "tags": tags, "content": text}).encode()
                    content, backed = _capped(body, content_cap)
                    yield SourceObject(
                        object_id=f"evernote:note:{note_id}",
                        doc_type="note",
                        title=title,
                        content=content,
                        preview=(str(text)[:200] or f"Note in {notebook}"),
                        meta={"notebook": notebook, "tags": tags, "kind": "note",
                              "content_backed_up": backed},
                        labels=[notebook, *(tags if isinstance(tags, list) else [])],
                        modified_at=modified,
                    )

                if want("attachments") and isinstance(enml, str):
                    import mimetypes
                    for h, mime in _en_media(enml):
                        ext = mimetypes.guess_extension(mime.split(";")[0]) or ""
                        fname = f"{title[:40]}-{h[:8]}{ext}"
                        cat, kind = classify_file(fname, mime)
                        raw = b""
                        try:
                            ares = session.tool("get_attachment", {"noteId": note_id, "hash": h})
                            raw = _attachment_bytes(ares)
                        except Exception as exc:
                            logger.warning("Evernote MCP: get_attachment failed (%s): %s", h[:8], exc)
                        content, backed = _capped(raw, content_cap) if raw else (
                            json.dumps({"_arkive": "no_content"}).encode(), False)
                        yield SourceObject(
                            object_id=f"evernote:res:{note_id}:{h}",
                            doc_type=kind, category=cat,
                            title=fname,
                            content=content,
                            preview=f"{mime} · {title}",
                            meta={"notebook": notebook, "note": title, "mime": mime,
                                  "hash": h, "kind": "attachment", "content_backed_up": backed},
                            labels=[notebook, "Attachments"],
                            modified_at=modified,
                        )
            # Stop when a page yields no new notes (empty, or startIndex ignored).
            if not hits or new == 0:
                break
            start += len(hits)
    finally:
        session.close()
