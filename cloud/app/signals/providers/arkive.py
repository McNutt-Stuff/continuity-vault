"""Arkive-native SignalProvider.

Emits protection / recovery / source-health / device-health signals from data
Arkive ALREADY has (Collections, ConnectorAccounts, SnapshotReceipts, Appliances,
Nodes, DesktopAgents) — no external calls. Drives protection-gap analysis, Insights,
Compliance and (Phase 2) AI assurance.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from . import engine
from .provider_base import SignalProvider, register


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ArkiveSignalProvider(SignalProvider):
    provider = "arkive"
    collector_version = "1"
    _capabilities = (
        "arkive.protection", "arkive.source_health", "arkive.backup_currency",
        "arkive.recovery_readiness", "node.health", "appliance.health",
        "endpoint.agent_health",
    )

    def collect(self, db: Session, tenant) -> dict:
        from ..models import (Appliance, ApplianceStorage, Collection,
                              ConnectorAccount, DesktopAgent, Node, Vault)
        tid = tenant.id
        now = _now()
        n_sig = 0
        n_obj = 0

        vault_owner = {v.id: v.owner_user_id for v in
                       db.query(Vault).filter(Vault.tenant_id == tid).all()}

        # --- Sources: protection status + sync health + backup currency ----------
        accounts = {a.id: a for a in
                    db.query(ConnectorAccount).filter(ConnectorAccount.tenant_id == tid).all()}
        for c in db.query(Collection).filter(Collection.tenant_id == tid).all():
            n_obj += 1
            owner = vault_owner.get(c.vault_id) or ""
            acct = accounts.get(c.connector_account_id) if c.connector_account_id else None
            healthy = not (acct and (acct.last_error or acct.auth_status == "needs-reauth"))
            engine.emit(db, tid, "arkive.source.protection_status", provider=self.provider,
                        subject_type="source", subject_id=c.id,
                        normalized_value="protected" if healthy else "degraded",
                        severity="info" if healthy else "high",
                        value={"name": c.name, "source_type": c.source_type,
                               "auth_status": (acct.auth_status if acct else "n/a"),
                               "last_error": (acct.last_error if acct else None)},
                        actor_type="user", actor_id=owner, collector_version=self.collector_version)
            n_sig += 1
            if acct is not None:
                fails = int(acct.fail_count or 0)
                engine.emit(db, tid, "arkive.source.sync_health", provider=self.provider,
                            subject_type="source", subject_id=c.id,
                            normalized_value="failing" if fails >= 3 else ("degraded" if acct.last_error else "healthy"),
                            severity="high" if fails >= 3 else ("medium" if acct.last_error else "info"),
                            value={"fail_count": fails, "last_error": acct.last_error,
                                   "last_sync_at": acct.last_sync_at.isoformat() if acct.last_sync_at else None},
                            actor_type="user", actor_id=owner)
                n_sig += 1
            # Backup currency: overdue if it hasn't run within 2x its interval.
            interval = c.backup_interval_minutes if c.backup_interval_minutes is not None else 1440
            current = True
            if interval and c.last_backup_run_at:
                age_min = (now - c.last_backup_run_at.replace(tzinfo=None)).total_seconds() / 60
                current = age_min <= max(interval * 2, interval + 60)
            elif interval and c.last_backup_run_at is None:
                current = False
            engine.emit(db, tid, "arkive.backup.current", provider=self.provider,
                        subject_type="source", subject_id=c.id,
                        normalized_value="true" if current else "false",
                        severity="info" if current else "high",
                        value={"name": c.name,
                               "last_backup_run_at": c.last_backup_run_at.isoformat() if c.last_backup_run_at else None,
                               "interval_minutes": interval},
                        actor_type="user", actor_id=owner)
            n_sig += 1

        # --- Nodes (fleet health) — only meaningful on the control plane ---------
        for node in db.query(Node).filter(Node.is_self.is_(False),
                                           Node.role == "customer-tenant").all():
            hb = node.last_heartbeat_at
            online = bool(hb and (now - hb.replace(tzinfo=None)).total_seconds() < 180)
            tel = node.telemetry or {}
            stg = (tel.get("storage") or {}) if isinstance(tel, dict) else {}
            engine.emit(db, tid, "node.online", provider="node",
                        subject_type="node", subject_id=node.id,
                        normalized_value="true" if online else "false",
                        severity="info" if online else "high",
                        value={"name": node.name, "role": node.role})
            n_sig += 1
            if isinstance(stg, dict) and stg:
                engine.emit(db, tid, "node.storage.health", provider="node",
                            subject_type="node", subject_id=node.id,
                            normalized_value=str(stg.get("health") or "unknown"),
                            value={"pct": stg.get("pct"), "used": stg.get("used"), "total": stg.get("total")})
                n_sig += 1
            n_obj += 1

        # --- Appliances + their storage volumes ---------------------------------
        storages: dict[str, list] = {}
        for s in db.query(ApplianceStorage).filter(ApplianceStorage.tenant_id == tid).all():
            storages.setdefault(s.appliance_id, []).append(s)
        for a in db.query(Appliance).filter(Appliance.tenant_id == tid).all():
            n_obj += 1
            hb = a.last_heartbeat_at
            online = bool(hb and (now - hb.replace(tzinfo=None)).total_seconds() < 20 * 60)
            engine.emit(db, tid, "appliance.online", provider="appliance",
                        subject_type="appliance", subject_id=a.id,
                        normalized_value="true" if online else "false",
                        severity="info" if online else "high",
                        value={"name": a.name, "state": a.state, "tamper": a.tamper_state})
            n_sig += 1
            for s in storages.get(a.id, []):
                h = s.health or {}
                mi = h.get("mirror_integrity") if isinstance(h.get("mirror_integrity"), dict) else None
                if s.kind == "mirror" and mi is not None:
                    engine.emit(db, tid, "appliance.mirror.in_sync", provider="appliance",
                                subject_type="appliance", subject_id=a.id,
                                resource_type="storage", resource_id=s.id,
                                normalized_value="true" if mi.get("in_sync") else "false",
                                severity="info" if mi.get("in_sync") else "medium",
                                value={"volume": s.name, "connected": s.state != "disconnected"})
                    n_sig += 1
                engine.emit(db, tid, "appliance.storage.health", provider="appliance",
                            subject_type="appliance", subject_id=a.id,
                            resource_type="storage", resource_id=s.id,
                            normalized_value=str(s.state or "unknown"),
                            severity="high" if s.state in ("error", "disconnected") else "info",
                            value={"volume": s.name, "kind": s.kind,
                                   "used": int(s.used_bytes or 0), "capacity": int(s.capacity_bytes or 0)})
                n_sig += 1

        # --- Endpoint agents (Arkive health only; posture comes from EndpointProvider)
        for ag in db.query(DesktopAgent).filter(DesktopAgent.tenant_id == tid,
                                                DesktopAgent.state != "retired").all():
            n_obj += 1
            hb = ag.last_heartbeat_at
            running = bool(hb and (now - hb.replace(tzinfo=None)).total_seconds() < 2 * 3600)
            engine.emit(db, tid, "arkive.agent.running", provider=self.provider,
                        subject_type="endpoint", subject_id=ag.id,
                        normalized_value="true" if running else "false",
                        severity="info" if running else "medium",
                        value={"hostname": ag.hostname, "name": ag.name, "version": ag.version},
                        actor_type="user", actor_id=(ag.owner_user_id or "") if hasattr(ag, "owner_user_id") else "")
            n_sig += 1

        engine.record_provider_health(db, tid, self.provider, connection_status="ok",
                                      success=True, objects_processed=n_obj,
                                      signals_produced=n_sig,
                                      capabilities=self.capabilities(),
                                      collector_version=self.collector_version)
        return {"signals": n_sig, "objects": n_obj, "errors": 0, "last_error": ""}


register(ArkiveSignalProvider())
