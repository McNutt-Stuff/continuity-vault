"""
Resolve platform integration credentials from admin-managed Config Objects.

Sources (OAuth connectors, SES) can be linked to a ``ConfigObject`` in the admin
panel; this module returns the decrypted values for a source, cached briefly so
hot paths (OAuth catalog, email send) don't hit the DB each call. Callers fall
back to environment settings when nothing is configured here.
"""

from __future__ import annotations

import time

_cache: dict = {}
_at: float = 0.0
_TTL = 20.0


def _load() -> dict:
    global _cache, _at
    if _cache and time.time() - _at < _TTL:
        return _cache
    data: dict = {}
    try:
        from .db import SessionLocal
        from .models import ConfigObject, SourceConfig
        from . import credstore
        with SessionLocal() as db:
            objs = {o.id: o for o in db.query(ConfigObject).all()}
            for sc in db.query(SourceConfig).all():
                values: dict = {}
                obj = objs.get(sc.config_object_id) if sc.config_object_id else None
                if obj and obj.encrypted_values:
                    try:
                        values = credstore.decrypt("platform", obj.encrypted_values)
                    except Exception:
                        values = {}
                data[sc.connector_type] = {
                    "enabled": bool(sc.enabled),
                    "config_object_id": sc.config_object_id,
                    "values": values,
                }
    except Exception:
        pass  # DB not ready / migration pending — fall back to env
    _cache, _at = data, time.time()
    return data


def source_values(connector_type: str) -> dict:
    return (_load().get(connector_type) or {}).get("values") or {}


def source_enabled(connector_type: str, default: bool = True) -> bool:
    row = _load().get(connector_type)
    return default if row is None else bool(row["enabled"])


def invalidate() -> None:
    global _at
    _at = 0.0
