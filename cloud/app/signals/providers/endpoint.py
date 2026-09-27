"""Endpoint SignalProvider.

Normalizes device-posture + application-inventory telemetry the endpoint agent
reports on heartbeat (DesktopAgent.telemetry) into signals — no separate agent, no
extra call. Only posture keys the agent actually sent are emitted; an absent key
lowers coverage rather than asserting a clean state. Respects the agent's privacy
model (metadata only: encryption/firewall booleans + app inventory, never content).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .. import engine
from ..provider_base import SignalProvider, register

# telemetry key -> (signal_type, normalized-as-bool). Present-only.
_POSTURE_BOOL = {
    "disk_encryption_enabled": "endpoint.disk_encryption.enabled",
    "firewall_enabled": "endpoint.firewall.enabled",
    "secure_boot_enabled": "endpoint.secure_boot.enabled",
    "screen_lock_enabled": "endpoint.screen_lock.enabled",
    "antimalware_running": "endpoint.antimalware.running",
    "auto_update_enabled": "endpoint.auto_update.enabled",
    "reboot_required": "endpoint.reboot_required",
}


class EndpointSignalProvider(SignalProvider):
    provider = "endpoint"
    collector_version = "1"
    _capabilities = (
        "endpoint.identity", "endpoint.posture", "endpoint.encryption",
        "endpoint.application_inventory",
    )

    def collect(self, db: Session, tenant) -> dict:
        from ...models import DesktopAgent
        tid = tenant.id
        n_sig = 0
        n_obj = 0
        agents = db.query(DesktopAgent).filter(
            DesktopAgent.tenant_id == tid, DesktopAgent.state != "retired").all()
        for ag in agents:
            n_obj += 1
            tel = ag.telemetry or {}
            if not isinstance(tel, dict):
                continue
            owner = (ag.owner_user_id or "") if hasattr(ag, "owner_user_id") else ""
            eid = ag.id
            for key, stype in _POSTURE_BOOL.items():
                if key not in tel:
                    continue
                val = bool(tel.get(key))
                # reboot_required is "bad when true"; the rest are "good when true".
                bad = val if key == "reboot_required" else (not val)
                engine.emit(db, tid, stype, provider=self.provider,
                            subject_type="endpoint", subject_id=eid,
                            normalized_value="true" if val else "false",
                            severity=None if not bad else "medium",
                            value={"hostname": ag.hostname, "os": tel.get("os")},
                            actor_type="user", actor_id=owner)
                n_sig += 1
            if "patch_age_days" in tel:
                age = int(tel.get("patch_age_days") or 0)
                engine.emit(db, tid, "endpoint.patch_age", provider=self.provider,
                            subject_type="endpoint", subject_id=eid,
                            normalized_value=str(age),
                            severity="high" if age > 60 else ("medium" if age > 30 else "info"),
                            value={"hostname": ag.hostname, "patch_age_days": age},
                            actor_type="user", actor_id=owner)
                n_sig += 1
            # Application inventory (governance + Phase-2 AI detection input).
            apps = tel.get("app_inventory")
            if isinstance(apps, list):
                for app in apps[:2000]:
                    if not isinstance(app, dict):
                        continue
                    name = app.get("name") or app.get("bundle_id")
                    if not name:
                        continue
                    engine.emit(db, tid, "endpoint.application.installed", provider=self.provider,
                                subject_type="endpoint", subject_id=eid,
                                resource_type="application", resource_id=str(name),
                                normalized_value=str(name),
                                value={"name": name, "version": app.get("version"),
                                       "bundle_id": app.get("bundle_id"),
                                       "hostname": ag.hostname},
                                actor_type="user", actor_id=owner,
                                fingerprint_extra=str(name))
                    n_sig += 1

        engine.record_provider_health(db, tid, self.provider, connection_status="ok",
                                      success=True, objects_processed=n_obj,
                                      signals_produced=n_sig,
                                      capabilities=self.capabilities(),
                                      collector_version=self.collector_version)
        return {"signals": n_sig, "objects": n_obj, "errors": 0, "last_error": ""}


register(EndpointSignalProvider())
