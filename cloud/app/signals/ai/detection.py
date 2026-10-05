"""AI usage detector.

Reads Phase-1 signals already in the store — endpoint application inventory
(``endpoint.application.installed``) and network DPI apps (``NetworkApp``) — and
emits ``ai.tool.detected`` signals for any that match the AI catalog. Runs during
the tenant analysis pass (after providers have emitted their signals), gated by
the ``signal_ai_enabled`` flag. Records its own provider health under "ai".
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from .. import engine
from . import catalog

logger = logging.getLogger("cv.signals.ai")

# A network DPI category hint that an AI service is worth checking even when the
# name token is generic — kept small; the catalog name match is the real gate.
_SIGNAL_TYPE = "ai.tool.detected"


def detect(db: Session, tenant) -> dict:
    """Emit ai.tool.detected signals for this tenant. Never raises (caller wraps,
    but the whole analysis pass must survive a bad row)."""
    from ...models import Signal, NetworkApp
    tid = tenant.id
    n_sig = 0
    n_obj = 0
    seen: set[tuple[str, str]] = set()  # (catalog id, subject_id) dedup within pass

    def _emit(entry: dict, *, subject_type: str, subject_id: str, surface: str,
              signal_id: str = "", host: str = "", owner: str = "",
              resource_id: str = "") -> None:
        nonlocal n_sig
        key = (entry["id"], subject_id)
        if key in seen:
            return
        seen.add(key)
        sanctioned = bool(entry.get("sanctioned"))
        sev = "info" if sanctioned else catalog.RISK_SEVERITY.get(
            entry.get("data_risk", "medium"), "medium")
        engine.emit(
            db, tid, _SIGNAL_TYPE, provider="ai",
            subject_type=subject_type, subject_id=subject_id,
            resource_type="ai_tool", resource_id=resource_id or entry["id"],
            normalized_value=entry["id"], severity=sev,
            value={"name": entry["name"], "vendor": entry.get("vendor", ""),
                   "category": entry.get("category", ""), "sanctioned": sanctioned,
                   "data_risk": entry.get("data_risk", "medium"), "surface": surface,
                   "hostname": host},
            actor_type="user", actor_id=owner,
            fingerprint_extra=f"{surface}:{entry['id']}")
        n_sig += 1

    # 1) Endpoint application inventory → desktop AI apps.
    try:
        apps = (db.query(Signal)
                .filter(Signal.tenant_id == tid, Signal.status == "active",
                        Signal.signal_type == "endpoint.application.installed").all())
        for s in apps:
            n_obj += 1
            v = s.value or {}
            entry = catalog.match(v.get("name") or s.normalized_value, v.get("bundle_id"))
            if not entry:
                continue
            _emit(entry, subject_type="endpoint", subject_id=s.subject_id,
                  surface="endpoint", signal_id=s.id,
                  host=v.get("hostname") or "", owner=(s.actor_id or ""),
                  resource_id=f"{s.subject_id}:{entry['id']}")
    except Exception:  # noqa: BLE001
        logger.exception("ai endpoint-inventory scan failed for tenant %s", tid)

    # 2) Network DPI apps / DNS-derived AI services seen on the network. A row the
    #    ingest already tagged as AI (meta.ai, app_key "ai:<id>") is authoritative;
    #    otherwise fall back to a catalog name match on the DPI app name.
    try:
        for a in (db.query(NetworkApp)
                  .filter(NetworkApp.tenant_id == tid).all()):
            n_obj += 1
            meta = a.meta if isinstance(a.meta, dict) else {}
            tool_id = meta.get("ai_tool_id") if meta.get("ai") else None
            entry = next((e for e in catalog.CATALOG if e["id"] == tool_id), None) if tool_id else None
            if entry is None:
                entry = catalog.match(a.name)
            if not entry:
                continue
            _emit(entry, subject_type="org", subject_id=tid, surface="network",
                  resource_id=f"net:{entry['id']}")
    except Exception:  # noqa: BLE001
        logger.exception("ai network-app scan failed for tenant %s", tid)

    engine.record_provider_health(
        db, tid, "ai", connection_status="ok", success=True,
        objects_processed=n_obj, signals_produced=n_sig,
        capabilities=["ai.shadow_detection", "ai.app_inventory", "ai.network_services"],
        collector_version="1")
    return {"signals": n_sig, "objects": n_obj, "errors": 0, "last_error": ""}
