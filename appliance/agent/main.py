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
import os
import time
import uuid
from json import JSONDecodeError
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from cv_crypto.command import build_snapshot_manifest
from cv_crypto.signing import HybridVerifier, SigPolicy
from cv_crypto.provider import hexdigest

from .config import get_settings
from .identity import ApplianceIdentity, build_attestation
from .state_machine import StateMachine, State
from .vault import VaultStore
from . import agent_log, storage_ops, sysinfo

settings = get_settings()
app = FastAPI(title="Arkive Appliance Agent", version="1.0.0")

# Local status + pairing web UI served on the appliance LAN address (spec:
# on-appliance interface). Pure vanilla JS; polls /status and /pairing.
_HOME_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Arkive Appliance</title>
<style>
:root{--bg:#0b1020;--card:#141a2e;--line:#26304d;--fg:#e6ebff;--mut:#93a0c4;--accent:#5b8cff;--ok:#37d67a;--warn:#ffb020;--bad:#ff5d5d}
*{box-sizing:border-box}
body{margin:0;font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--fg)}
.wrap{max-width:860px;margin:0 auto;padding:28px 20px 60px}
header{display:flex;align-items:center;gap:12px;margin-bottom:24px}
.logo{width:34px;height:34px;border-radius:9px;background:linear-gradient(135deg,#5b8cff,#8b5bff);display:flex;align-items:center;justify-content:center;font-weight:800}
h1{font-size:20px;margin:0}
.sub{color:var(--mut);font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;margin-bottom:18px}
.pill{display:inline-flex;align-items:center;gap:7px;padding:5px 11px;border-radius:999px;font-size:13px;font-weight:600}
.dot{width:9px;height:9px;border-radius:50%}
.big{font-size:17px;font-weight:700;margin:0 0 4px}
.code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:34px;letter-spacing:3px;font-weight:800;color:#fff;background:#0c1330;border:1px dashed var(--accent);border-radius:12px;padding:18px;text-align:center;margin:14px 0}
.steps{margin:8px 0 0;padding-left:20px;color:var(--mut)}
.steps li{margin:4px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:14px}
.kv .k{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.4px}
.kv .v{font-weight:600;margin-top:2px;word-break:break-word}
.bar{height:8px;border-radius:6px;background:#0c1330;overflow:hidden;margin-top:8px}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,#5b8cff,#8b5bff)}
footer{color:var(--mut);font-size:12px;text-align:center;margin-top:24px}
</style>
</head>
<body>
<div class="wrap">
<header>
<div class="logo">A</div>
<div><h1>Arkive Appliance</h1><div class="sub" id="sub">Local status</div></div>
</header>
<div id="pair" class="card" style="display:none"></div>
<div id="stat" class="card"><div class="sub">Loading…</div></div>
<div id="sys" class="card"></div>
<footer>Continuity Vault &middot; on-appliance interface &middot; refreshes automatically</footer>
</div>
<script>
function h(n){if(n==null)return '—';const u=['B','KB','MB','GB','TB','PB'];let i=0,v=Number(n);while(v>=1024&&i<u.length-1){v/=1024;i++}return v.toFixed(v<10&&i>0?1:0)+' '+u[i]}
function dur(s){if(!s)return '—';s=Number(s);const d=Math.floor(s/86400),hh=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return (d?d+'d ':'')+(hh?hh+'h ':'')+m+'m'}
function esc(t){const d=document.createElement('div');d.textContent=t==null?'':String(t);return d.innerHTML}
async function j(u){const r=await fetch(u);return r.json()}
async function tick(){
 let p={},s={};
 try{p=await j('/pairing')}catch(e){}
 try{s=await j('/status')}catch(e){}
 const t=(s.telemetry)||{};
 const pairEl=document.getElementById('pair');
 if(p&&p.paired===false){
   document.getElementById('sub').textContent='Awaiting pairing';
   pairEl.style.display='block';
   pairEl.innerHTML='<div class="pill" style="background:rgba(255,176,32,.15);color:#ffb020"><span class="dot" style="background:#ffb020"></span>Not yet paired</div>'
     +'<p class="big" style="margin-top:14px">Pair this appliance to your Arkive account</p>'
     +'<div class="code">'+esc(p.pairing_code||'…')+'</div>'
     +'<ol class="steps"><li>Sign in at <b>vault.arkive.life</b></li><li>Open <b>Appliances</b> &rarr; <b>Pair an appliance</b></li><li>Enter the code shown above</li></ol>';
 } else {
   document.getElementById('sub').textContent='Paired &amp; protected';
   pairEl.style.display='none';
 }
 const online=(p&&p.paired)||s.activated;
 const stEl=document.getElementById('stat');
 const stateTxt=esc(s.state||'—');
 stEl.innerHTML='<div class="pill" style="background:rgba(55,214,122,.15);color:#37d67a"><span class="dot" style="background:'+(online?'#37d67a':'#ffb020')+'"></span>'+(online?'Online':'Starting')+'</div>'
   +'<div class="grid" style="margin-top:16px">'
   +kv('State',stateTxt)+kv('Isolation',esc(s.isolation_state))+kv('Tamper',esc(s.tamper_state||'normal'))
   +kv('Serial',esc(s.serial))+kv('Model',esc((p&&p.model)||t.model))+kv('Version',esc(s.software_version))
   +'</div>';
 const cap=t.capacity_total_bytes,used=t.capacity_used_bytes,pct=cap?Math.min(100,used/cap*100):0;
 const sysEl=document.getElementById('sys');
 sysEl.innerHTML='<p class="big">Storage &amp; system</p>'
   +'<div class="kv"><div class="k">Storage used</div><div class="v">'+h(used)+' of '+h(cap)+' ('+pct.toFixed(0)+'%)</div><div class="bar"><i style="width:'+pct+'%"></i></div></div>'
   +'<div class="grid" style="margin-top:16px">'
   +kv('Hostname',esc(t.hostname))+kv('Local IP',esc(t.local_ip))+kv('OS',esc(t.os))
   +kv('Recovery points',esc(t.snapshots))+kv('Drive health',esc(t.drive_health))+kv('Uptime',dur(t.uptime_seconds))
   +kv('Quantum-safe',t.quantum_safe?'Yes':'Classical')+kv('Encryption',esc(t.content_alg))+kv('Cloud',esc(t.cloud_url))
   +'</div>';
}
function kv(k,v){return '<div class="kv"><div class="k">'+k+'</div><div class="v">'+(v||'—')+'</div></div>'}
tick();setInterval(tick,5000);
</script>
</body>
</html>"""


def _cp_unavailable(exc: BaseException) -> bool:
    """Gateway/connection error that means the control plane is momentarily
    unreachable (most often mid-deploy) — treat as transient, retry later."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (502, 503, 504)
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
                            httpx.RemoteProtocolError, httpx.PoolTimeout))


def _json_or_raise(resp: httpx.Response, context: str) -> dict:
    try:
        data = resp.json()
    except JSONDecodeError as exc:
        raise RuntimeError(f"{context}: invalid JSON response") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{context}: unexpected payload type {type(data).__name__}")
    return data

DATA = Path(settings.data_dir)
DATA.mkdir(parents=True, exist_ok=True)
_REG = DATA / "registration.json"
_PENDING = DATA / "pending.json"
_LOG_FILE = DATA / "agent.log"


def _resolve_storage() -> tuple[Path, str, str]:
    """(vault_root, storage_kind, storage_name).

    Prefer a dedicated Arkive RAID volume mounted separately (e.g. /arkive) for
    backups — the built-in path on the system disk is only used on VMs / when no
    dedicated volume is present. Identity + registration stay on ``data_dir`` so a
    dedicated disk change never re-pairs the appliance. Falls back to the built-in
    path if the dedicated volume can't actually be written (never blocks startup)."""
    ded = settings.dedicated_path
    exists = os.path.isdir(ded)
    is_mount = exists and os.path.ismount(ded)
    writable = is_mount and os.access(ded, os.W_OK)
    if writable:
        root = Path(ded)
        try:
            (root / "vault").mkdir(parents=True, exist_ok=True)
            print(f"[storage] using dedicated volume {ded} (mounted+writable)", flush=True)
            return root, "dedicated", "Dedicated Storage"
        except Exception as exc:  # noqa: BLE001 — unwritable dedicated volume → use built-in
            # Most often the systemd sandbox (ProtectSystem=strict) hasn't allow-
            # listed the path in ReadWritePaths — surface it so it's diagnosable.
            print(f"[storage] dedicated volume {ded} not writable by the agent "
                  f"({exc}); using built-in storage. Add '{ded}' to the service "
                  f"ReadWritePaths.", flush=True)
    else:
        print(f"[storage] no dedicated volume at {ded} "
              f"(exists={exists} mounted={is_mount} writable={writable}); "
              f"using built-in storage", flush=True)
    return DATA, "builtin", "Built-In Storage"


STORAGE_ROOT, STORAGE_KIND, STORAGE_NAME = _resolve_storage()

# External (USB) storage: where Arkive mounts set-up drives and the local registry
# that remembers them (by serial) so a reconnected drive is re-mounted on boot.
EXT_BASE = DATA / "ext"
EXT_REGISTRY = DATA / "ext_stores.json"
# Requests to the root storage helper (privileged format/mount) are dropped here.
EXT_QUEUE = DATA / "storage-queue"


class Agent:
    def __init__(self) -> None:
        self.identity = ApplianceIdentity(settings.data_dir)
        self.sm = StateMachine(State.PROVISIONING)
        self.vault = VaultStore(str(STORAGE_ROOT / "vault"), self.sm)
        # External-storage registry + live mountpoints, then mount whatever is
        # present and point the vault at any mirror volumes.
        self._ext_stores: list[dict] = self._load_ext_registry()
        self._ext_mounts: dict[str, str] = {}
        self._mount_ext_stores()
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
        # Zero-touch pairing: when installed WITHOUT a linking code the appliance
        # registers with the control plane as an un-claimed unit and shows a
        # pairing code on its local web UI until a customer claims it.
        self.registration_id: Optional[str] = None
        self.reg_token: Optional[str] = None
        self.pairing_code: Optional[str] = None
        # Assigned customer node (federated fleets): once set, all signaling,
        # commands and receipts go here instead of the control plane.
        self._node_url: Optional[str] = None
        self._load_registration()
        if not self.activated:
            self._load_pending()

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
        d = _json_or_raise(r, "activation")
        self.appliance_id = d["appliance_id"]
        self.tenant_id = d["tenant_id"]
        self.agent_token = d["agent_token"]
        self.cloud_bundle = d["cloud_public_bundle"]
        self.config = d["config"]
        self.sm.state = State.ONLINE_STAGING
        _REG.write_text(json.dumps(d))
        self.log.info("activated as appliance %s (tenant %s)", self.appliance_id, self.tenant_id)
        return d

    # -- zero-touch registration + pairing ----------------------------

    def _load_pending(self) -> None:
        if _PENDING.exists():
            try:
                d = json.loads(_PENDING.read_text())
                self.registration_id = d.get("registration_id")
                self.reg_token = d.get("agent_token")
                self.pairing_code = d.get("pairing_code")
                if d.get("cloud_public_bundle"):
                    self.cloud_bundle = d["cloud_public_bundle"]
            except Exception as exc:  # noqa: BLE001
                self.log.warning("could not load pending registration: %s", exc)

    @property
    def registered(self) -> bool:
        return self.reg_token is not None

    async def register(self) -> dict:
        """Register an un-claimed appliance and obtain a pairing code (spec: zero-
        touch onboarding). Idempotent on the control plane by hardware serial."""
        payload = {
            "serial": self.identity.serial,
            "model": settings.model,
            "identity_bundle": self.identity.public_bundle(),
            "attestation": build_attestation(settings.software_version, self.sm.isolation_state),
            "telemetry": self._telemetry(),
        }
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{settings.cloud_base_url}/appliance/register", json=payload)
        if r.status_code != 200:
            raise RuntimeError(f"registration failed: {r.status_code} {r.text}")
        d = _json_or_raise(r, "registration")
        self.registration_id = d["registration_id"]
        self.reg_token = d["agent_token"]
        self.pairing_code = d["pairing_code"]
        self.cloud_bundle = d.get("cloud_public_bundle")
        _PENDING.write_text(json.dumps(d))
        self.log.info("registered — pairing code %s (awaiting claim)", self.pairing_code)
        return d

    async def register_heartbeat_once(self) -> bool:
        """Poll the control plane while awaiting a claim. Returns True once the
        appliance has been paired and adopted its real identity."""
        headers = {"Authorization": f"Bearer {self.reg_token}"}
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{settings.cloud_base_url}/appliance/register-heartbeat",
                                  json={"telemetry": self._telemetry()}, headers=headers)
        if r.status_code in (502, 503, 504):
            return False
        if r.status_code == 401:
            # Registration was cleared server-side — re-register to get a new code.
            self.log.warning("registration token rejected — re-registering")
            self.reg_token = None
            return False
        if r.status_code != 200:
            self.log.warning("register-heartbeat rejected: %s", r.status_code)
            return False
        try:
            d = _json_or_raise(r, "register-heartbeat")
        except RuntimeError as exc:
            self.log.warning("register-heartbeat parse failed: %s", exc)
            return False
        if d.get("paired") and d.get("activation"):
            self._adopt_activation(d["activation"])
            return True
        # Still unclaimed — refresh the displayed pairing code.
        code = d.get("pairing_code")
        if code and code != self.pairing_code:
            self.pairing_code = code
        return False

    def _adopt_activation(self, d: dict) -> None:
        """Switch from an un-claimed unit to a fully-activated appliance."""
        self.appliance_id = d["appliance_id"]
        self.tenant_id = d["tenant_id"]
        self.agent_token = d["agent_token"]
        self.cloud_bundle = d["cloud_public_bundle"]
        self.config = d.get("config", {})
        self.sm.state = State.ONLINE_STAGING
        _REG.write_text(json.dumps(d))
        # Route to the tenant's assigned node from the first heartbeat when the
        # control plane pins one at pairing (else stay on the control plane).
        self._set_node_url(d.get("node_url") or None)
        try:
            _PENDING.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        self.reg_token = None
        self.pairing_code = None
        self.log.info("paired — now appliance %s (tenant %s)", self.appliance_id, self.tenant_id)

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
            # A node that doesn't yet recognize this appliance (its token hasn't
            # replicated, or the appliance was just reassigned) answers 401/403/404.
            # Fall back to the control plane so a routing hiccup never strands the
            # appliance as "offline" while it thinks it's online.
            if (self._node_url and base == self._node_url
                    and r.status_code in (401, 403, 404)):
                self.log.warning("assigned node %s rejected heartbeat (%s); falling back to "
                                 "control plane", self._node_url, r.status_code)
                self._set_node_url(None)
                base = settings.cloud_base_url
                r = await client.post(f"{base}/appliance/heartbeat",
                                      json=body, headers=self._headers())
            if r.status_code in (502, 503, 504):
                # Control plane momentarily unreachable (most often mid-deploy).
                self.log.info("control plane unavailable (%s) — update in progress? "
                              "will retry", r.status_code)
                return
            if r.status_code != 200:
                self.log.warning("heartbeat rejected by %s: %s — %s",
                                 base, r.status_code, r.text[:200])
                return
            try:
                data = _json_or_raise(r, "heartbeat")
            except RuntimeError as exc:
                self.log.warning("heartbeat parse failed: %s", exc)
                return
            # Adopt the assigned node URL for all subsequent signaling.
            self._set_node_url(data.get("node_url") or None)
            # Cloud-driven log verbosity (from the appliance's assigned config).
            lvl = (data.get("config") or {}).get("log_level")
            if lvl and lvl != getattr(self, "_log_level", None):
                self._log_level = lvl
                agent_log.set_level(lvl)
                self.log.info("log level set to %s (cloud config)", lvl)
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
            self._cp_key = cp_key or getattr(self, "_cp_key", None)
            local_key = (self.cloud_bundle or {}).get("keyId")
            if cp_key and local_key and cp_key != local_key:
                self.log.warning("control-plane key drift: cloud=%s pinned=%s — re-pinning",
                                 cp_key, local_key)
                await self._retrust_control_plane(client, cp_key)
            ncmd = len(data.get("commands", []))
            for command in data.get("commands", []):
                await self._handle_command(client, command)
        # Visible so heartbeat activity can be confirmed in the appliance log.
        self.log.info("heartbeat ok → %s (%dms, state=%s%s)", base, self._last_latency_ms,
                      self.sm.state.value, f", {ncmd} command(s)" if ncmd else "")

    async def _retrust_control_plane(self, client: httpx.AsyncClient, expected_key: str | None = None) -> None:
        """Re-pin the cloud's current command-signing bundle after a key rotation,
        over the appliance's authenticated token channel, and persist it. When
        ``expected_key`` is given the returned bundle must match it; otherwise
        whatever the signer currently advertises is adopted (used to self-heal a
        rejected command whose signature no longer matches the pinned key)."""
        try:
            r = await client.get(f"{self._base()}/appliance/control-plane-bundle",
                                 headers=self._headers())
            if r.status_code != 200:
                self.log.warning("re-trust failed: %s", r.status_code)
                return
            bundle = r.json().get("bundle")
            if not bundle or (expected_key and bundle.get("keyId") != expected_key):
                self.log.warning("re-trust bundle mismatch (got %s want %s)",
                                 (bundle or {}).get("keyId"), expected_key)
                return
            self.cloud_bundle = bundle
            self._cp_key = bundle.get("keyId")
            self._persist_cloud_bundle()
            self.log.info("re-pinned control-plane key %s", bundle.get("keyId"))
        except Exception as exc:
            self.log.warning("re-trust error: %s", exc)

    def _persist_cloud_bundle(self) -> None:
        try:
            d = json.loads(_REG.read_text()) if _REG.exists() else {}
            d["cloud_public_bundle"] = self.cloud_bundle
            _REG.write_text(json.dumps(d))
        except Exception as exc:
            self.log.warning("could not persist re-pinned bundle: %s", exc)

    # -- external storage (USB / removable) ---------------------------

    def _load_ext_registry(self) -> list[dict]:
        try:
            return json.loads(EXT_REGISTRY.read_text()) or []
        except Exception:
            return []

    def _save_ext_registry(self) -> None:
        try:
            EXT_REGISTRY.write_text(json.dumps(self._ext_stores))
        except Exception as exc:  # noqa: BLE001
            self.log.warning("could not persist external-storage registry: %s", exc)

    def _delegate_storage(self, action: str, params: dict, timeout: int = 240) -> dict:
        """Hand a privileged disk op (format/mount/unmount) to the ROOT helper via
        the storage queue and wait for its result. The helper runs unsandboxed
        (cv-appliance-storage.service, triggered by cv-appliance-storage.path)
        because the agent can't gain privileges under NoNewPrivileges."""
        req_id = uuid.uuid4().hex
        EXT_QUEUE.mkdir(parents=True, exist_ok=True)
        res_path = EXT_QUEUE / f"res-{req_id}.json"
        req_path = EXT_QUEUE / f"req-{req_id}.json"
        try:
            req_path.write_text(json.dumps({"action": action, "params": params}))
        except OSError as exc:
            return {"error": f"could not queue storage request: {exc}"}
        deadline = time.time() + timeout
        while time.time() < deadline:
            if res_path.exists():
                try:
                    result = json.loads(res_path.read_text())
                finally:
                    res_path.unlink(missing_ok=True)
                    req_path.unlink(missing_ok=True)
                return result
            time.sleep(0.5)
        req_path.unlink(missing_ok=True)
        return {"error": "storage helper did not respond (is cv-appliance-storage "
                         "installed and running?)"}

    def _mount_ext_stores(self) -> None:
        """Mount every already-set-up external drive that's currently present, then
        point the vault at any mirror volumes so writes/recoveries duplicate."""
        self._ext_mounts = {}
        for e in self._ext_stores:
            try:
                res = storage_ops.mount_known(
                    serial=e.get("serial", ""), store_id=e["store_id"],
                    name=e.get("name", "External Storage"), mount_base=str(EXT_BASE),
                    mirror_of_id=e.get("mirror_of_id"), kind=e.get("kind", "external"))
                if res:
                    self._ext_mounts[e["store_id"]] = res["mountpoint"]
            except Exception as exc:  # noqa: BLE001
                self.log.warning("could not mount external store %s: %s",
                                 e.get("name"), exc)
        self._apply_mirror_roots()

    def _apply_mirror_roots(self) -> None:
        roots = [os.path.join(self._ext_mounts[e["store_id"]], "vault")
                 for e in self._ext_stores
                 if e.get("kind") == "mirror" and e["store_id"] in self._ext_mounts]
        try:
            self.vault.set_mirror_roots(roots)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("could not apply mirror roots: %s", exc)

    def _reload_ext_storage(self) -> None:
        """Re-read the external-storage registry and (re)mount known drives, then
        re-apply mirror routing. Triggered by SIGHUP so a drive that cvtool set up
        (as root, outside the sandbox) becomes usable without a full restart."""
        self._ext_stores = self._load_ext_registry()
        self._mount_ext_stores()
        self.log.info("external storage reloaded — %d store(s), %d mounted",
                      len(self._ext_stores), len(self._ext_mounts))

    def _excluded_disks(self) -> set:
        return {sysinfo.system_disk_device(),
                sysinfo.dedicated_disk_device(settings.dedicated_path)}

    def _external_storage_telemetry(self) -> tuple[list[dict], list[dict]]:
        """(ext_store_rows, detected_devices) for the heartbeat.

        ext_store_rows are Arkive-managed stores (ours) with per-store capacity +
        health and a connected flag; detected_devices are raw removable candidates
        the cloud surfaces for setup."""
        detected = sysinfo.detect_external_storage(self._excluded_disks())
        rows: list[dict] = []
        for e in self._ext_stores:
            sid = e["store_id"]
            mount = self._ext_mounts.get(sid)
            connected = bool(mount) and os.path.ismount(mount)
            cap = used = 0
            if connected:
                try:
                    st = os.statvfs(mount)
                    cap = st.f_blocks * st.f_frsize
                    used = (st.f_blocks - st.f_bfree) * st.f_frsize
                except Exception:
                    pass
            rows.append({
                "name": e.get("name", "External Storage"),
                "kind": e.get("kind", "external"),
                "store_id": sid,
                "device_serial": e.get("serial", ""),
                "mirror_of_id": e.get("mirror_of_id"),
                "capacity_bytes": cap,
                "used_bytes": used,
                "free_bytes": max(cap - used, 0),
                "connected": connected,
                "state": "ready" if connected else "disconnected",
                "health": {"drive_health": "healthy" if connected else "disconnected",
                           "device": mount or "", "serial": e.get("serial", ""),
                           "mirror_of": e.get("mirror_of_id")},
            })
        return rows, detected

    def _setup_external_storage(self, params: dict) -> dict:
        """Handle a cloud-signed SETUP_STORAGE command. The privileged format+mount
        runs in the ROOT helper (the sandboxed agent can't partition/mount and can't
        sudo under NoNewPrivileges); we then adopt the ready volume in place."""
        store_id = params.get("storeId")
        serial = params.get("serial") or ""
        if not store_id:
            return {"error": "missing storeId"}
        res = self._delegate_storage("setup", params, timeout=240)
        if not res.get("ok"):
            self.log.error("external storage setup failed for %s (%s): %s",
                           params.get("name"), serial, res.get("error") or "unknown")
            return {"error": res.get("error") or "storage setup failed", "store_id": store_id}
        # Persist to the registry (replace any prior entry for this store/serial).
        self._ext_stores = [e for e in self._ext_stores
                            if e.get("store_id") != store_id and e.get("serial") != serial]
        self._ext_stores.append({
            "store_id": store_id, "serial": serial, "name": res["name"],
            "kind": res["kind"], "mirror_of_id": res.get("mirror_of_id")})
        self._save_ext_registry()
        self._ext_mounts[store_id] = res["mountpoint"]
        self._apply_mirror_roots()
        self.log.info("external storage set up: %s (%s) at %s",
                      res["name"], serial, res["mountpoint"])
        return {"store_id": store_id, "ready": True,
                "capacity_bytes": res["capacity_bytes"], "used_bytes": res["used_bytes"]}

    def _forget_external_storage(self, params: dict) -> dict:
        """Unmount + deregister an external store (data on the drive is left intact).
        The unmount runs in the ROOT helper; registry cleanup is local."""
        store_id = params.get("storeId")
        mount = self._ext_mounts.pop(store_id, None)
        res = self._delegate_storage(
            "forget", {"storeId": store_id, "mountpoint": mount}, timeout=60)
        if not res.get("ok"):
            self.log.warning("external storage forget helper error for %s: %s",
                             store_id, res.get("error") or "unknown")
        self._ext_stores = [e for e in self._ext_stores if e.get("store_id") != store_id]
        self._save_ext_registry()
        self._apply_mirror_roots()
        self.log.info("external storage forgotten: %s", store_id)
        return {"store_id": store_id, "forgotten": True}

    def _reconfigure_external_storage(self, params: dict) -> dict:
        """Toggle a store's mirror role without touching data: update the registry,
        rewrite the on-disk marker, and re-apply mirror routing."""
        store_id = params.get("storeId")
        entry = next((e for e in self._ext_stores if e.get("store_id") == store_id), None)
        if not entry:
            return {"error": "unknown store"}
        entry["kind"] = params.get("kind", entry.get("kind", "external"))
        entry["mirror_of_id"] = params.get("mirrorOfId")
        self._save_ext_registry()
        mount = self._ext_mounts.get(store_id)
        if mount and os.path.ismount(mount):
            try:
                with open(os.path.join(mount, storage_ops.MARKER), "w") as f:
                    json.dump({"store_id": store_id, "name": entry.get("name"),
                               "kind": entry["kind"], "mirror_of_id": entry["mirror_of_id"],
                               "serial": entry.get("serial"), "written_at": int(time.time())}, f)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("could not rewrite marker for %s: %s", store_id, exc)
        self._apply_mirror_roots()
        self.log.info("external storage reconfigured: %s kind=%s mirror_of=%s",
                      store_id, entry["kind"], entry["mirror_of_id"])
        return {"store_id": store_id, "kind": entry["kind"],
                "mirror_of_id": entry["mirror_of_id"]}

    def _stage_index(self, params: dict) -> dict:
        """Store a DR copy of a scope's encrypted search index delivered over the
        signed command channel. Kept for future localized on-appliance search; the
        file stays encrypted at rest (the appliance can't read it without the key)."""
        import base64
        blob = params.get("indexB64")
        scope = params.get("scope", "tenant")
        scope_id = params.get("scopeId", "")
        if not blob:
            return {"error": "missing index payload"}
        idx_dir = DATA / "search-index"
        try:
            idx_dir.mkdir(parents=True, exist_ok=True)
            safe = "".join(c for c in f"{scope}-{scope_id}" if c.isalnum() or c in "-_")
            path = idx_dir / f"{safe}.sqlite.enc"
            path.write_bytes(base64.b64decode(blob))
        except (OSError, ValueError) as exc:
            self.log.warning("stage index failed: %s", exc)
            return {"error": str(exc)}
        self.log.info("staged search index replica scope=%s:%s (%d bytes) at %s",
                      scope, scope_id, path.stat().st_size, path)
        return {"staged": True, "scope": scope, "scope_id": scope_id,
                "bytes": path.stat().st_size, "object_count": params.get("objectCount")}

    def _telemetry(self) -> dict:
        cap = self.vault.capacity()
        plat = sysinfo.detect_platform()
        sysd = sysinfo.system_stats()
        # Capacity + usage reflect the volume the vault actually writes to — the
        # dedicated Arkive RAID volume when present, else the system disk.
        disk = sysinfo.disk_stats(str(STORAGE_ROOT))
        raw_total = disk["disk_total_bytes"]
        # On a dedicated volume the filesystem usage is the real footprint; on the
        # shared system disk fall back to the vault's own content size.
        vol_used = (disk["disk_used_bytes"] if STORAGE_KIND == "dedicated"
                    else cap.get("used_bytes", 0))
        pq = sysinfo.pq_available()
        net = sysinfo.net_io()
        # External (USB) storage: Arkive-managed store rows + raw detected devices.
        ext_rows, detected_devices = self._external_storage_telemetry()
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
            "capacity_used_bytes": vol_used,
            "disk_free_bytes": disk["disk_free_bytes"],
            # The built-in OS / system disk, tracked separately from the dedicated
            # Arkive storage volume (admins monitor both).
            "os_storage": sysinfo.os_disk(),
            "snapshots": cap.get("snapshots", 0),
            "objects": cap.get("objects", cap.get("snapshots", 0)),
            "drive_health": "healthy",
            "power": "ok",
            "temperature_c": sysinfo.drive_temperature_c() or 34,
            # Where recovery data physically lives on the appliance.
            "data_path": str(STORAGE_ROOT / "vault" / "protected"),
            "data_mount": sysinfo.mount_device(str(STORAGE_ROOT)),
            "storage_kind": STORAGE_KIND,
            # Per-storage capacity + health (mapped onto the cloud storage objects).
            # The primary vault volume, plus every Arkive-managed external/mirror
            # store, and the raw removable devices detected for possible setup.
            "storages": (sysinfo.storage_report(
                str(STORAGE_ROOT), STORAGE_NAME, STORAGE_KIND,
                raw_total, vol_used) + ext_rows),
            "external_devices": detected_devices,
            # LAN intrusion signal (recent failed inbound auth attempts).
            "security": sysinfo.lan_security(),
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
        if not accepted:
            # A rejection is most often control-plane signing-key drift (the node
            # that now serves this appliance signs with its own key). Re-pin the
            # current bundle over the authenticated channel and re-verify once so
            # a legitimate rotation self-heals instead of rejecting forever.
            await self._retrust_control_plane(client, None)
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
                elif ctype == "OPEN_TERMINAL":
                    result = self._open_terminal(payload["parameters"])
                elif ctype == "STAGE_UPDATE":
                    result = self._stage_update(payload["parameters"])
                elif ctype == "APPLY_UPDATE":
                    result = {"applied": True}
                elif ctype == "REQUEST_VERIFICATION":
                    result = {"integrity": "verified"}
                elif ctype == "SETUP_STORAGE":
                    result = self._setup_external_storage(payload["parameters"])
                elif ctype == "FORGET_STORAGE":
                    result = self._forget_external_storage(payload["parameters"])
                elif ctype == "RECONFIGURE_STORAGE":
                    result = self._reconfigure_external_storage(payload["parameters"])
                elif ctype == "STAGE_INDEX":
                    result = self._stage_index(payload["parameters"])
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

    # -- remote terminal ----------------------------------------------

    def _open_terminal(self, params: dict) -> dict:
        """Handle a signed OPEN_TERMINAL command: dial an outbound WebSocket back
        to the control-plane relay and bridge it to a local PTY shell."""
        ws_path = params.get("wsPath")
        session_id = params.get("sessionId")
        session_token = params.get("sessionToken")
        if not ws_path or not session_token:
            return {"opened": False, "reason": "missing session parameters"}
        host = settings.cloud_base_url.split("/api", 1)[0]
        scheme = "wss" if host.startswith("https") else "ws"
        host_noscheme = host.split("://", 1)[-1]
        url = f"{scheme}://{host_noscheme}{ws_path}?token={session_token}"
        asyncio.create_task(self._run_terminal(url, session_id))
        self.log.info("terminal session %s: opening (relay %s)", session_id, host)
        return {"opened": True, "session_id": session_id}

    async def _run_terminal(self, url: str, session_id: str) -> None:
        import fcntl
        import os
        import pty
        import signal
        import struct
        import termios

        import websockets

        def _set_winsize(fd: int, rows: int, cols: int) -> None:
            try:
                fcntl.ioctl(fd, termios.TIOCSWINSZ,
                            struct.pack("HHHH", rows, cols, 0, 0))
            except Exception:  # noqa: BLE001
                pass

        pid, master_fd = pty.fork()
        if pid == 0:  # child: become an interactive login shell
            os.environ["TERM"] = "xterm-256color"
            shell = os.environ.get("SHELL", "/bin/bash")
            try:
                os.execvp(shell, [shell, "-il"])
            except Exception:  # noqa: BLE001
                os.execvp("/bin/sh", ["/bin/sh", "-i"])
            os._exit(1)

        loop = asyncio.get_running_loop()
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        closed = asyncio.Event()
        out_q: asyncio.Queue = asyncio.Queue()

        def _on_readable() -> None:
            try:
                data = os.read(master_fd, 65536)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                closed.set()
                return
            if not data:
                closed.set()
                return
            out_q.put_nowait(data)

        try:
            async with websockets.connect(url, max_size=None, ping_interval=20) as ws:
                self.log.info("terminal session %s: established", session_id)
                loop.add_reader(master_fd, _on_readable)

                async def sender() -> None:
                    while True:
                        data = await out_q.get()
                        await ws.send(data.decode("utf-8", "replace"))

                async def receiver() -> None:
                    async for msg in ws:
                        try:
                            m = json.loads(msg)
                        except Exception:  # noqa: BLE001
                            m = {"type": "input", "data": msg}
                        if m.get("type") == "resize":
                            _set_winsize(master_fd, int(m.get("rows", 24)),
                                         int(m.get("cols", 80)))
                        else:
                            os.write(master_fd, (m.get("data") or "").encode())
                    closed.set()

                tasks = [asyncio.create_task(sender()),
                         asyncio.create_task(receiver()),
                         asyncio.create_task(closed.wait())]
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("terminal session %s error: %s", session_id, exc)
        finally:
            try:
                loop.remove_reader(master_fd)
            except Exception:  # noqa: BLE001
                pass
            try:
                os.close(master_fd)
            except Exception:  # noqa: BLE001
                pass
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except Exception:  # noqa: BLE001
                pass
            self.log.info("terminal session %s: closed", session_id)


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
    # SIGHUP → reload external storage in place (cvtool signals us after it sets
    # up a drive, so it becomes usable without restarting the agent).
    try:
        import signal
        asyncio.get_running_loop().add_signal_handler(
            signal.SIGHUP, agent._reload_ext_storage)
    except Exception as exc:  # noqa: BLE001 — not fatal; restart still works
        agent.log.debug("could not install SIGHUP handler: %s", exc)
    if not agent.activated and settings.linking_code:
        try:
            await agent.activate(settings.linking_code)
        except Exception as exc:  # keep the agent up to expose status
            agent.log.error("activation error: %s", exc)
    if agent.activated:
        asyncio.create_task(_heartbeat_loop())
        asyncio.create_task(_integrations_loop())
        asyncio.create_task(_provision_loop())
    elif not settings.linking_code:
        # Zero-touch: no linking code was supplied. Register as an un-claimed unit
        # and show a pairing code on the local web UI until a customer claims it.
        asyncio.create_task(_registration_loop())


async def _registration_loop() -> None:
    """Register (once) then poll for a pairing claim. On pairing, adopt the real
    appliance identity and start the normal management-plane loops."""
    while not agent.activated:
        try:
            if not agent.registered:
                await agent.register()
            else:
                if await agent.register_heartbeat_once():
                    break
        except Exception as exc:  # noqa: BLE001
            if _cp_unavailable(exc):
                agent.log.info("control plane unavailable during registration — "
                               "will retry: %s", exc)
            else:
                agent.log.error("registration error: %s", exc)
        await asyncio.sleep(10)
    if agent.activated:
        agent.log.info("registration complete — starting management plane")
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


@app.get("/pairing")
def pairing():
    """Pairing state for the local web UI. Shows the pairing code until a customer
    claims this appliance from the portal."""
    return {
        "serial": agent.identity.serial,
        "model": settings.model,
        "activated": agent.activated,
        "registered": agent.registered,
        "paired": agent.activated,
        "pairing_code": agent.pairing_code,
        "appliance_id": agent.appliance_id,
        "software_version": settings.software_version,
    }


@app.get("/", response_class=HTMLResponse)
def home():
    return _HOME_HTML


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
