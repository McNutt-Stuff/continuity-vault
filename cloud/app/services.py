"""
Resolve the storage/email service objects selected on the running node.

A ``ServiceObject`` names a concrete backend (Amazon S3 / Azure Blob storage, or
SES email) and links to a ``ConfigObject`` for its credentials. Each Node selects
one storage service and one email service; the running ("self") node's selections
determine where cloud objects are stored and how mail is sent. Results are cached
briefly so hot paths don't hit the database on every call. Callers fall back to
environment settings / the global EmailConfig when nothing is selected here.
"""

from __future__ import annotations

import time
from typing import Optional

_cache: dict = {}
_at: float = 0.0
_TTL = 20.0


def tenant_node_url(db, tenant_id: str) -> Optional[str]:
    """API base URL of the node a tenant is assigned to (federated mode), so its
    agents/appliances signal, take commands, back up and ingest there instead of
    the control plane. None when unassigned or federation is off."""
    from .config import get_settings
    from .models import Node, Tenant
    if not get_settings().node_sync_scope:
        return None
    t = db.get(Tenant, tenant_id)
    if not t or not t.node_id:
        return None
    n = db.get(Node, t.node_id)
    if n and n.endpoint:
        return n.endpoint.rstrip("/")
    return None


def _self_services() -> dict:
    global _cache, _at
    if _cache and time.time() - _at < _TTL:
        return _cache
    out: dict = {"storage": None, "email": None}
    try:
        from .db import SessionLocal
        from .models import ConfigObject, Node, ServiceObject
        from . import credstore, node_config
        with SessionLocal() as db:
            node = db.query(Node).filter(Node.is_self.is_(True)).first()
            # The assigned service is chosen via configuration (profile/override key
            # service.storage / service.email) so it works across the whole fleet;
            # the Node column is the legacy fallback.
            eff = {}
            try:
                eff = node_config.effective(db)
            except Exception:
                eff = {}
            ids = {
                "storage": eff.get("service.storage") or (node.storage_service_id if node else None),
                "email": eff.get("service.email") or (node.email_service_id if node else None),
            }
            for slot, sid in ids.items():
                if not sid:
                    continue
                svc = db.get(ServiceObject, sid)
                if svc is None or not svc.enabled:
                    continue
                values: dict = {}
                if svc.config_object_id:
                    obj = db.get(ConfigObject, svc.config_object_id)
                    if obj and obj.encrypted_values:
                        try:
                            values = credstore.decrypt("platform", obj.encrypted_values)
                        except Exception:
                            values = {}
                out[slot] = {"kind": svc.kind, "name": svc.name,
                             "config": {**values, **(svc.settings or {})}}
    except Exception:
        pass  # DB not ready / migration pending — fall back to env
    _cache, _at = out, time.time()
    return out


def self_storage_service() -> Optional[dict]:
    """{"kind","name","config"} for the running node's storage service, or None."""
    return _self_services().get("storage")


def self_email_service() -> Optional[dict]:
    """{"kind","name","config"} for the running node's email service, or None."""
    return _self_services().get("email")


def invalidate() -> None:
    global _at
    _at = 0.0
