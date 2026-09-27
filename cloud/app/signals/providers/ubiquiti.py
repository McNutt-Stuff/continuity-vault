"""Ubiquiti / network SignalProvider.

Normalizes the network telemetry the existing Ubiquiti integration ALREADY stored
(NetworkClient / NetworkApp) into signals — no second UniFi controller call. The
generic network.client / network.application / network.destination signals are
reused by asset reconciliation now and become inputs to AI discovery in Phase 2
(the collector stays a NETWORK provider; AI classification is downstream).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .. import engine
from ..provider_base import SignalProvider, register


def _iso(dt):
    return dt.isoformat() if dt else None


class UbiquitiSignalProvider(SignalProvider):
    provider = "ubiquiti"
    collector_version = "1"
    _capabilities = (
        "asset.discovery", "network.devices", "network.clients",
        "network.applications", "network.destinations",
        "network.infrastructure", "network.firmware", "network.segmentation",
    )

    def collect(self, db: Session, tenant) -> dict:
        from ...models import IntegrationInstance, NetworkApp, NetworkClient, NetworkDevice
        tid = tenant.id
        # Only run when the tenant actually has a network integration (else there's
        # no telemetry to normalize and provider health should read "not configured").
        insts = (db.query(IntegrationInstance)
                 .filter(IntegrationInstance.tenant_id == tid,
                         IntegrationInstance.integration_type == "ubiquiti").all())
        if not insts:
            engine.record_provider_health(db, tid, self.provider,
                                          connection_status="not_configured",
                                          success=False, capabilities=self.capabilities(),
                                          collector_version=self.collector_version)
            return {"signals": 0, "objects": 0, "errors": 0, "last_error": ""}

        n_sig = 0
        n_obj = 0

        # --- Infrastructure devices (gateway / switch / AP) ---------------------
        for d in db.query(NetworkDevice).filter(NetworkDevice.tenant_id == tid).all():
            n_obj += 1
            stype = ("network.gateway.online" if d.device_type == "gateway"
                     else "network.device.online")
            engine.emit(db, tid, stype, provider=self.provider,
                        subject_type="network_device", subject_id=(d.device_key or d.id),
                        source_integration_id=d.integration_id or "",
                        normalized_value="true" if d.online else "false",
                        severity="info" if d.online else ("high" if d.device_type == "gateway" else "medium"),
                        value={"name": d.name, "model": d.model, "type": d.device_type,
                               "ip": d.ip, "firmware": d.firmware, "clients": int(d.client_count or 0)},
                        observed_at=d.last_seen)
            n_sig += 1
            if d.update_available:
                engine.emit(db, tid, "network.device.update_available", provider=self.provider,
                            subject_type="network_device", subject_id=(d.device_key or d.id),
                            normalized_value="true", severity="low",
                            value={"name": d.name, "model": d.model, "firmware": d.firmware})
                n_sig += 1

        # --- Segmentation / security posture (site config) ----------------------
        cfg = {}
        for inst in insts:
            netcfg = (inst.config or {}).get("network") or {}
            if isinstance(netcfg, dict):
                cfg = {**cfg, **netcfg}
        if cfg:
            if "ids_enabled" in cfg:
                engine.emit(db, tid, "network.ids.enabled", provider=self.provider,
                            subject_type="org", subject_id=tid,
                            normalized_value="true" if cfg.get("ids_enabled") else "false",
                            severity="info" if cfg.get("ids_enabled") else "medium",
                            value={"ips_enabled": cfg.get("ips_enabled")})
                n_sig += 1
            if "guest_isolation" in cfg:
                engine.emit(db, tid, "network.guest_isolation.enabled", provider=self.provider,
                            subject_type="org", subject_id=tid,
                            normalized_value="true" if cfg.get("guest_isolation") else "false",
                            severity="info" if cfg.get("guest_isolation") else "low",
                            value={"guest_networks": cfg.get("guest_networks")})
                n_sig += 1

        for c in db.query(NetworkClient).filter(NetworkClient.tenant_id == tid,
                                                NetworkClient.monitor_state != "ignored").all():
            n_obj += 1
            engine.emit(db, tid, "network.client.detected", provider=self.provider,
                        subject_type="network_client", subject_id=(c.client_key or c.id),
                        normalized_value=(c.device_type or "device"),
                        source_integration_id=c.integration_id or "",
                        actor_type="user", actor_id=(c.owner_user_id or ""),
                        value={"name": c.nickname or c.name or c.hostname,
                               "ip": c.ip, "mac": c.mac, "device_type": c.device_type,
                               "is_guest": bool(c.is_guest), "ownership": c.ownership,
                               "total_bytes": int(c.total_bytes or 0),
                               "last_seen": _iso(c.last_seen)},
                        observed_at=c.last_seen)
            n_sig += 1

        for app in db.query(NetworkApp).filter(NetworkApp.tenant_id == tid).all():
            n_obj += 1
            # STATE observation of "this app/service is present on the network" —
            # AI-ready metadata (name/category/mapped source_type) for Phase 2.
            engine.emit(db, tid, "network.application.detected", provider=self.provider,
                        subject_type="application", subject_id=(app.app_key or app.id),
                        normalized_value=(app.name or app.app_key or "app"),
                        source_integration_id=app.integration_id or "",
                        value={"name": app.name, "category": app.category,
                               "mapped_source_type": app.source_type,
                               "clients": int(app.client_count or 0),
                               "total_bytes": int(app.total_bytes or 0),
                               "last_seen": _iso(app.last_seen)},
                        observed_at=app.last_seen)
            n_sig += 1

        engine.record_provider_health(db, tid, self.provider, connection_status="ok",
                                      success=True, objects_processed=n_obj,
                                      signals_produced=n_sig,
                                      capabilities=self.capabilities(),
                                      collector_version=self.collector_version)
        return {"signals": n_sig, "objects": n_obj, "errors": 0, "last_error": ""}


register(UbiquitiSignalProvider())
