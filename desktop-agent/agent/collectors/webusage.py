"""Endpoint web-usage collector (macOS).

Reads local browser history (Chromium family + Safari) read-only and reports
AGGREGATED per-host visit counts — the web apps/services this device used. Hosts
only, never full URLs/paths/queries, so it's app/service-level telemetry (like a
network DPI "apps" list), not a browsing-history export. Opt-in: only runs when
the agent's ``collect_web_usage`` setting is on. Results are cached (~30 min) so a
heartbeat never re-scans every cycle. The cloud classifies hosts (AI vs general
service) with its catalog — the agent stays dumb.
"""

from __future__ import annotations

import glob
import logging
import os
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

log = logging.getLogger("arkive")

_TTL = 1800.0            # re-scan at most every 30 min
_CACHE: list | None = None
_CACHE_AT = 0.0

_HOME = os.path.expanduser("~")
_APPSUP = os.path.join(_HOME, "Library", "Application Support")

# Chromium-family profiles: each browser keeps per-profile "History" SQLite DBs.
_CHROMIUM_GLOBS = [
    os.path.join(_APPSUP, "Google", "Chrome", "*", "History"),
    os.path.join(_APPSUP, "Microsoft Edge", "*", "History"),
    os.path.join(_APPSUP, "BraveSoftware", "Brave-Browser", "*", "History"),
    os.path.join(_APPSUP, "Arc", "User Data", "*", "History"),
    os.path.join(_APPSUP, "Vivaldi", "*", "History"),
    os.path.join(_APPSUP, "com.operasoftware.Opera", "History"),
    os.path.join(_APPSUP, "Chromium", "*", "History"),
]
_SAFARI_DB = os.path.join(_HOME, "Library", "Safari", "History.db")

# Hosts that are browser/OS plumbing, not a "service the user used".
_SKIP_HOST_SUFFIXES = (
    "gstatic.com", "googleapis.com", "google-analytics.com", "doubleclick.net",
    "googlesyndication.com", "cloudflare.com", "akamaized.net", "akamai.net",
    "cloudfront.net", "fbcdn.net", "licdn.com", "gvt1.com", "gvt2.com",
    " gstatic.com", "aaplimg.com", "apple.com.akadns.net", "mzstatic.com",
)


def _skip(host: str) -> bool:
    if not host or "." not in host:
        return True
    return any(host == s or host.endswith("." + s) for s in _SKIP_HOST_SUFFIXES)


def _registered(host: str) -> str:
    """Coarse eTLD+1: last two labels (good enough for app/service grouping)."""
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    # Handle the common two-level public suffixes minimally (co.uk, com.au, …).
    if parts[-2] in ("co", "com", "net", "org", "gov", "ac") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _open_ro(path: str) -> sqlite3.Connection | None:
    """Open a browser DB without disturbing the running browser: try immutable
    read-only, else a temp copy (the live DB is often write-locked)."""
    try:
        return sqlite3.connect(f"file:{path}?immutable=1", uri=True, timeout=2)
    except Exception:  # noqa: BLE001
        pass
    try:
        tmp = tempfile.mktemp(suffix=".sqlite")
        shutil.copy2(path, tmp)
        return sqlite3.connect(tmp, timeout=2)
    except Exception as exc:  # noqa: BLE001
        log.debug("webusage: cannot open %s: %s", path, exc)
        return None


def _scan_chromium(path: str, since_epoch: float, agg: dict) -> int:
    con = _open_ro(path)
    if con is None:
        return 0
    seen = 0
    try:
        # Chromium last_visit_time: microseconds since 1601-01-01.
        min_chrome = int((since_epoch + 11644473600) * 1_000_000)
        cur = con.execute(
            "SELECT url, visit_count, last_visit_time FROM urls "
            "WHERE last_visit_time > ? ORDER BY last_visit_time DESC LIMIT 5000",
            (min_chrome,))
        for url, visits, last in cur.fetchall():
            host = (urlparse(url or "").hostname or "").lower()
            if _skip(host):
                continue
            key = _registered(host)
            g = agg.setdefault(key, {"visits": 0, "last": 0.0})
            g["visits"] += int(visits or 1)
            g["last"] = max(g["last"], (int(last or 0) / 1_000_000) - 11644473600)
            seen += 1
    except Exception as exc:  # noqa: BLE001
        log.debug("webusage: chromium scan %s failed: %s", path, exc)
    finally:
        con.close()
    log.debug("webusage: chromium %s → %d host-row(s)", path, seen)
    return seen


def _scan_safari(since_epoch: float, agg: dict) -> int:
    con = _open_ro(_SAFARI_DB)
    if con is None:
        return 0
    seen = 0
    try:
        # Safari visit_time: seconds since 2001-01-01 (CFAbsoluteTime).
        min_safari = since_epoch - 978307200
        cur = con.execute(
            "SELECT i.url, i.visit_count, MAX(v.visit_time) "
            "FROM history_items i JOIN history_visits v ON v.history_item = i.id "
            "GROUP BY i.id HAVING MAX(v.visit_time) > ? LIMIT 5000", (min_safari,))
        for url, visits, last in cur.fetchall():
            host = (urlparse(url or "").hostname or "").lower()
            if _skip(host):
                continue
            key = _registered(host)
            g = agg.setdefault(key, {"visits": 0, "last": 0.0})
            g["visits"] += int(visits or 1)
            g["last"] = max(g["last"], float(last or 0) + 978307200)
            seen += 1
    except Exception as exc:  # noqa: BLE001
        log.debug("webusage: safari scan failed (Full Disk Access?): %s", exc)
    finally:
        con.close()
    log.debug("webusage: safari → %d host-row(s)", seen)
    return seen


def collect(window_days: int = 7, limit: int = 800) -> list[dict]:
    """Return ``[{host, visits, last_seen}]`` for web services used in the window,
    most-used first. Cached ~30 min. Empty on non-macOS or when nothing readable."""
    global _CACHE, _CACHE_AT
    if _CACHE is not None and (time.time() - _CACHE_AT) < _TTL:
        log.debug("webusage: cache hit (%d service(s), age %.0fs)",
                  len(_CACHE), time.time() - _CACHE_AT)
        return _CACHE
    since = time.time() - window_days * 86400
    agg: dict[str, dict] = {}
    browsers = 0
    rows = 0
    for pattern in _CHROMIUM_GLOBS:
        for path in glob.glob(pattern):
            browsers += 1
            rows += _scan_chromium(path, since, agg)
    if os.path.exists(_SAFARI_DB):
        browsers += 1
        rows += _scan_safari(since, agg)
    ordered = sorted(agg.items(), key=lambda kv: -kv[1]["visits"])[:limit]
    out = [{"host": host,
            "visits": int(g["visits"]),
            "last_seen": datetime.fromtimestamp(
                g["last"], timezone.utc).replace(tzinfo=None).isoformat()}
           for host, g in ordered if g["last"] > 0]
    _CACHE = out
    _CACHE_AT = time.time()
    log.info("webusage: scanned %d browser profile(s), %d history row(s) → "
             "%d distinct service(s) (window %dd)", browsers, rows, len(out), window_days)
    if out:
        top = ", ".join(f"{r['host']}({r['visits']})" for r in out[:8])
        log.debug("webusage: top services — %s", top)
    return out
