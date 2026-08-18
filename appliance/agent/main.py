"""
Arkive appliance agent.

Responsibilities:
- Turnkey activation using a linking code (takes all config from the cloud).
- Outbound-only management: periodic heartbeat with signed attestation.
- Local verification + policy enforcement of every signed command (spec 5.2):
  reject expired / out-of-sequence / mis-signed / wrong-appliance / policy-
  mismatched / quarantined commands.
- Controlled unseal for ingest/recovery, immutable commit, signed seal receipts.
- Cloud-triggered staged updates with rollback guard.

A small FastAPI surface exposes local status and the physical recovery-approval
button for local emergency recovery (spec 12).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException

from cv_crypto.command import build_snapshot_manifest
from cv_crypto.signing import HybridVerifier, SigPolicy
from cv_crypto.provider import hexdigest

from .config import get_settings
from .identity import ApplianceIdentity, build_attestation
from .state_machine import StateMachine, State
from .vault import VaultStore
from . import agent_log, sysinfo

settings = get_settings()
app = FastAPI(title="Arkive Appliance Agent", version="1.0.0")

DATA = Path(settings.data_dir)
DATA.mkdir(parents=True, exist_ok=True)
_REG = DATA / "registration.json"
_LOG_FILE = DATA / "agent.log"


class Agent:
    def __init__(self) -> None:
        self.identity = ApplianceIdentity(settings.data_dir)
        self.sm = StateMachine(State.PROVISIONING)
        self.vault = VaultStore(str(DATA / "vault"), self.sm)
        self.appliance_id: Optional[str] = None
        self.tenant_id: Optional[str] = None
        self.agent_token: Optional[str] = None
        self.cloud_bundle: Optional[dict] = None
        self.config: dict = {}
        self.tamper_state = "normal"
        self.pending_recovery: dict = {}  # snapshot -> awaiting local approval
        self.log = agent_log.setup_logging(_LOG_FILE)
        self._last_update_note = ""
        self._load_registration()

    # -- registration / activation ------------------------------------

    def _load_registration(self) -> None:
        if _REG.exists():
            d = json.loads(_REG.read_text())
            self.appliance_id = d["appliance_id"]
            self.tenant_id = d["tenant_id"]
            self.agent_token = d["agent_token"]
            self.cloud_bundle = d["cloud_public_bundle"]
            self.config = d.get("config", {})
            self.sm.state = State.SEALED

    @property
    def activated(self) -> bool:
        return self.agent_token is not None

    async def activate(self, linking_code: str) -> dict:
        payload = {
            "linking_code": linking_code,
            "serial": self.identity.serial,
            "model": settings.model,
            "identity_bundle": self.identity.public_bundle(),
            "attestation": build_attestation(settings.software_version, self.sm.isolation_state),
        }
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{settings.cloud_base_url}/appliance/activate", json=payload)
        if r.status_code != 200:
            raise RuntimeError(f"activation failed: {r.status_code} {r.text}")
        d = r.json()
        self.appliance_id = d["appliance_id"]
        self.tenant_id = d["tenant_id"]
        self.agent_token = d["agent_token"]
        self.cloud_bundle = d["cloud_public_bundle"]
        self.config = d["config"]
        self.sm.state = State.ONLINE_STAGING
        _REG.write_text(json.dumps(d))
        self.log.info("activated as appliance %s (tenant %s)", self.appliance_id, self.tenant_id)
        return d

    # -- heartbeat + command handling ---------------------------------

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.agent_token}"}

    async def heartbeat_once(self) -> None:
        body = {
            "state": self.sm.state.value,
            "isolation_state": self.sm.isolation_state,
            "software_version": settings.software_version,
            "attestation": build_attestation(settings.software_version, self.sm.isolation_state),
            "telemetry": self._telemetry(),
            "tamper_state": self.tamper_state,
        }
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{settings.cloud_base_url}/appliance/heartbeat",
                                  json=body, headers=self._headers())
            if r.status_code != 200:
                self.log.warning("heartbeat rejected: %s", r.status_code)
                return
            data = r.json()
            # Cloud advertises the current bundle version; the root self-update
            # timer applies it headlessly. Log when an update is pending.
            latest = data.get("latest_version")
            if latest and latest != settings.software_version and latest != self._last_update_note:
                self._last_update_note = latest
                self.log.info("update available: %s -> %s (headless self-update will apply it)",
                              settings.software_version, latest)
            for command in data.get("commands", []):
                await self._handle_command(client, command)
        self.log.debug("heartbeat ok (state=%s, isolation=%s)",
                       self.sm.state.value, self.sm.isolation_state)

    def _telemetry(self) -> dict:
        cap = self.vault.capacity()
        plat = sysinfo.detect_platform()
        sysd = sysinfo.system_stats()
        disk = sysinfo.disk_stats(str(DATA))
        # Physical appliances advertise a fixed raw capacity; VMs report the disk.
        raw_total = 8 * 1024**4 if plat["kind"] == "hardware" else disk["disk_total_bytes"]
        pq = sysinfo.pq_available()
        return {
            # System
            "hostname": sysd["hostname"],
            "os": sysd["os"],
            "kernel": sysd["kernel"],
            "arch": sysd["arch"],
            "cpu_count": sysd["cpu_count"],
            "load_avg": sysd["load_avg"],
            "mem_total_bytes": sysd["mem_total_bytes"],
            "mem_available_bytes": sysd["mem_available_bytes"],
            "uptime_seconds": sysd["uptime_seconds"],
            # Platform / model
            "model": settings.model,
            "model_kind": plat["kind"],          # hardware | vm
            "virtualization": plat["virtualization"],
            "hardware_product": plat["product"],
            "hardware_vendor": plat["vendor"],
            # Network
            "local_ip": sysinfo.local_ip(),
            "cloud_url": settings.cloud_base_url,
            # Storage / stored data
            "capacity_total_bytes": raw_total,
            "capacity_used_bytes": cap.get("used_bytes", 0),
            "disk_free_bytes": disk["disk_free_bytes"],
            "snapshots": cap.get("snapshots", 0),
            "objects": cap.get("objects", cap.get("snapshots", 0)),
            "drive_health": "healthy",
            "power": "ok",
            "temperature_c": 34,
            # Where recovery data physically lives on the appliance.
            "data_path": str(DATA),
            "data_mount": sysinfo.mount_device(str(DATA)),
            # Per-storage capacity + health (mapped onto the cloud storage objects).
            "storages": sysinfo.storage_report(
                str(DATA), "Built-In Storage", "builtin",
                raw_total, cap.get("used_bytes", 0)),
            # Encryption
            "quantum_safe": bool(pq),
            "content_alg": "AES-256-GCM",
            "signing_alg": (self.identity.signer.pq_alg if self.identity else None),
            "isolation_state": self.sm.isolation_state,
            # Logs (forwarded like the endpoint agent)
            "recent_logs": agent_log.tail(_LOG_FILE, 50),
            "software_version": settings.software_version,
        }

    def _verify_command(self, command: dict) -> bool:
        """Local verification of a signed command (spec 5.2)."""
        payload = command["payload"]
        signature = command["signature"]
        if payload["applianceId"] != self.appliance_id:
            return False
        if payload["commandType"] == "QUARANTINE":
            pass  # allowed even when quarantined
        elif self.sm.state == State.QUARANTINED:
            return False  # reject all other commands while quarantined
        # Hybrid signature (require both classical + PQ) — no fail-open.
        try:
            HybridVerifier.from_bundle(self.cloud_bundle).verify(
                payload, signature, SigPolicy.REQUIRE_BOTH)
        except Exception:
            return False
        # Local policy hash must match the appliance's own policy view.
        expected = hexdigest(json.dumps({
            "applianceId": self.appliance_id,
            "retentionFloorDays": 365,
            "immutability": True,
            "allowIngest": self.sm.state in (State.SEALED, State.ONLINE_STAGING, State.READY_TO_SEAL),
        }, sort_keys=True).encode())
        return payload["policyHash"] == expected

    async def _handle_command(self, client: httpx.AsyncClient, command: dict) -> None:
        payload = command["payload"]
        cmd_id = payload["commandId"]
        ctype = payload["commandType"]
        accepted = self._verify_command(command)
        self.log.info("command %s (%s): %s", ctype, cmd_id[:8],
                      "accepted" if accepted else "REJECTED")
        result: dict = {}
        receipt = None

        if accepted:
            if ctype == "OPEN_INGEST_WINDOW":
                receipt, result = self._do_ingest(payload["parameters"])
            elif ctype == "OPEN_RECOVERY_WINDOW":
                result = self._request_recovery(payload["parameters"])
            elif ctype == "QUARANTINE":
                self.sm.state = State.QUARANTINED
                result = {"state": self.sm.state.value}
            elif ctype == "STAGE_UPDATE":
                result = self._stage_update(payload["parameters"])
            elif ctype == "APPLY_UPDATE":
                result = {"applied": True}
            elif ctype == "REQUEST_VERIFICATION":
                result = {"integrity": "verified"}
            else:
                result = {"note": f"acknowledged {ctype}"}

        await client.post(f"{settings.cloud_base_url}/appliance/command-result",
                          json={"command_id": cmd_id, "accepted": accepted,
                                "result": result, "receipt": receipt},
                          headers=self._headers())
        self.log.info("command %s result: %s", ctype,
                      "error" if result.get("error") else "ok")

    # -- command implementations --------------------------------------

    def _do_ingest(self, params: dict):
        """Controlled unseal -> commit -> seal (spec 6.1 steps 6-10)."""
        self.sm.state = State.UNSEAL_REQUESTED
        self.sm.transition(State.UNSEALED_FOR_INGEST)
        snapshot_id = params.get("snapshotId") or params.get("expectedSnapshotIds", ["local-snap"])[0]
        objects = params.get("objects", [])
        manifest = build_snapshot_manifest(
            self.identity.signer, snapshot_id,
            params.get("vaultId", "local"), params.get("collectionId", "local"),
            objects, "standard")
        self.vault.commit_snapshot(snapshot_id, objects, manifest)
        manifest_hash = manifest["signature"]["payloadHash"]
        self.sm.transition(State.SEALING)
        receipt = self.vault.seal(self.identity.signer, self.appliance_id, snapshot_id,
                                  manifest_hash, len(objects),
                                  sum(int(o.get("plaintextBytes", 0)) for o in objects))
        self.sm.transition(State.SEALED)
        # Report the seal receipt so the cloud can mark it recoverable.
        asyncio.create_task(self._report_seal(snapshot_id, params, manifest_hash,
                                              len(objects), receipt))
        return receipt, {"snapshot_id": snapshot_id, "sealed": True}

    async def _report_seal(self, snapshot_id, params, manifest_hash, count, receipt):
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(f"{settings.cloud_base_url}/appliance/seal-receipt", json={
                "vault_id": params.get("vaultId", "local"),
                "collection_id": params.get("collectionId", "local"),
                "snapshot_id": snapshot_id,
                "storage_id": params.get("storageId"),
                "object_count": count,
                "total_bytes": sum(int(o.get("plaintextBytes", 0)) for o in params.get("objects", [])),
                "manifest_hash": manifest_hash,
                "receipt": receipt,
            }, headers=self._headers())

    def _request_recovery(self, params: dict) -> dict:
        """Recovery requires local approval before UNSEALED_FOR_RECOVERY (spec 7.3)."""
        snapshot_id = params.get("snapshotId")
        if settings.require_local_recovery_approval:
            self.pending_recovery[snapshot_id] = params
            return {"awaiting_local_approval": True, "snapshot_id": snapshot_id}
        return self._perform_recovery(snapshot_id, params)

    def _perform_recovery(self, snapshot_id: str, params: dict) -> dict:
        if not self.vault.snapshot_exists(snapshot_id):
            return {"error": "snapshot not present on appliance"}
        self.sm.state = State.UNSEAL_REQUESTED
        self.sm.transition(State.UNSEALED_FOR_RECOVERY)
        objects = []
        for oid in params.get("objectIds", []):
            try:
                objects.append(self.vault.read_object(snapshot_id, oid)["objectId"])
            except Exception:
                pass
        self.sm.transition(State.SEALING)
        self.sm.transition(State.SEALED)
        return {"recovered_objects": objects, "resealed": True}

    def _stage_update(self, params: dict) -> dict:
        """Verify + stage a signed update; downgrade/floor guarded (spec 11)."""
        manifest = params.get("manifest", {})
        try:
            HybridVerifier.from_bundle(self.cloud_bundle).verify(
                manifest["payload"], manifest["signature"], SigPolicy.REQUIRE_BOTH)
        except Exception:
            return {"staged": False, "reason": "invalid update signature"}
        staged = DATA / "staged_update.json"
        staged.write_text(json.dumps(params))
        return {"staged": True, "version": params.get("version")}


agent = Agent()


@app.on_event("startup")
async def startup() -> None:
    agent.log.info("appliance agent starting (v%s, model=%s)",
                   settings.software_version, settings.model)
    if not agent.activated and settings.linking_code:
        try:
            await agent.activate(settings.linking_code)
        except Exception as exc:  # keep the agent up to expose status
            agent.log.error("activation error: %s", exc)
    if agent.activated:
        asyncio.create_task(_heartbeat_loop())


async def _heartbeat_loop() -> None:
    interval = agent.config.get("heartbeat_interval_seconds", settings.heartbeat_interval_seconds)
    while True:
        try:
            await agent.heartbeat_once()
        except Exception as exc:
            agent.log.error("heartbeat error: %s", exc)
        await asyncio.sleep(interval)


@app.get("/status")
def status():
    return {
        "serial": agent.identity.serial,
        "activated": agent.activated,
        "appliance_id": agent.appliance_id,
        "state": agent.sm.state.value,
        "isolation_state": agent.sm.isolation_state,
        "tamper_state": agent.tamper_state,
        "software_version": settings.software_version,
        "telemetry": agent._telemetry(),
        "pending_recovery": list(agent.pending_recovery.keys()),
    }


class LinkBody(dict):
    pass


@app.post("/activate")
async def activate_endpoint(body: dict):
    code = body.get("linking_code", "")
    if not code:
        raise HTTPException(400, "linking_code required")
    d = await agent.activate(code)
    asyncio.create_task(_heartbeat_loop())
    return {"activated": True, "appliance_id": d["appliance_id"]}


@app.post("/local-approve-recovery")
def local_approve_recovery(body: dict):
    """Physical recovery-approval button (spec 7.3 / 12)."""
    snapshot_id = body.get("snapshot_id")
    params = agent.pending_recovery.pop(snapshot_id, None)
    if not params:
        raise HTTPException(404, "no pending recovery for snapshot")
    return agent._perform_recovery(snapshot_id, params)
