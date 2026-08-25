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
    return httpx.Client(verify=False, timeout=30, follow_redirects=True)


def _login(c: httpx.Client, base: str, username: str, password: str, log) -> str:
    """Authenticate to UniFi OS; returns the CSRF token (cookies set on client)."""
    r = c.post(f"{base}/api/auth/login",
               json={"username": username, "password": password, "rememberMe": True})
    r.raise_for_status()
    csrf = r.headers.get("x-csrf-token") or r.headers.get("x-updated-csrf-token") or ""
    log.info("ubiquiti: authenticated to %s", base)
    return csrf


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


def provision(config: dict, credentials: dict, log) -> dict:
    """Validate the login and return the credential dict to persist. Prefers a
    minted API key; otherwise keeps the login for headless re-auth."""
    base = _base_url(config.get("host") or credentials.get("host", ""))
    username = credentials.get("username", "")
    password = credentials.get("password", "")
    site = config.get("site") or "default"
    with _client() as c:
        csrf = _login(c, base, username, password, log)
        api_key = _try_mint_api_key(c, base, site, csrf, log)
    out = {"host": base, "site": site}
    if api_key:
        out["api_key"] = api_key
    else:
        # No key endpoint — keep the login so polling can re-auth headlessly.
        out["username"] = username
        out["password"] = password
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
        if not credentials.get("api_key"):
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
