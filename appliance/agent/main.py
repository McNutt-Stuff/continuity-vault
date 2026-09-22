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
import threading
import time
import uuid
from datetime import datetime, timezone
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

# How long a heavy-telemetry snapshot (full-vault capacity + drive health) is
# reused before a background refresh recomputes it.
_HEAVY_TTL = 60.0

# External (USB) storage: where Arkive mounts set-up drives, the local registry
# that remembers them (by serial) so a reconnected drive is re-mounted on boot,
# the request queue to the privileged root helper, and the cached mirror-integrity
# report (written by the scheduled verify; read by telemetry + `cvtool storage
# verify`).
EXT_BASE = DATA / "ext"
EXT_REGISTRY = DATA / "ext_stores.json"
EXT_QUEUE = DATA / "storage-queue"
MIRROR_INTEGRITY = DATA / "mirror_integrity.json"
# Written by the root self-updater (installers/appliance-update.sh) each run, read
# here so the agent can forward the self-update outcome to the control plane.
UPDATE_STATUS = DATA / "update-status.json"
UPDATE_LOG = DATA / "update.log"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z"


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


class Agent:
    def __init__(self) -> None:
        self.identity = ApplianceIdentity(settings.data_dir)
        self.sm = StateMachine(State.PROVISIONING)
        self.vault = VaultStore(str(STORAGE_ROOT / "vault"), self.sm)
        self.appliance_id: Optional[str] = None
        self.tenant_id: Optional[str] = None
        self.agent_token: Optional[str] = None
        self.cloud_bundle: Optional[dict] = None
        self.config: dict = {}
        self.tamper_state = "normal"
        self.pending_recovery: dict = {}  # snapshot -> awaiting local approval
        self.log = agent_log.setup_logging(_LOG_FILE)
        # External-storage registry + live mountpoints. Logging is ready above so
        # the mount path (which logs) is safe to run here. _store_conn tracks each
        # store's last-known usable state so a change is logged once (not every
        # heartbeat).
        self._ext_stores: list[dict] = self._load_ext_registry()
        self._ext_mounts: dict[str, str] = {}
        self._store_conn: dict[str, bool] = {}
        self._mount_ext_stores()
        self._last_update_note = ""
        self._last_latency_ms: Optional[int] = None  # heartbeat round-trip
        # Last self-update run we've already forwarded to the control plane (so a
        # new run is logged once, not every heartbeat).
        self._last_update_ran_at = ""
        # Heavy telemetry (full-vault capacity walk + SMART/RAID subprocesses) is
        # computed on a BACKGROUND thread and cached, so /status and heartbeat never
        # block on a large vault. See _heavy_telemetry.
        self._heavy_cache: dict = {}
        self._heavy_at: float = 0.0
        self._heavy_refreshing = False
        # Zero-touch pairing: when installed WITHOUT a linking code the appliance
        # registers with the control plane as an un-claimed unit and shows a
        # pairing code on its local web UI until a customer claims it.
        self.registration_id: Optional[str] = None
        self.reg_token: Optional[str] = None
        self.pairing_code: Optional[str] = None
        # Assigned customer node (federated fleets): once set, all signaling,
        # commands and receipts go here instead of the control plane.
        self._node_url: Optional[str] = None
        # A node that just rejected us (e.g. right after a switchover, before it
        # replicated our record) is put on a short cooldown so we stay on the
        # control plane instead of re-adopting it and flip-flopping every beat.
        self._node_cooldown: dict[str, float] = {}
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

    def _adopt_node(self, url: Optional[str]) -> None:
        """Adopt the assigned-node URL the CP advertises, UNLESS that node is on a
        post-rejection cooldown (it just switched over and hasn't replicated our
        record yet). Staying on the control plane a little longer avoids a per-beat
        node↔CP flip-flop until the new active node is warm."""
        url = (url or "").rstrip("/") or None
        if url and self._node_cooldown.get(url, 0) > time.time():
            if self._node_url is not None:
                self._set_node_url(None)  # ride the CP until the cooldown clears
            return
        self._set_node_url(url)

    async def heartbeat_once(self) -> None:
        # Surface the root self-updater's last outcome (once per run) so a stuck /
        # failing self-update is visible from the control plane.
        self._forward_update_status()
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
                    self._node_cooldown[self._node_url] = time.time() + 120
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
                self._node_cooldown[self._node_url] = time.time() + 120
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
            self._adopt_node(data.get("node_url") or None)
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

    # -- self-update status (from the root self-updater) --------------

    def _read_update_status(self) -> dict:
        try:
            return json.loads(UPDATE_STATUS.read_text())
        except Exception:  # noqa: BLE001
            return {}

    def _forward_update_status(self) -> None:
        """Log the outcome of the ROOT self-updater once per run so a stuck/failed
        self-update is visible in Platform Logs. The updater (a separate root
        service) writes update-status.json; the agent — which forwards logs — turns
        each new run into a log line (WARNING on failure so it surfaces at the
        default log level)."""
        st = self._read_update_status()
        ran = st.get("ran_at") or ""
        if not ran or ran == self._last_update_ran_at:
            return
        self._last_update_ran_at = ran
        result = st.get("result")
        frm, to = st.get("from_version") or "?", st.get("to_version") or "?"
        msg = st.get("message") or ""
        if result == "failed":
            self.log.warning("appliance self-update FAILED (%s -> %s): %s", frm, to, msg)
        elif result == "updated":
            self.log.info("appliance self-update applied: %s -> %s", frm, to)
        else:  # up-to-date / unknown — don't spam Platform Logs every 10 min
            self.log.debug("appliance self-update: %s (%s)", result, msg)

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

    def _excluded_disks(self) -> set:
        return {sysinfo.system_disk_device(),
                sysinfo.dedicated_disk_device(settings.dedicated_path)}

    def _delegate_storage(self, action: str, params: dict, timeout: int = 240) -> dict:
        """Hand a privileged disk op (format/mount/unmount) to the ROOT helper via
        the storage queue and wait for its result. The helper runs unsandboxed
        (cv-appliance-storage.service, triggered by cv-appliance-storage.path)."""
        req_id = uuid.uuid4().hex
        EXT_QUEUE.mkdir(parents=True, exist_ok=True)
        res_path = EXT_QUEUE / f"res-{req_id}.json"
        req_path = EXT_QUEUE / f"req-{req_id}.json"
        try:
            req_path.write_text(json.dumps({"action": action, "params": params}))
        except OSError as exc:
            self.log.error("storage %s: could not queue request to the root helper at %s: %s",
                           action, EXT_QUEUE, exc)
            return {"error": f"could not queue storage request: {exc}"}
        self.log.info("storage %s: queued request %s for the root helper (%s); waiting up to %ds",
                      action, req_id, EXT_QUEUE, timeout)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if res_path.exists():
                try:
                    result = json.loads(res_path.read_text())
                finally:
                    res_path.unlink(missing_ok=True)
                    req_path.unlink(missing_ok=True)
                self.log.info("storage %s: root helper responded: %s", action, result)
                return result
            time.sleep(0.5)
        req_path.unlink(missing_ok=True)
        self.log.error("storage %s: the root helper never responded within %ds — the privileged "
                       "helper (cv-appliance-storage.service / .path) is likely not installed or "
                       "not running. Re-run the appliance installer. Queue dir: %s",
                       action, timeout, EXT_QUEUE)
        return {"error": "storage helper did not respond (is cv-appliance-storage "
                         "installed and running?)"}

    def _mount_ext_stores(self) -> None:
        """Mount every already-set-up external drive that's currently present, then
        point the vault at any mirror volumes so writes/recoveries duplicate. A
        drive that isn't present, or can't be mounted, is skipped (logged) — never
        fatal to startup."""
        self._ext_mounts = {}
        for e in self._ext_stores:
            try:
                mp = self._mount_known_ext(e)
                if mp:
                    self._ext_mounts[e["store_id"]] = mp
            except Exception as exc:  # noqa: BLE001
                self.log.warning("could not mount external store %s: %s",
                                 e.get("name"), exc)
        self._apply_mirror_roots()

    def _mount_known_ext(self, e: dict) -> Optional[str]:
        """Return the mountpoint of a known external store, mounting it if needed
        (delegated to the root helper — the sandboxed agent can't mount). Returns
        None when the drive isn't present or couldn't be mounted."""
        store_id = e["store_id"]
        mountpoint = os.path.join(str(EXT_BASE), store_id)
        if os.path.ismount(mountpoint):
            return mountpoint
        if not sysinfo.resolve_device_by_serial(e.get("serial", "")):
            self.log.info("external store %s (SN %s) not present — skipping mount",
                          e.get("name"), e.get("serial"))
            return None
        res = self._delegate_storage("mount", {
            "serial": e.get("serial", ""), "storeId": store_id,
            "name": e.get("name", "External Storage"),
            "mirrorOfId": e.get("mirror_of_id"), "kind": e.get("kind", "external"),
        }, timeout=90)
        if res.get("error"):
            self.log.warning("could not mount external store %s via the root helper: %s",
                             e.get("name"), res["error"])
            return None
        return res.get("mountpoint")

    def _apply_mirror_roots(self, force_verify: bool = False) -> None:
        # Only route mirroring to volumes that are actually healthy right now. A
        # drive that's mounted but dead (ext4 'shutdown' after an I/O fault) is
        # EXCLUDED so backups + _sync_mirrors don't hammer it with failing writes;
        # it's reported disconnected and resumes once repaired/reconnected.
        roots: list[str] = []
        for e in self._ext_stores:
            if e.get("kind") != "mirror":
                continue
            mount = self._ext_mounts.get(e["store_id"])
            if not mount:
                continue
            if not sysinfo.mount_health(mount).get("healthy"):
                self.log.warning("mirror volume %s at %s is not usable (drive I/O error?) — "
                                 "excluding from mirroring until repaired", e.get("name"), mount)
                continue
            roots.append(os.path.join(mount, "vault"))
        try:
            self.vault.set_mirror_roots(roots)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("could not apply mirror roots: %s", exc)
        self.log.info("mirror routing applied: %d mirror volume(s) active", len(roots))
        if roots:
            threading.Thread(target=self._sync_mirrors, args=("mirror routing changed", force_verify),
                             daemon=True).start()

    def _sync_mirrors(self, reason: str = "", force_verify: bool = False) -> dict:
        """Reconcile every mirror volume with the primary vault (object data) AND
        the encrypted search index — a backfill for a newly-added mirror plus repair
        for anything the live duplication missed. Idempotent."""
        mirror_mounts = [self._ext_mounts[e["store_id"]] for e in self._ext_stores
                         if e.get("kind") == "mirror" and e["store_id"] in self._ext_mounts]
        if not mirror_mounts:
            return {"mirrors": 0}
        # Show "resyncing…" (in_sync=null) while the backfill runs so the UI doesn't
        # display a stale "out of sync" during/right after a repair.
        self._mark_mirror_syncing()
        res: dict = {"mirrors": len(mirror_mounts)}
        try:
            res.update(self.vault.sync_mirrors())
        except Exception as exc:  # noqa: BLE001
            self.log.warning("mirror data sync failed: %s", exc)
            res["errors"] = res.get("errors", 0) + 1
        idx_src = DATA / "search-index"
        idx_copied = idx_pruned = 0
        if idx_src.is_dir():
            src_files = {f.name for f in idx_src.glob("*.enc")}
            for mount in mirror_mounts:
                dst_dir = Path(mount) / "search-index"
                try:
                    dst_dir.mkdir(parents=True, exist_ok=True)
                    for f in idx_src.glob("*.enc"):
                        dst = dst_dir / f.name
                        s = f.stat()
                        if dst.exists():
                            d = dst.stat()
                            if d.st_size == s.st_size and int(d.st_mtime) >= int(s.st_mtime):
                                continue
                        data = f.read_bytes()
                        tmp = dst.with_suffix(dst.suffix + ".tmp")
                        tmp.write_bytes(data)
                        if tmp.stat().st_size != len(data):
                            raise OSError("short index mirror write")
                        tmp.replace(dst)
                        idx_copied += 1
                    if src_files:
                        for dst in dst_dir.glob("*.enc"):
                            if dst.name not in src_files:
                                dst.unlink()
                                idx_pruned += 1
                    for stray in dst_dir.glob("*.tmp"):
                        try:
                            stray.unlink()
                        except OSError:
                            pass
                except Exception as exc:  # noqa: BLE001
                    self.log.warning("mirror index sync to %s failed: %s", mount, exc)
                    res["errors"] = res.get("errors", 0) + 1
        res["index_files_copied"] = idx_copied
        res["index_files_pruned"] = idx_pruned
        self.log.info("mirror sync (%s): %s", reason or "periodic", res)
        if force_verify or any(res.get(k) for k in ("files_copied", "files_pruned", "snapshots_pruned",
                                                    "index_files_copied", "index_files_pruned")):
            try:
                self._verify_mirrors("after sync")
            except Exception as exc:  # noqa: BLE001
                self.log.debug("post-sync verify failed: %s", exc)
        return res

    def _verify_mirrors(self, reason: str = "") -> dict:
        """Read-only integrity check: confirm every mirror volume is a true 1:1 copy
        of the primary vault AND the replicated search index. Caches the report to
        MIRROR_INTEGRITY for telemetry + `cvtool storage verify`."""
        mirror_entries = [e for e in self._ext_stores
                          if e.get("kind") == "mirror" and e["store_id"] in self._ext_mounts]
        report: dict = {"mirrors": len(mirror_entries),
                        "checked_at": _now_iso(), "in_sync": True, "stores": []}
        if not mirror_entries:
            report["in_sync"] = None
            self._write_mirror_integrity(report)
            return report
        try:
            vault_rep = self.vault.verify_mirrors()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("mirror verify (data) failed: %s", exc)
            vault_rep = {"roots": [], "in_sync": False, "error": str(exc)[:200]}
        by_root = {r.get("mirror_root"): r for r in vault_rep.get("roots", [])}
        idx_src = DATA / "search-index"
        src_idx = {f.name: f.stat().st_size for f in idx_src.glob("*.enc")} if idx_src.is_dir() else {}
        for e in mirror_entries:
            sid = e["store_id"]
            mount = self._ext_mounts.get(sid)
            data_root = str(Path(mount) / "vault" / "protected") if mount else ""
            drep = by_root.get(data_root, {})
            idx_dir = Path(mount) / "search-index" if mount else None
            mir_idx = ({f.name: f.stat().st_size for f in idx_dir.glob("*.enc")}
                       if idx_dir and idx_dir.is_dir() else {})
            idx_missing = sum(1 for n, s in src_idx.items() if mir_idx.get(n) != s)
            idx_extra = sum(1 for n in mir_idx if n not in src_idx)
            data_ok = bool(drep.get("in_sync"))
            store_ok = data_ok and idx_missing == 0 and idx_extra == 0
            report["stores"].append({
                "store_id": sid, "name": e.get("name", "Mirror"),
                "serial": e.get("serial", ""), "mirror_of_id": e.get("mirror_of_id"),
                "connected": bool(drep.get("connected", bool(mount))),
                "in_sync": store_ok,
                "data": {k: drep.get(k) for k in (
                    "primary_snapshots", "mirror_snapshots", "primary_files",
                    "mirror_files", "primary_bytes", "mirror_bytes", "missing",
                    "extra", "sample_missing", "sample_extra")},
                "index": {"primary_files": len(src_idx), "mirror_files": len(mir_idx),
                          "missing": idx_missing, "extra": idx_extra},
            })
            if not store_ok:
                report["in_sync"] = False
        self._write_mirror_integrity(report)
        self.log.info("mirror verify (%s): in_sync=%s stores=%d",
                      reason or "scheduled", report["in_sync"], len(report["stores"]))
        return report

    def _write_mirror_integrity(self, report: dict) -> None:
        try:
            MIRROR_INTEGRITY.write_text(json.dumps(report))
        except Exception as exc:  # noqa: BLE001
            self.log.debug("could not cache mirror integrity: %s", exc)

    def _read_mirror_integrity(self) -> dict:
        try:
            return json.loads(MIRROR_INTEGRITY.read_text())
        except Exception:  # noqa: BLE001
            return {}

    def _mark_mirror_syncing(self) -> None:
        """Flag mirror integrity as pending (in_sync=null / syncing) so the UI shows
        'resyncing…' instead of a stale 'out of sync' while a backfill/repair sync is
        running; the follow-up verify writes the real state."""
        rep = self._read_mirror_integrity() or {}
        rep["in_sync"] = None
        rep["syncing"] = True
        for s in rep.get("stores", []) or []:
            s["in_sync"] = None
        self._write_mirror_integrity(rep)

    def _reload_ext_storage(self) -> None:
        """Re-read the registry and (re)mount known drives, then re-apply mirror
        routing. Triggered by SIGHUP so a drive that cvtool set up (as root, outside
        the sandbox) becomes usable without a full restart."""
        self.log.info("external storage reload requested (SIGHUP)")
        self._ext_stores = self._load_ext_registry()
        self._mount_ext_stores()
        # Drop the heavy-telemetry cache so the next heartbeat reflects the change.
        self._heavy_at = 0.0
        self.log.info("external storage reloaded — %d store(s), %d mounted",
                      len(self._ext_stores), len(self._ext_mounts))

    def _store_usable(self, store_id: str, mount: Optional[str], kind: str) -> tuple[bool, dict]:
        """Determine whether an external/mirror store is genuinely usable right now,
        distinguishing 'mounted but dead' (a dropped USB drive whose ext4 is in a
        shutdown/error state — ismount() still True) from truly connected. Logs the
        reason once whenever a store's usable state changes so 'why is it offline'
        is always answerable from the appliance log. Returns (usable, mount_health)."""
        present = bool(sysinfo.resolve_device_by_serial(
            next((e.get("serial", "") for e in self._ext_stores
                  if e.get("store_id") == store_id), "")))
        mh = sysinfo.mount_health(mount) if mount else {
            "mounted": False, "healthy": False, "reason": "not mounted", "options": ""}
        usable = bool(mh.get("healthy"))
        if not mh.get("mounted") and not present:
            mh["reason"] = mh.get("reason") or "drive not connected"
        prev = self._store_conn.get(store_id)
        if prev != usable:
            self._store_conn[store_id] = usable
            if usable:
                self.log.info("external store %s (%s) is CONNECTED and healthy at %s",
                              store_id, kind, mount)
            else:
                self.log.warning("external store %s (%s) is OFFLINE: %s "
                                 "(mount=%s options=%s)", store_id, kind,
                                 mh.get("reason") or "unknown", mount or "-",
                                 mh.get("options") or "-")
        return usable, mh

    def _external_storage_telemetry(self) -> tuple[list[dict], list[dict], str]:
        """(ext_store_rows, detected_devices, worst_drive_health) for the heartbeat.

        ext_store_rows are Arkive-managed stores with per-store capacity + real
        health (a mounted-but-dead drive reports 'disconnected', not 'healthy');
        detected_devices are raw removable candidates the cloud offers for setup;
        worst_drive_health folds every external/mirror store into the appliance-wide
        drive-health signal so a failed mirror drags the overall status down."""
        detected = sysinfo.detect_external_storage(self._excluded_disks())
        rows: list[dict] = []
        integ = self._read_mirror_integrity()
        integ_by_store = {s.get("store_id"): s for s in (integ.get("stores") or [])}
        worst = "healthy"
        for e in self._ext_stores:
            sid = e["store_id"]
            kind = e.get("kind", "external")
            mount = self._ext_mounts.get(sid)
            if mount and not os.path.ismount(mount):
                mount = None  # unmounted since we last mounted it
            connected, mh = self._store_usable(sid, mount, kind)
            cap = used = 0
            if connected:
                try:
                    st = os.statvfs(mount)
                    cap = st.f_blocks * st.f_frsize
                    used = (st.f_blocks - st.f_bfree) * st.f_frsize
                except OSError as exc:
                    self.log.warning("statvfs failed for store %s at %s: %s", sid, mount, exc)
                    connected = False
            drive_health = "healthy" if connected else "disconnected"
            if connected and mh.get("read_only"):
                drive_health = "degraded"
            if drive_health != "healthy":
                worst = "degraded" if worst == "healthy" else worst
            health = {"drive_health": drive_health,
                      "device": mount or "", "serial": e.get("serial", ""),
                      "mirror_of": e.get("mirror_of_id")}
            if mh.get("reason"):
                health["reason"] = mh["reason"]
            if e.get("kind") == "mirror":
                si = integ_by_store.get(sid)
                if si is not None:
                    health["mirror_integrity"] = {
                        "in_sync": si.get("in_sync"),
                        "checked_at": integ.get("checked_at"),
                        "data_missing": (si.get("data") or {}).get("missing"),
                        "data_extra": (si.get("data") or {}).get("extra"),
                        "index_missing": (si.get("index") or {}).get("missing"),
                        "index_extra": (si.get("index") or {}).get("extra"),
                        "primary_files": (si.get("data") or {}).get("primary_files"),
                        "mirror_files": (si.get("data") or {}).get("mirror_files"),
                        "primary_bytes": (si.get("data") or {}).get("primary_bytes"),
                        "mirror_bytes": (si.get("data") or {}).get("mirror_bytes"),
                    }
                    # An out-of-sync (but connected) mirror is a degraded signal.
                    if connected and si.get("in_sync") is False and worst == "healthy":
                        worst = "degraded"
                else:
                    health["mirror_integrity"] = {"in_sync": None, "checked_at": None}
            rows.append({
                "name": e.get("name", "External Storage"),
                "kind": kind,
                "store_id": sid,
                "device_serial": e.get("serial", ""),
                "mirror_of_id": e.get("mirror_of_id"),
                "capacity_bytes": cap,
                "used_bytes": used,
                "free_bytes": max(cap - used, 0),
                "connected": connected,
                "state": "ready" if connected else "disconnected",
                "health": health,
            })
        return rows, detected, worst

    def _setup_external_storage(self, params: dict) -> dict:
        """Handle a cloud-signed SETUP_STORAGE command. The privileged format+mount
        runs in the ROOT helper; we then adopt the ready volume in place."""
        store_id = params.get("storeId")
        serial = params.get("serial") or ""
        if not store_id:
            return {"error": "missing storeId"}
        res = self._delegate_storage("setup", params, timeout=240)
        if not res.get("ok"):
            self.log.error("external storage setup failed for %s (%s): %s",
                           params.get("name"), serial, res.get("error") or "unknown")
            return {"error": res.get("error") or "storage setup failed", "store_id": store_id}
        self._ext_stores = [e for e in self._ext_stores
                            if e.get("store_id") != store_id and e.get("serial") != serial]
        self._ext_stores.append({
            "store_id": store_id, "serial": serial, "name": res["name"],
            "kind": res["kind"], "mirror_of_id": res.get("mirror_of_id")})
        self._save_ext_registry()
        self._ext_mounts[store_id] = res["mountpoint"]
        self._apply_mirror_roots()
        self._heavy_at = 0.0
        self.log.info("external storage set up: %s (%s) at %s",
                      res["name"], serial, res["mountpoint"])
        return {"store_id": store_id, "ready": True,
                "capacity_bytes": res["capacity_bytes"], "used_bytes": res["used_bytes"]}

    def _forget_external_storage(self, params: dict) -> dict:
        """Unmount + deregister an external store (data on the drive is left intact)."""
        store_id = params.get("storeId")
        mount = self._ext_mounts.pop(store_id, None)
        res = self._delegate_storage(
            "forget", {"storeId": store_id, "mountpoint": mount}, timeout=60)
        if not res.get("ok"):
            self.log.warning("external storage forget helper error for %s: %s",
                             store_id, res.get("error") or "unknown")
        self._ext_stores = [e for e in self._ext_stores if e.get("store_id") != store_id]
        self._store_conn.pop(store_id, None)
        self._save_ext_registry()
        self._apply_mirror_roots()
        self._heavy_at = 0.0
        self.log.info("external storage forgotten: %s", store_id)
        return {"store_id": store_id, "forgotten": True}

    def _repair_external_storage(self, params: dict) -> dict:
        """Handle a cloud-signed REPAIR_STORAGE command: force-release a dead/stale
        mount and re-mount the drive by serial (non-destructive), then re-apply
        mirror routing so a repaired mirror resumes duplicating."""
        store_id = params.get("storeId")
        entry = next((e for e in self._ext_stores if e.get("store_id") == store_id), None)
        if not entry:
            return {"error": "unknown store"}
        self.log.info("repairing external store %s (%s)", store_id, entry.get("name"))
        res = self._delegate_storage("repair", {
            "storeId": store_id, "serial": entry.get("serial", ""),
            "name": entry.get("name", "External Storage"),
            "mirrorOfId": entry.get("mirror_of_id"), "kind": entry.get("kind", "external"),
        }, timeout=120)
        if not res.get("ok"):
            self.log.warning("external storage repair failed for %s: %s",
                             store_id, res.get("error") or "unknown")
            return {"error": res.get("error") or "storage repair failed", "store_id": store_id}
        self._ext_mounts[store_id] = res["mountpoint"]
        self._store_conn.pop(store_id, None)  # force a fresh connected-state log line
        self._apply_mirror_roots(force_verify=True)
        self._heavy_at = 0.0
        self.log.info("external storage repaired: %s at %s", store_id, res["mountpoint"])
        return {"store_id": store_id, "repaired": True,
                "capacity_bytes": res.get("capacity_bytes"), "used_bytes": res.get("used_bytes")}

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
        self._heavy_at = 0.0
        self.log.info("external storage reconfigured: %s kind=%s mirror_of=%s",
                      store_id, entry["kind"], entry["mirror_of_id"])
        return {"store_id": store_id, "kind": entry["kind"],
                "mirror_of_id": entry["mirror_of_id"]}

    def _stage_index(self, params: dict) -> dict:
        """Store a DR copy of a scope's encrypted search index (pulled over the
        authenticated HTTPS channel; tiny legacy payloads may arrive inline). The
        file stays encrypted at rest."""
        import base64
        scope = params.get("scope", "tenant")
        scope_id = params.get("scopeId", "")
        store_id = params.get("storeId", "")
        idx_dir = DATA / "search-index"
        safe = "".join(c for c in f"{scope}-{scope_id}" if c.isalnum() or c in "-_")
        path = idx_dir / f"{safe}.sqlite.enc"
        try:
            idx_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.log.error("stage index: cannot create %s: %s", idx_dir, exc)
            return {"error": str(exc)}
        blob = params.get("indexB64")
        if blob:
            self.log.info("stage index: writing inline payload scope=%s:%s", scope, scope_id)
            try:
                path.write_bytes(base64.b64decode(blob))
            except (OSError, ValueError) as exc:
                self.log.error("stage index (inline) failed scope=%s:%s: %s", scope, scope_id, exc)
                return {"error": str(exc)}
        elif params.get("pull"):
            url = (f"{self._base()}/appliance/index-replica"
                   f"?scope={scope}&scopeId={scope_id}&storeId={store_id}")
            self.log.info("stage index: pulling scope=%s:%s (%s bytes expected) from %s",
                          scope, scope_id, params.get("bytes"), url)
            try:
                tmp = path.with_suffix(path.suffix + ".tmp")
                with httpx.Client(timeout=120) as c:
                    with c.stream("GET", url, headers=self._headers()) as resp:
                        if resp.status_code != 200:
                            body = resp.read()[:200]
                            self.log.error("stage index: pull HTTP %s for scope=%s:%s — %s",
                                           resp.status_code, scope, scope_id, body)
                            return {"error": f"index pull failed: HTTP {resp.status_code}"}
                        with open(tmp, "wb") as fh:
                            for chunk in resp.iter_bytes(1024 * 256):
                                fh.write(chunk)
                tmp.replace(path)
            except Exception as exc:  # noqa: BLE001
                self.log.exception("stage index: pull failed scope=%s:%s", scope, scope_id)
                return {"error": f"index pull failed: {exc}"}
        else:
            self.log.warning("stage index: no payload and no pull flag scope=%s:%s", scope, scope_id)
            return {"error": "missing index payload"}
        size = path.stat().st_size
        self.log.info("staged search index replica scope=%s:%s (%d bytes) at %s",
                      scope, scope_id, size, path)
        return {"staged": True, "scope": scope, "scope_id": scope_id,
                "bytes": size, "object_count": params.get("objectCount")}

    def _heavy_telemetry(self) -> dict:
        """The expensive telemetry — a full-vault capacity walk plus the storage
        SMART/RAID/filesystem subprocesses — computed on a BACKGROUND thread and
        cached, so /status and heartbeat return instantly and never freeze the
        appliance on a large vault. Returns the last snapshot immediately and kicks
        off a refresh when it's stale."""
        now = time.time()
        if (now - self._heavy_at) > _HEAVY_TTL and not self._heavy_refreshing:
            self._heavy_refreshing = True
            threading.Thread(target=self._refresh_heavy, name="cv-telemetry", daemon=True).start()
        return self._heavy_cache

    def _refresh_heavy(self) -> None:
        try:
            cap = self.vault.capacity()
            disk = sysinfo.disk_stats(str(STORAGE_ROOT))
            raw_total = disk["disk_total_bytes"]
            # On a dedicated volume the filesystem usage is the real footprint; on
            # the shared system disk fall back to the vault's own content size.
            vol_used = (disk["disk_used_bytes"] if STORAGE_KIND == "dedicated"
                        else cap.get("used_bytes", 0))
            primary = sysinfo.storage_report(
                str(STORAGE_ROOT), STORAGE_NAME, STORAGE_KIND, raw_total, vol_used)
            # External (USB) storage: Arkive-managed store rows + raw detected
            # devices + the worst external drive-health signal. Runs here (off the
            # hot path) because it probes each drive (lsblk/statvfs/write test).
            ext_rows, detected_devices, ext_worst = self._external_storage_telemetry()
            # Appliance-wide drive health = worst of the primary volume + every
            # external/mirror store, so a failed/degraded/disconnected drive is
            # reflected in the overall status instead of always reading "healthy".
            primary_health = (primary[0].get("health", {}) if primary else {}).get("drive_health", "healthy")
            order = {"healthy": 0, "degraded": 1, "disconnected": 2, "failing": 3}
            overall = max((primary_health, ext_worst), key=lambda h: order.get(h, 1))
            self._heavy_cache = {
                "capacity_total_bytes": raw_total,
                "capacity_used_bytes": vol_used,
                "disk_free_bytes": disk["disk_free_bytes"],
                "snapshots": cap.get("snapshots", 0),
                "objects": cap.get("objects", cap.get("snapshots", 0)),
                "temperature_c": sysinfo.drive_temperature_c() or 34,
                "data_mount": sysinfo.mount_device(str(STORAGE_ROOT)),
                "storages": primary + ext_rows,
                "external_devices": detected_devices,
                "drive_health": overall,
            }
            self._heavy_at = time.time()
        except Exception as exc:  # noqa: BLE001 — telemetry must never crash the agent
            self.log.warning("telemetry refresh failed: %s", exc)
        finally:
            self._heavy_refreshing = False

    def _telemetry(self) -> dict:
        # Heavy fields (capacity walk + drive health) come from the background cache;
        # cheap live stats (cpu/mem/load/net) are computed inline so they stay fresh.
        heavy = self._heavy_telemetry()
        plat = sysinfo.detect_platform()
        sysd = sysinfo.system_stats()
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
            # Storage / stored data (from the background heavy-telemetry cache)
            "capacity_total_bytes": heavy.get("capacity_total_bytes", 0),
            "capacity_used_bytes": heavy.get("capacity_used_bytes", 0),
            "disk_free_bytes": heavy.get("disk_free_bytes", 0),
            # The built-in OS / system disk, tracked separately from the dedicated
            # Arkive storage volume (admins monitor both). shutil.disk_usage = fast.
            "os_storage": sysinfo.os_disk(),
            "snapshots": heavy.get("snapshots", 0),
            "objects": heavy.get("objects", 0),
            "drive_health": heavy.get("drive_health", "healthy"),
            "power": "ok",
            "temperature_c": heavy.get("temperature_c", 34),
            # Where recovery data physically lives on the appliance.
            "data_path": str(STORAGE_ROOT / "vault" / "protected"),
            "data_mount": heavy.get("data_mount", ""),
            "storage_kind": STORAGE_KIND,
            # Per-storage capacity + health (primary volume + every Arkive-managed
            # external/mirror store) mapped onto the cloud storage objects, plus the
            # raw removable devices detected for possible setup.
            "storages": heavy.get("storages", []),
            "external_devices": heavy.get("external_devices", []),
            # Encryption
            "quantum_safe": bool(pq),
            "content_alg": "AES-256-GCM",
            "signing_alg": (self.identity.signer.pq_alg if self.identity else None),
            "isolation_state": self.sm.isolation_state,
            # Logs (forwarded like the endpoint agent)
            "recent_logs": agent_log.tail(_LOG_FILE, 50),
            "software_version": settings.software_version,
            # Last outcome of the root self-updater (from update-status.json) so the
            # admin can see WHY a "update available" appliance isn't updating.
            "update_status": self._read_update_status(),
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
                    if not result.get("error"):
                        threading.Thread(target=self._sync_mirrors, args=("after ingest",),
                                         daemon=True).start()
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
                    result = self._trigger_self_update()
                elif ctype == "REQUEST_VERIFICATION":
                    result = {"integrity": "verified"}
                elif ctype == "SETUP_STORAGE":
                    result = await asyncio.to_thread(self._setup_external_storage, payload["parameters"])
                elif ctype == "FORGET_STORAGE":
                    result = await asyncio.to_thread(self._forget_external_storage, payload["parameters"])
                elif ctype == "RECONFIGURE_STORAGE":
                    result = self._reconfigure_external_storage(payload["parameters"])
                elif ctype == "REPAIR_STORAGE":
                    # _repair_external_storage already re-applies mirror routing and
                    # kicks a forced resync+verify, so no extra sync here.
                    result = await asyncio.to_thread(self._repair_external_storage, payload["parameters"])
                elif ctype == "STAGE_INDEX":
                    result = await asyncio.to_thread(self._stage_index, payload["parameters"])
                    if not result.get("error"):
                        threading.Thread(target=self._sync_mirrors, args=("after index stage",),
                                         daemon=True).start()
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

    def _trigger_self_update(self) -> dict:
        """Kick the root self-updater now (operator-initiated 'Update now'). Runs
        detached via cvtool (whitelisted in sudoers) so restarting the agent
        mid-update doesn't kill the updater; the outcome is reported back via
        update-status.json on the next heartbeat."""
        import shutil
        import subprocess
        cvtool = shutil.which("cvtool") or "/usr/local/bin/cvtool"
        try:
            subprocess.Popen(["sudo", "-n", cvtool, "update", "--force"],
                             start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.log.info("self-update triggered (operator-initiated)")
            return {"triggered": True}
        except Exception as exc:  # noqa: BLE001
            self.log.warning("could not trigger self-update: %s", exc)
            return {"error": str(exc)}

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
        asyncio.create_task(_integrity_loop())
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
        asyncio.create_task(_integrity_loop())


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


async def _integrity_loop() -> None:
    """Periodically verify every mirror volume is a true 1:1 copy of the primary
    (data + index). First pass shortly after boot, then every 6h; the result is
    cached + shipped in telemetry so the admin/customer views show drive status.
    Self-healing: if a CONNECTED mirror verifies out of sync, kick a repair sync
    (backfill missed objects/index) automatically, then re-check soon to confirm."""
    await asyncio.sleep(90)  # let mounts settle + a first sync happen
    delay = 6 * 3600
    healed_last = False  # true right after an auto-repair, so the next pass is a confirm
    while True:
        try:
            rep = await asyncio.to_thread(agent._verify_mirrors, "scheduled")
            # Auto-repair drift on a still-connected mirror — the live duplication
            # missed a write (e.g. a transient I/O error) and no ingest/reconnect
            # has since re-synced it. _sync_mirrors is idempotent + self-verifying.
            needs_heal = (rep.get("in_sync") is False and any(
                s.get("connected") and s.get("in_sync") is False
                for s in rep.get("stores", []) or []))
            if needs_heal and not healed_last:
                agent.log.warning("scheduled verify found mirror(s) out of sync — "
                                  "auto-repairing (backfill resync)")
                await asyncio.to_thread(agent._sync_mirrors,
                                        "auto-repair (drift detected)", True)
                healed_last, delay = True, 900  # confirm the resync settled in 15 min
            elif needs_heal:
                # The resync didn't clear it — likely an unhealthy drive that needs
                # a physical Repair. Stop hammering; surface it and back off.
                agent.log.warning("mirror still out of sync after auto-repair — manual "
                                  "Repair/Resync may be needed (drive fault?)")
                healed_last, delay = False, 6 * 3600
            else:
                healed_last, delay = False, 6 * 3600
        except Exception as exc:  # noqa: BLE001
            agent.log.warning("scheduled mirror verify failed: %s", exc)
            healed_last, delay = False, 6 * 3600
        await asyncio.sleep(delay)


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
    asyncio.create_task(_integrity_loop())
    return {"activated": True, "appliance_id": d["appliance_id"]}


@app.post("/local-approve-recovery")
def local_approve_recovery(body: dict):
    """Physical recovery-approval button (spec 7.3 / 12)."""
    snapshot_id = body.get("snapshot_id")
    params = agent.pending_recovery.pop(snapshot_id, None)
    if not params:
        raise HTTPException(404, "no pending recovery for snapshot")
    return agent._perform_recovery(snapshot_id, params)
