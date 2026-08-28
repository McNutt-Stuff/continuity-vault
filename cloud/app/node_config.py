"""
Effective node settings resolver.

Configuration profiles let admins reconfigure a running node's behavior without a
redeploy. This resolves the settings in effect on THIS node:

  * on the control plane (and any node whose DB holds the profiles) the profiles
    bound to the self node are merged live; and
  * a remote node applies the settings it received on its last heartbeat (written
    to ``node-settings.json`` by the heartbeat client).

Consumers read a typed value with ``get_int``/``get_bool``/``get_list``/``get``.
Results are cached briefly; ``invalidate()`` clears the cache after an edit.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

# Where the heartbeat client writes the settings delivered to a remote node.
SETTINGS_PATH = "/etc/arkive/node-settings.json"

_cache: dict = {}
_at: float = 0.0
_TTL = 15.0


def _from_file() -> dict:
    try:
        return json.loads(Path(SETTINGS_PATH).read_text()) or {}
    except Exception:  # noqa: BLE001 — absent/unreadable on the control plane
        return {}


def _from_profiles(db) -> dict:
    out: dict = {}
    try:
        from .models import ConfigProfile, Node
        node = db.query(Node).filter(Node.is_self.is_(True)).first()
        if node is None:
            return out
        for p in (db.query(ConfigProfile)
                  .filter(ConfigProfile.enabled.is_(True))
                  .order_by(ConfigProfile.name).all()):
            if node.id in (p.node_ids or []):
                out.update(p.data or {})
    except Exception:  # noqa: BLE001 — table missing / DB not ready
        pass
    return out


def effective(db) -> dict:
    """All settings in effect on this node (live profiles win over the delivered file)."""
    global _cache, _at
    if _cache and time.time() - _at < _TTL:
        return _cache
    merged = _from_file()
    merged.update(_from_profiles(db))
    _cache, _at = merged, time.time()
    return merged


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
