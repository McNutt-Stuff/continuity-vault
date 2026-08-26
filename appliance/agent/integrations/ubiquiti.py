"""UniFi (Dream Machine / UniFi OS) integration runner.

Runs on the appliance because it must reach the local gateway. Authenticates to
the controller, then pulls the applications/cloud services in use, the clients
using them, and per-client traffic (via UniFi DPI). Returns a normalized report
the appliance ships to the node / control plane.

Setup is one step: the user supplies the controller address + admin login. We
validate it, mint/keep a reusable credential, and from then on poll headlessly.
Nothing here can be unit-tested without a live controller, so every call is
defensive and logs what it did.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import httpx

from .dpi_signatures import DPI_APPS, DPI_CATS

# UniFi OS rejects requests it doesn't recognize as a browser with HTTP 499, so a
# real browser User-Agent (not python-httpx/…) is required to authenticate.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _base_url(host: str) -> str:
    host = (host or "").strip().rstrip("/")
    if not host:
        raise ValueError("controller host is required")
    if not host.startswith("http"):
        host = "https://" + host
    return host


def _client() -> httpx.Client:
    # UniFi OS ships a self-signed cert on the LAN; verification off is expected.
    # The browser-like headers are what get past UniFi's 499 bot rejection.
    return httpx.Client(
        verify=False, timeout=30, follow_redirects=True,
        headers={"User-Agent": _UA, "Accept": "application/json",
                 "Content-Type": "application/json", "Referer": "https://unifi/"})


def _login(c: httpx.Client, base: str, username: str, password: str, log) -> str:
    """Authenticate to the controller; returns the CSRF token (cookies set on the
    client). Tries UniFi OS (`/api/auth/login`) then the legacy path (`/api/login`)."""
    if not username or not password:
        raise RuntimeError("no controller login — enter the admin username and password")
    body = {"username": username, "password": password, "rememberMe": True}
    last: Optional[httpx.Response] = None
    for path in ("/api/auth/login", "/api/login"):
        log.info("ubiquiti: authenticating to %s%s", base, path)
        try:
            r = c.post(f"{base}{path}", json=body)
        except Exception as exc:  # noqa: BLE001
            log.warning("ubiquiti: login POST to %s failed: %s", path, exc)
            continue
        if r.status_code < 300:
            csrf = r.headers.get("x-csrf-token") or r.headers.get("x-updated-csrf-token") or ""
            log.info("ubiquiti: authenticated to %s (via %s)", base, path)
            return csrf
        log.warning("ubiquiti: login via %s returned HTTP %s", path, r.status_code)
        last = r
    code = last.status_code if last is not None else "no response"
    raise RuntimeError(f"controller login failed (HTTP {code}) — check the address, "
                       "username and password")


def _auth_headers(credentials: dict, csrf: str = "") -> dict:
    h = {}
    key = credentials.get("api_key")
    if key:
        h["X-API-KEY"] = key
    if csrf:
        h["x-csrf-token"] = csrf
    return h


def _try_mint_api_key(c: httpx.Client, base: str, site: str, csrf: str, log) -> Optional[str]:
    """Best-effort: mint a scoped API key so future polls don't need the password.
    UniFi OS versions differ; try the known endpoints and fall back to session
    auth (stored login) when none is available."""
    name = "Arkive Integration"
    for path, payload in (
        ("/proxy/network/api/s/%s/rest/apikey" % site, {"name": name}),
        ("/api/apikey", {"name": name}),
    ):
        try:
            r = c.post(f"{base}{path}", json=payload,
                       headers={"x-csrf-token": csrf} if csrf else {})
            if r.status_code < 300:
                data = r.json()
                key = (data.get("data") or [{}])
                key = (key[0] if isinstance(key, list) and key else data).get("api_key") \
                    or data.get("key") or data.get("apiKey")
                if key:
                    log.info("ubiquiti: minted API key via %s", path)
                    return key
        except Exception as exc:  # noqa: BLE001
            log.debug("ubiquiti: api-key mint via %s failed: %s", path, exc)
    return None


def _is_mfa(r: httpx.Response) -> bool:
    """Does this login response mean multi-factor / email verification is required?
    UniFi OS signals it via HTTP 499 or a ``required``/``needs2fa`` field."""
    if r.status_code == 499:
        return True
    try:
        data = r.json()
    except Exception:
        data = {}
    if isinstance(data, dict):
        req = str(data.get("required") or data.get("error") or "").lower()
        if any(k in req for k in ("2fa", "mfa", "otp", "email", "totp")):
            return True
        if data.get("needs2fa") or data.get("mfa_required"):
            return True
    return False


def _mfa_message(r: httpx.Response) -> str:
    try:
        data = r.json()
    except Exception:
        data = {}
    req = str((data or {}).get("required") or "").lower()
    if "email" in req:
        return "Enter the verification code we just emailed to your Ubiquiti account."
    return ("Enter the verification code from your authenticator app, or the one "
            "sent to your Ubiquiti account email.")


def begin_auth(config: dict, credentials: dict, log):
    """Start the controller login. Returns ``(session, result)`` where result
    ``state`` is ``authenticated`` (no MFA), ``mfa_required`` (need an OTP), or
    ``error``. The open session is reused to submit the OTP and mint a key."""
    base = _base_url(config.get("host") or credentials.get("host", ""))
    site = config.get("site") or credentials.get("site") or "default"
    username = credentials.get("username", "")
    password = credentials.get("password", "")
    if not username or not password:
        return None, {"state": "error",
                      "message": "Enter the controller admin username and password."}
    c = _client()
    body = {"username": username, "password": password, "rememberMe": True}
    last: Optional[httpx.Response] = None
    for path in ("/api/auth/login", "/api/login"):
        log.info("ubiquiti: authenticating to %s%s", base, path)
        try:
            r = c.post(f"{base}{path}", json=body)
        except Exception as exc:  # noqa: BLE001
            log.warning("ubiquiti: login POST %s failed: %s", path, exc)
            continue
        last = r
        if r.status_code < 300:
            csrf = r.headers.get("x-csrf-token") or ""
            log.info("ubiquiti: authenticated (no MFA) via %s", path)
            return ({"client": c, "base": base, "site": site, "csrf": csrf},
                    {"state": "authenticated"})
        if _is_mfa(r):
            csrf = r.headers.get("x-csrf-token") or ""
            log.info("ubiquiti: MFA required (HTTP %s via %s)", r.status_code, path)
            return ({"client": c, "base": base, "site": site, "csrf": csrf},
                    {"state": "mfa_required", "message": _mfa_message(r)})
        log.warning("ubiquiti: login via %s returned HTTP %s", path, r.status_code)
    c.close()
    code = last.status_code if last is not None else "no response"
    return None, {"state": "error",
                  "message": f"Controller login failed (HTTP {code}). "
                             "Check the address, username and password."}


def submit_otp(session: dict, credentials: dict, otp: str, log):
    """Submit the user's verification code on the existing session."""
    c = session["client"]
    base = session["base"]
    csrf = session.get("csrf", "")
    username = credentials.get("username", "")
    password = credentials.get("password", "")
    otp = (otp or "").strip()
    headers = {"x-csrf-token": csrf} if csrf else {}
    attempts = (
        ("/api/auth/login", {"username": username, "password": password,
                             "token": otp, "rememberMe": True}),
        ("/api/auth/login", {"username": username, "password": password,
                             "ubic_2fa_token": otp, "rememberMe": True}),
        ("/api/auth/mfa", {"token": otp}),
        ("/api/auth/2fa/verify", {"token": otp}),
    )
    last: Optional[httpx.Response] = None
    for path, payload in attempts:
        try:
            r = c.post(f"{base}{path}", json=payload, headers=headers)
        except Exception as exc:  # noqa: BLE001
            log.warning("ubiquiti: otp POST %s failed: %s", path, exc)
            continue
        last = r
        if r.status_code < 300:
            session["csrf"] = r.headers.get("x-csrf-token") or csrf
            log.info("ubiquiti: verification accepted via %s", path)
            return {"state": "authenticated"}
    code = last.status_code if last is not None else "?"
    log.warning("ubiquiti: verification failed (HTTP %s)", code)
    return {"state": "error", "message": "That code didn't work — check it and try again."}


def finalize(session: dict, config: dict, log) -> dict:
    """After authentication, mint a reusable API key (preferred) or persist the
    session cookies so future polls are headless. Returns the credential update."""
    c = session["client"]
    base = session["base"]
    site = session["site"]
    csrf = session.get("csrf", "")
    out = {"host": base, "site": site}
    api_key = _try_mint_api_key(c, base, site, csrf, log)
    if api_key:
        out["api_key"] = api_key
    else:
        cookies = {k: v for k, v in c.cookies.items()}
        if cookies:
            out["cookies"] = cookies
            if csrf:
                out["csrf"] = csrf
            log.info("ubiquiti: stored authenticated session (no API-key endpoint)")
    return out


def _load_dpi_names(c: httpx.Client, base: str, site: str, log) -> tuple[dict, dict]:
    """Resolve DPI app/category id→name from the controller when available,
    merged over the static fallback."""
    apps = dict(DPI_APPS)
    cats = dict(DPI_CATS)
    for path in (f"/proxy/network/api/s/{site}/stat/dpiapp",
                 f"/proxy/network/api/s/{site}/rest/dpiapp"):
        try:
            r = c.get(f"{base}{path}")
            if r.status_code < 300:
                for row in (r.json().get("data") or []):
                    aid, name = row.get("app"), row.get("name") or row.get("app_name")
                    if aid is not None and name:
                        apps[int(aid)] = name
                    cid, cname = row.get("cat"), row.get("cat_name")
                    if cid is not None and cname:
                        cats[int(cid)] = cname
        except Exception as exc:  # noqa: BLE001
            log.debug("ubiquiti: dpi name load via %s failed: %s", path, exc)
    return apps, cats


def _get(c: httpx.Client, base: str, path: str, headers: dict) -> list:
    r = c.get(f"{base}{path}", headers=headers)
    r.raise_for_status()
    body = r.json()
    return body.get("data", body) if isinstance(body, dict) else body


def _device_type(name: str, oui: str, is_wired: bool) -> str:
    hay = f"{name} {oui}".lower()
    if any(k in hay for k in ("iphone", "ipad", "android", "pixel", "galaxy", "phone")):
        return "phone"
    if any(k in hay for k in ("macbook", "imac", "laptop", "desktop", "pc", "windows", "thinkpad")):
        return "computer"
    if any(k in hay for k in ("tv", "roku", "firestick", "chromecast", "appletv")):
        return "media"
    if any(k in hay for k in ("nest", "ring", "echo", "camera", "bulb", "sensor", "printer")):
        return "iot"
    return "computer" if is_wired else "device"


def collect(config: dict, credentials: dict, log) -> dict:
    """Poll the controller and return a normalized report:
    {clients:[...], apps:[...], usage:[...], stats:{...}}."""
    base = _base_url(config.get("host") or credentials.get("host", ""))
    site = config.get("site") or credentials.get("site") or "default"
    with _client() as c:
        csrf = ""
        if credentials.get("api_key"):
            pass  # API key sent as a header (see _auth_headers)
        elif credentials.get("cookies"):
            # Reuse a session captured during interactive setup (MFA accounts).
            for k, v in (credentials.get("cookies") or {}).items():
                c.cookies.set(k, v)
            csrf = credentials.get("csrf", "")
        else:
            csrf = _login(c, base, credentials.get("username", ""),
                          credentials.get("password", ""), log)
        headers = _auth_headers(credentials, csrf)
        apps_names, cat_names = _load_dpi_names(c, base, site, log)

        # --- Clients (named, known + active) ---------------------------------
        by_mac: dict[str, dict] = {}

        def _fold(rows: list, active: bool) -> None:
            for u in rows:
                mac = (u.get("mac") or "").lower()
                if not mac:
                    continue
                name = (u.get("name") or u.get("hostname")
                        or u.get("display_name") or mac)
                cur = by_mac.setdefault(mac, {
                    "client_key": mac, "mac": mac, "name": name,
                    "hostname": u.get("hostname", ""), "ip": u.get("ip", ""),
                    "is_wired": bool(u.get("is_wired")), "is_guest": bool(u.get("is_guest")),
                    "tx_bytes": 0, "rx_bytes": 0, "total_bytes": 0,
                    "last_seen": _now_iso(),
                })
                if name and cur["name"] == mac:
                    cur["name"] = name
                cur["hostname"] = cur["hostname"] or u.get("hostname", "")
                cur["ip"] = u.get("ip", "") or cur["ip"]
                if active:
                    cur["tx_bytes"] = int(u.get("tx_bytes", 0) or 0)
                    cur["rx_bytes"] = int(u.get("rx_bytes", 0) or 0)
                    cur["total_bytes"] = cur["tx_bytes"] + cur["rx_bytes"]
                cur["device_type"] = _device_type(cur["name"], u.get("oui", ""),
                                                  cur["is_wired"])

        try:
            _fold(_get(c, base, f"/proxy/network/api/s/{site}/rest/user", headers), False)
        except Exception as exc:  # noqa: BLE001
            log.debug("ubiquiti: rest/user failed: %s", exc)
        try:
            _fold(_get(c, base, f"/proxy/network/api/s/{site}/stat/sta", headers), True)
        except Exception as exc:  # noqa: BLE001
            log.warning("ubiquiti: stat/sta failed: %s", exc)

        # --- DPI per client (apps + traffic) ---------------------------------
        apps_agg: dict[str, dict] = {}
        usage: list[dict] = []
        try:
            dpi = _get(c, base, f"/proxy/network/api/s/{site}/stat/stadpi", headers)
        except Exception as exc:  # noqa: BLE001
            log.warning("ubiquiti: stat/stadpi failed: %s", exc)
            dpi = []
        for entry in dpi:
            mac = (entry.get("mac") or "").lower()
            for a in entry.get("by_app", []):
                aid = a.get("app")
                cid = a.get("cat")
                tx = int(a.get("tx_bytes", 0) or 0)
                rx = int(a.get("rx_bytes", 0) or 0)
                total = tx + rx
                if total <= 0:
                    continue
                aname = apps_names.get(int(aid)) if aid is not None else None
                cname = cat_names.get(int(cid)) if cid is not None else None
                name = aname or cname or (f"App {aid}" if aid is not None else "Unknown")
                app_key = f"{cid}:{aid}"
                ag = apps_agg.setdefault(app_key, {
                    "app_key": app_key, "name": name, "category": cname or "",
                    "tx_bytes": 0, "rx_bytes": 0, "total_bytes": 0,
                    "clients": set(), "last_seen": _now_iso(),
                })
                ag["tx_bytes"] += tx
                ag["rx_bytes"] += rx
                ag["total_bytes"] += total
                if mac:
                    ag["clients"].add(mac)
                usage.append({"client_key": mac, "app_key": app_key,
                              "tx_bytes": tx, "rx_bytes": rx, "total_bytes": total,
                              "last_seen": _now_iso()})

        apps = []
        for ag in apps_agg.values():
            apps.append({
                "app_key": ag["app_key"], "name": ag["name"], "category": ag["category"],
                "tx_bytes": ag["tx_bytes"], "rx_bytes": ag["rx_bytes"],
                "total_bytes": ag["total_bytes"], "client_count": len(ag["clients"]),
                "last_seen": ag["last_seen"],
            })
        apps.sort(key=lambda a: -a["total_bytes"])
        clients = list(by_mac.values())
        total_bytes = sum(a["total_bytes"] for a in apps)
        log.info("ubiquiti: collected %d client(s), %d app(s), %s bytes",
                 len(clients), len(apps), total_bytes)
        return {
            "clients": clients,
            "apps": apps,
            "usage": usage,
            "stats": {"clients": len(clients), "apps": len(apps),
                      "bytes_seen": total_bytes},
        }
