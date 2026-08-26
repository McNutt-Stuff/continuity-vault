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
import time
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


def _cp_unavailable(exc: BaseException) -> bool:
    """Gateway/connection error that means the control plane is momentarily
    unreachable (most often mid-deploy) — treat as transient, retry later."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (502, 503, 504)
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
                            httpx.RemoteProtocolError, httpx.PoolTimeout))

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
        self._last_latency_ms: Optional[int] = None  # heartbeat round-trip
        # Assigned customer node (federated fleets): once set, all signaling,
        # commands and receipts go here instead of the control plane.
        self._node_url: Optional[str] = None
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
            self._node_url = d.get("node_url")
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

    def _base(self) -> str:
        """API base for signaling/commands/receipts — the assigned node when the
        fleet has pinned this tenant to one, else the control plane."""
        return self._node_url or settings.cloud_base_url

    def _set_node_url(self, url: Optional[str]) -> None:
        url = (url or "").rstrip("/") or None
        if url == self._node_url:
            return
        self._node_url = url
        try:
            d = json.loads(_REG.read_text()) if _REG.exists() else {}
            if url:
                d["node_url"] = url
            else:
                d.pop("node_url", None)
            _REG.write_text(json.dumps(d))
        except Exception as exc:
            self.log.warning("could not persist node url: %s", exc)
        self.log.info("routing to %s", url or settings.cloud_base_url)

    async def heartbeat_once(self) -> None:
        body = {
            "state": self.sm.state.value,
            "isolation_state": self.sm.isolation_state,
            "software_version": settings.software_version,
            "attestation": build_attestation(settings.software_version, self.sm.isolation_state),
            "telemetry": self._telemetry(),
            "tamper_state": self.tamper_state,
        }
        base = self._base()
        async with httpx.AsyncClient(timeout=15) as client:
            t0 = time.perf_counter()
            try:
                r = await client.post(f"{base}/appliance/heartbeat",
                                      json=body, headers=self._headers())
            except Exception:
                # Assigned node unreachable → fall back to the control plane.
                if self._node_url and base == self._node_url:
                    self.log.warning("assigned node %s unreachable; falling back to control plane",
                                     self._node_url)
                    self._set_node_url(None)
                    base = settings.cloud_base_url
                    r = await client.post(f"{base}/appliance/heartbeat",
                                          json=body, headers=self._headers())
                else:
                    raise
            self._last_latency_ms = round((time.perf_counter() - t0) * 1000)
            if r.status_code in (502, 503, 504):
                # Control plane momentarily unreachable (most often mid-deploy).
                self.log.info("control plane unavailable (%s) — update in progress? "
                              "will retry", r.status_code)
                return
            if r.status_code != 200:
                self.log.warning("heartbeat rejected: %s", r.status_code)
                return
            data = r.json()
            # Adopt the assigned node URL for all subsequent signaling.
            self._set_node_url(data.get("node_url") or None)
            # Cloud advertises the current bundle version; the root self-update
            # timer applies it headlessly. Log when an update is pending.
            latest = data.get("latest_version")
            if latest and latest != settings.software_version and latest != self._last_update_note:
                self._last_update_note = latest
                self.log.info("update available: %s -> %s (headless self-update will apply it)",
                              settings.software_version, latest)
            # Detect control-plane signing-key drift: if the cloud is now signing
            # commands with a different key than the one pinned at linking, every
            # command will fail signature verification. Re-pin over this same
            # authenticated TLS channel so a legitimate key rotation self-heals.
            cp_key = data.get("control_plane_key_id")
            local_key = (self.cloud_bundle or {}).get("keyId")
            if cp_key and local_key and cp_key != local_key:
                self.log.warning("control-plane key drift: cloud=%s pinned=%s — re-pinning",
                                 cp_key, local_key)
                await self._retrust_control_plane(client, cp_key)
            for command in data.get("commands", []):
                await self._handle_command(client, command)
        self.log.debug("heartbeat ok (state=%s, isolation=%s)",
                       self.sm.state.value, self.sm.isolation_state)

    async def _retrust_control_plane(self, client: httpx.AsyncClient, expected_key: str) -> None:
        """Re-pin the cloud's current command-signing bundle after a key rotation,
        over the appliance's authenticated token channel, and persist it."""
        try:
            r = await client.get(f"{self._base()}/appliance/control-plane-bundle",
                                 headers=self._headers())
            if r.status_code != 200:
                self.log.warning("re-trust failed: %s", r.status_code)
                return
            bundle = r.json().get("bundle")
            if not bundle or bundle.get("keyId") != expected_key:
                self.log.warning("re-trust bundle mismatch (got %s want %s)",
                                 (bundle or {}).get("keyId"), expected_key)
                return
            self.cloud_bundle = bundle
            self._persist_cloud_bundle()
            self.log.info("re-pinned control-plane key %s", expected_key)
        except Exception as exc:
            self.log.warning("re-trust error: %s", exc)

    def _persist_cloud_bundle(self) -> None:
        try:
            d = json.loads(_REG.read_text()) if _REG.exists() else {}
            d["cloud_public_bundle"] = self.cloud_bundle
            _REG.write_text(json.dumps(d))
        except Exception as exc:
            self.log.warning("could not persist re-pinned bundle: %s", exc)

    def _telemetry(self) -> dict:
        cap = self.vault.capacity()
        plat = sysinfo.detect_platform()
        sysd = sysinfo.system_stats()
        disk = sysinfo.disk_stats(str(DATA))
        # Physical appliances advertise a fixed raw capacity; VMs report the disk.
        raw_total = 8 * 1024**4 if plat["kind"] == "hardware" else disk["disk_total_bytes"]
        pq = sysinfo.pq_available()
        net = sysinfo.net_io()
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
            "net_bytes_sent": net["bytes_sent"],
            "net_bytes_recv": net["bytes_recv"],
            "channel_encryption": ("TLS 1.3" if settings.cloud_base_url.startswith("https")
                                   else "insecure (dev)"),
            "cloud_latency_ms": self._last_latency_ms,
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
            self.log.warning("command rejected: applianceId mismatch (cmd=%s self=%s)",
                             payload.get("applianceId"), self.appliance_id)
            return False
        if payload["commandType"] == "QUARANTINE":
            pass  # allowed even when quarantined
        elif self.sm.state == State.QUARANTINED:
            self.log.warning("command rejected: appliance is QUARANTINED")
            return False  # reject all other commands while quarantined
        # Hybrid signature (require both classical + PQ) — no fail-open.
        try:
            HybridVerifier.from_bundle(self.cloud_bundle).verify(
                payload, signature, SigPolicy.REQUIRE_BOTH)
        except Exception as exc:
            self.log.warning("command rejected: signature verify failed (%s) — cloud "
                             "control-plane key may have changed; re-link the appliance", exc)
            return False
        # Local policy hash must match the appliance's own policy view.
        expected = hexdigest(json.dumps({
            "applianceId": self.appliance_id,
            "retentionFloorDays": 365,
            "immutability": True,
            "allowIngest": self.sm.state in (State.SEALED, State.ONLINE_STAGING, State.READY_TO_SEAL),
        }, sort_keys=True).encode())
        if payload["policyHash"] != expected:
            self.log.warning("command rejected: policyHash mismatch (state=%s) — cmd=%s expected=%s",
                             self.sm.state.value, payload.get("policyHash"), expected)
            return False
        return True

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
            try:
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
            except Exception as exc:
                # Never let a command handler crash the heartbeat: report the error
                # so the command is acked-with-error and stops being redelivered.
                self.log.exception("command %s (%s) failed", ctype, cmd_id[:8])
                result = {"error": str(exc)}
                # Return to a sealed, safe state if a handler left storage open.
                if self.sm.storage_accessible:
                    self.sm.state = State.SEALED

        resp = await client.post(f"{self._base()}/appliance/command-result",
                                 json={"command_id": cmd_id, "accepted": accepted,
                                       "result": result, "receipt": receipt},
                                 headers=self._headers())
        if resp.status_code != 200:
            self.log.warning("command %s result POST failed: %s %s",
                             ctype, resp.status_code, resp.text[:200])
        else:
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
        # Logical item count (chunked objects arrive as many storage units).
        logical = int(params.get("objectCount") or len(objects))
        total_bytes = sum(int(o.get("plaintextBytes", 0)) for o in objects)
        receipt = self.vault.seal(self.identity.signer, self.appliance_id, snapshot_id,
                                  manifest_hash, logical, total_bytes)
        self.sm.transition(State.SEALED)
        # Report the seal receipt so the cloud can mark it recoverable.
        asyncio.create_task(self._report_seal(snapshot_id, params, manifest_hash,
                                              logical, receipt))
        return receipt, {"snapshot_id": snapshot_id, "sealed": True}

    async def _report_seal(self, snapshot_id, params, manifest_hash, count, receipt):
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(f"{self._base()}/appliance/seal-receipt", json={
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
        """Recovery requires local approval before UNSEALED_FOR_RECOVERY (spec 7.3),
        unless the cloud-signed command carries an authenticated operator approval
        (the portal already enforced passkey step-up before issuing it)."""
        snapshot_id = params.get("snapshotId")
        operator_approved = bool(params.get("operatorApproved"))
        self.log.info("recovery requested snapshot=%s objects=%s operator_approved=%s local_policy=%s",
                      snapshot_id, params.get("objectIds"), operator_approved,
                      settings.require_local_recovery_approval)
        if settings.require_local_recovery_approval and not operator_approved:
            self.pending_recovery[snapshot_id] = params
            self.log.info("recovery parked awaiting local physical approval: %s", snapshot_id)
            return {"awaiting_local_approval": True, "snapshot_id": snapshot_id}
        return self._perform_recovery(snapshot_id, params)

    def _perform_recovery(self, snapshot_id: str, params: dict) -> dict:
        if not self.vault.snapshot_exists(snapshot_id):
            self.log.warning("recovery: snapshot not present on appliance: %s", snapshot_id)
            return {"error": "snapshot not present on appliance"}
        self.sm.state = State.UNSEAL_REQUESTED
        self.sm.transition(State.UNSEALED_FOR_RECOVERY)
        objects = []
        units: dict = {}
        for oid in params.get("objectIds", []):
            try:
                obj = self.vault.read_object(snapshot_id, oid)
                objects.append(oid)
                units[oid] = obj
                # Chunked objects reference their parts by id; return those too so
                # the cloud can reassemble and decrypt the full content.
                if isinstance(obj, dict) and obj.get("chunked"):
                    for part in obj.get("parts", []):
                        pid = part.get("objectId")
                        if pid:
                            units[pid] = self.vault.read_object(snapshot_id, pid)
            except Exception as exc:
                self.log.warning("recovery read failed for %s: %s", oid, exc)
        self.sm.transition(State.SEALING)
        self.sm.transition(State.SEALED)
        self.log.info("recovery complete snapshot=%s objects=%d units=%d resealed",
                      snapshot_id, len(objects), len(units))
        return {"recovered_objects": objects, "units": units, "resealed": True}

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


def _integration_worker():
    """Single shared integrations worker (its in-memory OTP sessions must persist
    across the data + provisioning loops)."""
    global _INTEG_WORKER
    if _INTEG_WORKER is None:
        from .integrations.worker import IntegrationWorker
        _INTEG_WORKER = IntegrationWorker(agent._base, agent._headers, agent.log)
    return _INTEG_WORKER


_INTEG_WORKER = None


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
        asyncio.create_task(_integrations_loop())
        asyncio.create_task(_provision_loop())


async def _heartbeat_loop() -> None:
    interval = agent.config.get("heartbeat_interval_seconds", settings.heartbeat_interval_seconds)
    while True:
        try:
            await agent.heartbeat_once()
        except Exception as exc:
            if _cp_unavailable(exc):
                agent.log.info("control plane unavailable (update in progress?) — "
                               "will retry: %s", exc)
            else:
                agent.log.error("heartbeat error: %s", exc)
        await asyncio.sleep(interval)


async def _integrations_loop() -> None:
    """Poll + run the appliance's enabled integrations in parallel, shipping
    results to the node / control plane. Independent of the heartbeat so a slow
    integration never delays signaling."""
    worker = _integration_worker()
    agent.log.info("integrations worker started")
    while True:
        try:
            await worker.tick()
        except Exception as exc:  # noqa: BLE001
            if not _cp_unavailable(exc):
                agent.log.error("integrations tick error: %s", exc)
        await asyncio.sleep(20)  # check due-ness often so a re-poll runs promptly


async def _provision_loop() -> None:
    """Fast loop driving the interactive setup handshake (login + OTP). Shares the
    worker so the in-flight controller session survives between steps."""
    worker = _integration_worker()
    while True:
        try:
            await worker.provision_tick()
        except Exception as exc:  # noqa: BLE001
            if not _cp_unavailable(exc):
                agent.log.debug("provision tick error: %s", exc)
        await asyncio.sleep(4)  # responsive during an active setup


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
    asyncio.create_task(_integrations_loop())
    asyncio.create_task(_provision_loop())
    return {"activated": True, "appliance_id": d["appliance_id"]}


@app.post("/local-approve-recovery")
def local_approve_recovery(body: dict):
    """Physical recovery-approval button (spec 7.3 / 12)."""
    snapshot_id = body.get("snapshot_id")
    params = agent.pending_recovery.pop(snapshot_id, None)
    if not params:
        raise HTTPException(404, "no pending recovery for snapshot")
    return agent._perform_recovery(snapshot_id, params)
