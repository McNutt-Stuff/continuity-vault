"""
Effective node settings resolver.

Configuration profiles let admins reconfigure a running node's behavior without a
redeploy. Settings resolve in a strict precedence:

    1. Override   — a per-node admin override (highest; Node.config_overrides)
    2. Profile    — an enabled configuration profile bound to the node
    3. Local      — the node's built-in / environment default (each consumer's fallback)

On the control plane the profiles + overrides for the self node are read live from
the DB; a remote node applies the layers it received on its last heartbeat (written
to ``node-settings.json`` by the heartbeat client as {"profiles": …, "overrides": …}).

Consumers read a typed value with ``get_int``/``get_bool``/``get_list``/``get`` (the
default they pass IS the "local" layer). ``effective_detailed`` exposes per-key
provenance for the admin UI. Results are cached briefly; ``invalidate()`` clears it.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("cv.nodeconfig")

# Where the heartbeat client writes the layers delivered to a remote node.
SETTINGS_PATH = "/etc/arkive/node-settings.json"

_cache: dict = {}
_at: float = 0.0
_TTL = 15.0
_last_sig: str | None = None


def _from_file() -> dict:
    """Delivered layers for a remote node → {"profiles": {...}, "overrides": {...}}.
    Accepts the legacy flat format (a bare dict = profile settings)."""
    try:
        raw = json.loads(Path(SETTINGS_PATH).read_text())
    except Exception:  # noqa: BLE001 — absent/unreadable on the control plane
        return {"profiles": {}, "overrides": {}}
    if isinstance(raw, dict) and ("profiles" in raw or "overrides" in raw):
        return {"profiles": dict(raw.get("profiles") or {}),
                "overrides": dict(raw.get("overrides") or {})}
    return {"profiles": dict(raw) if isinstance(raw, dict) else {}, "overrides": {}}


def _self_layers(db) -> tuple[dict, dict]:
    """(profiles, overrides) for THIS node — DB on the control plane, else the
    delivered file. One self-node lookup drives both."""
    file = _from_file()
    profiles = dict(file.get("profiles") or {})
    overrides = dict(file.get("overrides") or {})
    try:
        from .models import ConfigProfile, Node
        node = db.query(Node).filter(Node.is_self.is_(True)).first()
        if node is not None:
            # One assigned profile (kind='node') is authoritative; fall back to the
            # legacy node_ids membership for profiles bound the old way.
            prof = None
            if node.config_profile_id:
                prof = db.get(ConfigProfile, node.config_profile_id)
            if prof is not None and prof.enabled:
                profiles.update(prof.data or {})
            elif prof is None:
                for p in (db.query(ConfigProfile)
                          .filter(ConfigProfile.enabled.is_(True))
                          .order_by(ConfigProfile.name).all()):
                    if node.id in (p.node_ids or []):
                        profiles.update(p.data or {})
            if node.config_overrides:
                overrides.update(node.config_overrides)
    except Exception:  # noqa: BLE001 — table missing / DB not ready
        pass
    return profiles, overrides


def effective(db) -> dict:
    """All settings in effect on this node (override wins over profile)."""
    global _cache, _at, _last_sig
    if _cache and time.time() - _at < _TTL:
        return _cache
    profiles, overrides = _self_layers(db)
    merged = {**profiles, **overrides}
    _cache, _at = merged, time.time()
    # Log once whenever the effective configuration actually changes, so the
    # running process (scheduler / notifications) shows when it adopts new settings.
    try:
        sig = json.dumps(merged, sort_keys=True, default=str)
        if sig != _last_sig:
            _last_sig = sig
            if merged:
                logger.info("effective node settings updated: %s",
                            {k: merged[k] for k in sorted(merged)})
            else:
                logger.info("effective node settings: none (using built-in defaults)")
    except Exception:  # noqa: BLE001
        pass
    return merged


def effective_detailed(db) -> dict:
    """{key: {"value", "source"}} where source is 'override' | 'profile' — for the
    admin config view (keys not listed fall back to the local/env default)."""
    profiles, overrides = _self_layers(db)
    out: dict = {}
    for k, v in profiles.items():
        out[k] = {"value": v, "source": "profile"}
    for k, v in overrides.items():
        out[k] = {"value": v, "source": "override"}
    return out


def invalidate() -> None:
    global _at
    _at = 0.0


def get(db, key: str, default=None):
    v = effective(db).get(key)
    return default if v is None else v


def get_int(db, key: str, default: int) -> int:
    try:
        return int(effective(db).get(key, default))
    except (TypeError, ValueError):
        return default


def get_float(db, key: str, default: float) -> float:
    try:
        return float(effective(db).get(key, default))
    except (TypeError, ValueError):
        return default


def get_bool(db, key: str, default: bool) -> bool:
    v = effective(db).get(key)
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def get_list(db, key: str, default=None) -> list:
    v = effective(db).get(key)
    if v is None:
        return list(default) if default is not None else []
    if isinstance(v, list):
        return v
    return [x.strip() for x in str(v).split(",") if x.strip()]


# --------------------------------------------------------------------------- #
# Timezone (CV_TIMEZONE) — governs the daily-summary send time, log timestamps  #
# and server-side time formatting for this node.                               #
# --------------------------------------------------------------------------- #

_applied_tz: str | None = None


def timezone_name(db) -> str:
    """The configured IANA timezone name for this node ("" if unset → UTC)."""
    return str(get(db, "CV_TIMEZONE", "") or "").strip()


def tzinfo(db):
    """A tzinfo for this node's configured timezone, falling back to UTC."""
    name = timezone_name(db)
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 — unknown zone → UTC
            logger.warning("invalid CV_TIMEZONE %r — using UTC", name)
    return timezone.utc


def local_now(db) -> datetime:
    """Current time as an aware datetime in this node's configured timezone."""
    return datetime.now(tzinfo(db))


def to_local(db, dt: datetime) -> datetime:
    """Convert a datetime (naive values are treated as UTC — matching the DB's
    tz-naive UTC columns) into this node's configured timezone."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tzinfo(db))


def apply_process_timezone(db) -> str:
    """Set the process TZ so Python's local-time functions and log timestamps
    reflect the node's configured timezone. Idempotent; only calls tzset when the
    zone changes. DB writes stay UTC (they use explicit tz-aware UTC helpers)."""
    global _applied_tz
    name = timezone_name(db)
    if name == (_applied_tz or ""):
        return name
    try:
        if name:
            os.environ["TZ"] = name
        else:
            os.environ.pop("TZ", None)
        if hasattr(time, "tzset"):
            time.tzset()
        _applied_tz = name
        logger.info("process timezone set to %s", name or "system default (UTC)")
    except Exception:  # noqa: BLE001
        logger.warning("could not apply process timezone %r", name)
    return name
