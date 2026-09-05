"""
Arkive desktop agent (macOS).

Registers with the cloud using a linking code (like an appliance), collects data
locally via native tools (1Password `op` CLI), pushes normalized objects to the
platform (through the cloud to cloud storage, or directly to an appliance),
reports telemetry, and self-updates on a cloud command.

Run as a launchd service:  python -m agent.main run
CLI:  link <CODE> | run | collect | status
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import platform
import plistlib
import queue
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from .config import Config
from .collectors import onepassword
from .collectors import files as files_collector
from .collectors import imessage
from .collectors import outlook_local
from . import agent_log
from .crypto import encrypt_content, load_or_create_key, wrap_for_recovery

# Number of collection workers, so independent sources collect concurrently.
_WORKER_THREADS = 4

# Gateway statuses that mean the control plane is momentarily unreachable (e.g.
# mid-deploy): treat as transient and retry rather than a hard failure.
_GATEWAY_CODES = (502, 503, 504)


class ControlPlaneUnavailable(Exception):
    """The control plane is temporarily unreachable (gateway error / connection
    refused) — most often because it's being updated. Retry later."""


def _is_cp_unavailable(exc: BaseException) -> bool:
    if isinstance(exc, ControlPlaneUnavailable):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _GATEWAY_CODES
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
                            httpx.RemoteProtocolError, httpx.PoolTimeout))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return ""


def _local_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER", "")


def _console_user() -> str:
    """The currently logged-in GUI (Aqua) user, so we can tell an operator which
    account the agent SHOULD run as when it's been mis-installed as root."""
    try:
        import pwd
        st = os.stat("/dev/console")
        return pwd.getpwuid(st.st_uid).pw_name
    except Exception:
        return ""


class Agent:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.log = agent_log.setup_logging(cfg.log_file)
        self._log_environment()
        self.reg = self._load_registration()
        self._last_collect = 0.0
        self._last_heartbeat = 0.0
        # Assigned customer node URL (federated fleets): once set, all signaling +
        # ingest goes here instead of the control plane. Persisted in registration.
        self._node_url: Optional[str] = (self.reg or {}).get("node_url")
        self._last_update_attempt = 0.0
        self._last_telemetry: dict = {}
        # Per-collector advisory notices surfaced to the cloud on heartbeat (e.g.
        # New Outlook is captured via the experimental HxStore decoder). Keyed by
        # source_type; shown on the source in the portal.
        self._collector_notices: dict = {}
        self._agent_key: Optional[bytes] = None
        # Cached filesystem folder index (rebuilt in the background so the portal
        # can navigate the tree instantly instead of scanning per-folder).
        self._fs_index: Optional[dict] = None
        self._fs_index_lock = threading.Lock()
        self._fs_index_file = Path(cfg.data_dir) / "fs_index.json"
        self._rebuild_event = threading.Event()
        self._indexer_started = False
        # Prior endpoint-files backup state ({path: {size, mtime, hash}}) so each
        # run only reads + uploads new or changed files (incremental dedup).
        self._files_state_file = Path(cfg.data_dir) / "files_state.json"
        # Incremental collect state for the message/mail collectors.
        self._imessage_state_file = Path(cfg.data_dir) / "imessage_state.json"
        self._outlook_state_file = Path(cfg.data_dir) / "outlook_local_state.json"
        # Push-model scheduling: the agent owns the cadence for its sources. The
        # per-source last-run timestamps are persisted so a restart doesn't
        # immediately re-collect everything, and the mappings (source + interval +
        # selection) are pulled from the cloud on each heartbeat.
        self._mappings: list = []
        self._collect_state_file = Path(cfg.data_dir) / "collect_state.json"
        self._last_collect_by_source: dict = self._load_collect_state()
        # Collections run on a background worker so heartbeats keep flowing while
        # a (potentially long) collection is in progress.
        self._job_queue: "queue.Queue[dict]" = queue.Queue()
        self._worker_started = False
        self._queued_sources: set = set()
        self._queued_lock = threading.Lock()
        # Serialize persistence of the per-source schedule state so parallel
        # collectors don't clobber the file mid-write.
        self._collect_state_lock = threading.Lock()
        # Guard against repeated tray-preference restart attempts when the agent
        # was launched headless by an external command (no local marker to flip).
        self._tray_launch_mode_warned = False

    @property
    def agent_key(self) -> bytes:
        if self._agent_key is None:
            self._agent_key = load_or_create_key(self.cfg.data_dir)
        return self._agent_key

    # -- registration -------------------------------------------------

    def _log_environment(self) -> None:
        """Log the effective user/home/data paths on startup so 'no logs' / 'no
        data found' issues are self-diagnosing. Warns loudly when run as root,
        which points HOME at /var/root — the agent then can't see the logged-in
        user's Outlook/iMessage data and can't draw a menu-bar icon."""
        user = _local_user()
        home = str(Path.home())
        self.log.info("agent starting — user=%s home=%s data_dir=%s log=%s cloud=%s",
                      user, home, self.cfg.data_dir, self.cfg.log_file,
                      self.cfg.cloud_base_url)
        try:
            is_root = os.geteuid() == 0
        except AttributeError:
            is_root = False
        if is_root:
            console = _console_user() or "<your macOS account>"
            self.log.warning(
                "agent is running as ROOT — HOME=%s. It cannot see the logged-in "
                "user's Outlook/iMessage data (those live under /Users/%s/Library) "
                "and cannot show a menu-bar icon. Reinstall/run the agent as the "
                "logged-in user (do NOT use sudo).", home, console)

    def _load_registration(self) -> Optional[dict]:
        if self.cfg.registration_file.exists():
            return json.loads(self.cfg.registration_file.read_text())
        return None

    @property
    def registered(self) -> bool:
        return bool(self.reg and self.reg.get("agent_token"))

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.reg['agent_token']}"}

    def _base(self) -> str:
        """The API base for signaling/ingest — the assigned node when the fleet
        has pinned this tenant to one, otherwise the control plane."""
        return self._node_url or self.cfg.cloud_base_url

    def _set_node_url(self, url: Optional[str]) -> None:
        url = (url or "").rstrip("/") or None
        if url == self._node_url:
            return
        self._node_url = url
        if self.reg is not None:
            if url:
                self.reg["node_url"] = url
            else:
                self.reg.pop("node_url", None)
        self.log.info("routing to %s", url or self.cfg.cloud_base_url)

    def activate(self, code: str) -> dict:
        body = {
            "linking_code": code,
            "hostname": socket.gethostname(),
            "platform": "macos",
            "version": self.cfg.version,
            "collectors": self._collectors(),
        }
        r = httpx.post(f"{self.cfg.cloud_base_url}/agent/activate", json=body, timeout=30)
        r.raise_for_status()
        self.reg = r.json()
        self.cfg.registration_file.write_text(json.dumps(self.reg))
        self.cfg.registration_file.chmod(0o600)
        self.log.info("Activated as agent %s", self.reg.get("agent_id"))
        try:
            self._escrow_key()
        except Exception as exc:
            self.log.error("escrow failed: %s", exc)
        return self.reg

    def _escrow_key(self) -> None:
        """Wrap the local data key to the vault recovery key and register it so
        client-encrypted content is recoverable if this Mac is lost."""
        pub = self.reg.get("recovery_public_key")
        alg = self.reg.get("recovery_kem_alg")
        if not pub:
            return
        wrapped = wrap_for_recovery(self.agent_key, pub, alg)
        r = httpx.post(f"{self.cfg.cloud_base_url}/agent/register-key",
                       json={"wrapped_key": wrapped}, headers=self._headers(), timeout=30)
        r.raise_for_status()

    # -- telemetry ----------------------------------------------------

    def telemetry(self) -> dict:
        cfg = (self.reg or {}).get("config", {})
        try:
            from cv_crypto.provider import get_provider
            pq = get_provider().pq_available
        except Exception:
            pq = False
        return {
            "hostname": socket.gethostname(),
            "local_ip": _local_ip(),
            "local_user": _local_user(),
            "os": platform.platform(),
            "op_available": onepassword.available(),
            "op_auth": onepassword.auth_state(self.cfg.op_service_account_token),
            "collectors": self._collectors(),
            "collector_notices": self._collector_notices,
            "version": self.cfg.version,
            "cloud_url": self.cfg.cloud_base_url,
            "last_collection_at": (self.reg or {}).get("last_collection_at"),
            "crypto": {
                "client_side_encryption": True,
                "content_alg": "AES-256-GCM",
                "pq_available": pq,
                "recovery_kem_alg": self.reg.get("recovery_kem_alg") if self.reg else None,
                "recovery_escrow": "escrowed" if cfg.get("escrow_wrapped_key") else "pending",
            },
            "recent_logs": agent_log.tail(self.cfg.log_file, 50),
            "reported_at": _now_iso(),
            "fs_index": {
                "built_at": (self._fs_index or {}).get("built_at"),
                "folders": (self._fs_index or {}).get("nodes", 0),
                "building": self._fs_index is None,
            },
        }

    def _write_status(self, extra: dict) -> None:
        status = {"registered": self.registered,
                  "agent_id": (self.reg or {}).get("agent_id"),
                  "cloud": self.cfg.cloud_base_url, "telemetry": self.telemetry(),
                  **extra}
        self.cfg.status_file.write_text(json.dumps(status, indent=2))

    def status_snapshot(self) -> dict:
        """Structured status for the menu-bar UI."""
        return {
            "name": (self.reg or {}).get("name") or socket.gethostname(),
            "agent_id": (self.reg or {}).get("agent_id"),
            "registered": self.registered,
            "cloud_url": self.cfg.cloud_base_url,
            "version": self.cfg.version,
            "last_heartbeat_epoch": self._last_heartbeat,
            "last_collect_epoch": self._last_collect,
            "telemetry": self._last_telemetry or self.telemetry(),
        }

    # -- heartbeat + commands -----------------------------------------

    def heartbeat(self) -> dict:
        # Ensure the background workers are running regardless of entry point
        # (headless run loop or the menu-bar app, which both call this).
        self._start_indexer()
        self._start_worker()
        tel = self.telemetry()
        self._last_telemetry = tel
        body = {"state": "active", "version": self.cfg.version, "telemetry": tel}
        base = self._base()
        try:
            r = httpx.post(f"{base}/agent/heartbeat",
                           json=body, headers=self._headers(), timeout=30)
            r.raise_for_status()
        except Exception:
            # Assigned node unreachable → fall back to the control plane so the
            # agent keeps reporting and can re-discover its node.
            if self._node_url and base == self._node_url:
                self.log.warning("assigned node %s unreachable; falling back to control plane",
                                 self._node_url)
                self._set_node_url(None)
                r = httpx.post(f"{self.cfg.cloud_base_url}/agent/heartbeat",
                               json=body, headers=self._headers(), timeout=30)
                r.raise_for_status()
            else:
                raise
        self._last_heartbeat = time.time()
        data = r.json()
        self.reg["config"] = data.get("config", self.reg.get("config", {}))
        # Cloud-driven log verbosity: explicit log_level wins, else the legacy
        # verbose_logging bool (debug), else info.
        _cfg = self.reg.get("config", {})
        agent_log.set_level(_cfg.get("log_level")
                            or ("debug" if _cfg.get("verbose_logging") else "info"))
        # Menu-bar icon show/hide preference — restart to apply if it changed.
        self._apply_tray_preference()
        # Per-node routing: once the tenant is pinned to a customer node the cloud
        # hands us that node's API base; from then on ALL signaling, commands and
        # ingest go there instead of the control plane.
        self._set_node_url(data.get("node_url") or data.get("ingest_url") or None)
        self.cfg.registration_file.write_text(json.dumps(self.reg))
        # Auto-update: pull a new bundle when the cloud advertises a newer version.
        self._maybe_self_update(data.get("latest_version"))
        # Push-model scheduling: the cloud tells us WHICH sources to collect and
        # HOW OFTEN (from the Data Map); the agent decides WHEN, based on its own
        # timers — so nothing fires when we're offline or the data is unreachable.
        mappings = data.get("mappings")
        if isinstance(mappings, list):
            self._mappings = mappings
        self._run_due_collects()
        # Handle every command the cloud drained this cycle (new agents), falling
        # back to the single `command` field for older payloads.
        commands = data.get("commands")
        handled = 0
        if isinstance(commands, list) and commands:
            for c in commands:
                self._handle_command(c)
            handled = len(commands)
        else:
            command = data.get("command")
            if command:
                self._handle_command(command)
                handled = 1
        # Poll again quickly when we did command work or more is queued, so
        # interactive folder browsing isn't throttled to one folder per cycle.
        self._fast_poll = handled > 0 or bool(data.get("pending_more"))
        return data

    def _apply_tray_preference(self) -> None:
        """When the cloud config toggles the menu-bar icon, sync the local marker
        and restart (launchd KeepAlive relaunches) so the change takes effect.
        Only one restart occurs: the new process reads the marker and matches."""
        mode = getattr(self, "_tray_mode", None)
        if mode is None:
            return
        desired = bool(self.reg.get("config", {}).get("show_tray_icon", True))
        if desired == mode:
            return
        if desired and not getattr(self, "_tray_available", False):
            return  # can't show a tray without rumps — never loop-restart
        marker = self.cfg.data_dir / "no_tray"
        changed = False
        has_marker = marker.exists()
        try:
            if desired:
                # If we're headless with NO local override marker, try to self-heal
                # launchd's entrypoint to agent.menubar, then restart once.
                if not has_marker and mode is False:
                    if self._ensure_menubar_launch_mode():
                        changed = True
                    else:
                        if not self._tray_launch_mode_warned:
                            self._tray_launch_mode_warned = True
                            self.log.warning(
                                "menu-bar icon requested but agent is running headless "
                                "without a local no_tray marker; restart skipped to "
                                "avoid a loop. Ensure launchd runs '-m agent.menubar'.")
                        return
                if has_marker:
                    marker.unlink(missing_ok=False)
                    changed = True
            else:
                if not has_marker:
                    marker.write_text("1")
                    changed = True
        except Exception as exc:
            self.log.warning("menu-bar preference update failed (show=%s): %s",
                             desired, exc)
            return
        if not changed:
            return
        self.log.info("menu-bar icon preference changed (show=%s) — restarting to apply", desired)
        os._exit(0)

    def _ensure_menubar_launch_mode(self) -> bool:
        """Best-effort local repair: if launchd still points at agent.main, rewrite
        ProgramArguments to agent.menubar so show_tray_icon=True can converge.
        Returns True only when a change was written."""
        plist = Path.home() / "Library" / "LaunchAgents" / "com.arkive.agent.plist"
        if not plist.exists():
            return False
        try:
            data = plistlib.loads(plist.read_bytes())
        except Exception as exc:
            self.log.warning("could not read launchd plist for tray recovery: %s", exc)
            return False

        args = data.get("ProgramArguments")
        if not isinstance(args, list) or not args:
            return False
        if "agent.menubar" in args:
            return False

        updated = False
        for i, token in enumerate(args):
            if token == "agent.main":
                args[i] = "agent.menubar"
                updated = True

        if not updated:
            for i in range(len(args) - 1):
                if args[i] == "-m" and args[i + 1].startswith("agent."):
                    args[i + 1] = "agent.menubar"
                    updated = True
                    break

        if not updated:
            return False

        data["ProgramArguments"] = args
        try:
            plist.write_bytes(plistlib.dumps(data))
            self.log.info("updated launchd ProgramArguments to agent.menubar in %s", plist)
            return True
        except Exception as exc:
            self.log.warning("could not update launchd plist for tray recovery: %s", exc)
            return False

    def _maybe_self_update(self, latest: Optional[str]) -> None:
        if not latest or latest == self.cfg.version:
            return
        # Throttle so a failing update doesn't loop every heartbeat.
        if time.time() - self._last_update_attempt < 300:
            return
        # Persisted guard: if we already tried to reach this exact target recently
        # but the installed VERSION still hasn't changed, don't keep restarting
        # (a broken update.sh would otherwise loop restart→update→restart forever).
        marker = Path(self.cfg.data_dir) / "update_attempt.json"
        try:
            prev = json.loads(marker.read_text())
            if prev.get("target") == latest and time.time() - float(prev.get("at", 0)) < 1800:
                self.log.warning("skipping self-update to %s: already attempted "
                                 "recently but VERSION is still %s (update may be "
                                 "failing — check update.log)", latest, self.cfg.version)
                return
        except Exception:
            pass
        self._last_update_attempt = time.time()
        try:
            marker.write_text(json.dumps({"target": latest, "at": time.time()}))
        except Exception:
            pass
        self.log.info("new agent version available (%s -> %s); self-updating",
                      self.cfg.version, latest)
        self.self_update()

    def _handle_command(self, command: dict) -> None:
        ctype = command.get("type")
        params = command.get("params") or {}
        self.log.info("command received: %s", ctype)
        # Long-running work is dispatched to the background worker so the
        # heartbeat/command channel never blocks (no deadlock during collection).
        if ctype == "collect":
            self._enqueue_collect(params)
            self._post_command_result("collect", True, {"queued": True})
            return
        if ctype == "scan_fs":
            self._report_fs_scan(params)
            return
        ok, detail = True, {}
        try:
            if ctype == "update":
                self.self_update()
                detail = {"updating": True}
            elif ctype == "reconfigure":
                detail = {"config": self.reg.get("config")}
            elif ctype == "quarantine":
                detail = {"quarantined": True}
        except Exception as exc:
            ok, detail = False, {"error": str(exc)}
        self._post_command_result(ctype, ok, detail)

    def _post_command_result(self, ctype: str, ok: bool, detail: dict) -> None:
        try:
            r = httpx.post(f"{self._base()}/agent/command-result",
                           json={"type": ctype, "ok": ok, "detail": detail},
                           headers=self._headers(), timeout=30)
            r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            if _is_cp_unavailable(exc):
                self.log.warning("command result upload deferred for %s: control plane unavailable",
                                 ctype)
            else:
                self.log.warning("command result upload failed for %s: %s", ctype, exc)

    def _report_fs_scan(self, params: dict) -> None:
        """Serve the folder tree from the cached index (fast) and, if asked,
        signal a background rebuild. Never scans inline so the heartbeat can't
        block on a slow filesystem walk. A specific ``path`` means a lazy
        expansion request — scan just that folder's immediate children."""
        request_id = params.get("request_id", "")
        path = (params.get("path") or "").strip()
        if path:
            self._report_fs_expand(path, request_id)
            return
        if params.get("rebuild"):
            self._rebuild_event.set()
        index = self._current_index()
        self._post_fs_index(index, request_id=request_id)

    def _report_fs_expand(self, path: str, request_id: str) -> None:
        """Scan one folder's immediate children on demand (fast) so the portal
        can expand the tree lazily beyond the pre-built index's bounds."""
        ok, err = True, None
        try:
            s = files_collector.scan(path, max_entries=800)
            children = [{"path": d["path"], "name": d["name"], "files": 0,
                         "children": [], "hasMore": bool(d.get("hasChildren"))}
                        for d in s.get("dirs", [])]
            result = {"path": path, "children": children,
                      "files": s.get("files", 0), "bytes": s.get("bytes", 0)}
        except Exception as exc:
            result, ok, err = {"path": path, "children": []}, False, str(exc)
        try:
            httpx.post(f"{self._base()}/agent/fs-expand-result", json={
                "request_id": request_id, "path": path, "ok": ok, "error": err,
                "result": result,
            }, headers=self._headers(), timeout=30)
        except Exception as exc:
            self.log.error("fs-expand report failed: %s", exc)

    def _post_fs_index(self, index: dict, request_id: str = "auto-index",
                       ok: bool = True, err: Optional[str] = None) -> None:
        try:
            httpx.post(f"{self._base()}/agent/fs-scan-result", json={
                "request_id": request_id, "ok": ok, "error": err, "result": index,
            }, headers=self._headers(), timeout=60)
            self.log.info("fs-index reported (%d folders, req=%s)",
                          index.get("nodes", 0), request_id)
        except Exception as exc:
            self.log.error("fs-index report failed: %s", exc)

    # -- filesystem index (background) --------------------------------

    def _current_index(self) -> dict:
        """Return the in-memory index, falling back to the on-disk cache; if
        neither exists yet, trigger a build and return an empty placeholder."""
        with self._fs_index_lock:
            if self._fs_index is not None:
                return self._fs_index
        try:
            if self._fs_index_file.exists():
                idx = json.loads(self._fs_index_file.read_text())
                with self._fs_index_lock:
                    self._fs_index = idx
                return idx
        except Exception:
            pass
        self._rebuild_event.set()
        return {"roots": [], "built_at": None, "nodes": 0, "building": True}

    def _rebuild_index(self) -> None:
        idx = files_collector.build_index()
        with self._fs_index_lock:
            self._fs_index = idx
        try:
            self._fs_index_file.write_text(json.dumps(idx))
        except Exception as exc:
            self.log.warning("could not cache fs index: %s", exc)
        self.log.info("filesystem index built (%d folders)", idx.get("nodes", 0))
        # Push the fresh tree so the portal always has a current view.
        if self.registered:
            self._post_fs_index(idx, request_id="auto-index")

    def _start_indexer(self) -> None:
        if self._indexer_started:
            return
        self._indexer_started = True
        self.log.info("starting background filesystem indexer")
        threading.Thread(target=self._indexer_loop, name="fs-indexer", daemon=True).start()

    def _indexer_loop(self) -> None:
        interval = int(self.reg.get("config", {}).get("index_interval_seconds", 900)) if self.reg else 900
        while True:
            try:
                self.log.info("building filesystem index…")
                self._rebuild_index()
            except Exception as exc:
                self.log.error("index rebuild failed: %s", exc)
            # Rebuild every interval, or sooner if a rebuild was requested.
            self._rebuild_event.wait(timeout=max(60, interval))
            self._rebuild_event.clear()

    # -- background collection worker ---------------------------------

    def _start_worker(self) -> None:
        if self._worker_started:
            return
        self._worker_started = True
        # A small pool of workers so independent sources (1Password, files,
        # iMessage, Outlook…) collect in parallel instead of queueing behind one
        # another. Same-source runs are still de-duplicated by _queued_sources.
        self.log.info("starting %d background collection workers", _WORKER_THREADS)
        for i in range(_WORKER_THREADS):
            threading.Thread(target=self._worker_loop, name=f"collector-{i}",
                             daemon=True).start()

    def _enqueue_collect(self, params: Optional[dict]) -> bool:
        """Queue a collection to run on the worker. De-duplicates by source so a
        backlog of identical requests doesn't pile up. Returns True if queued."""
        params = params or {}
        source = params.get("source_type") or "default"
        with self._queued_lock:
            if source in self._queued_sources:
                self.log.info("collect for %s already queued/running — skipping", source)
                return False
            self._queued_sources.add(source)
        self._job_queue.put(params)
        self.log.info("collect queued for %s (queue depth ~%d)", source, self._job_queue.qsize())
        return True

    def _worker_loop(self) -> None:
        while True:
            params = self._job_queue.get()
            source = params.get("source_type") or "default"
            started = time.time()
            deferred = False
            self.log.info("collection starting: %s", source)
            try:
                detail = self.collect_and_push(params)
                elapsed = time.time() - started
                self.log.info("collection finished: %s in %.1fs (%s object(s))",
                              source, elapsed, detail.get("objects", "?"))
                self._post_command_result("collect", True, detail)
            except ControlPlaneUnavailable as exc:
                deferred = True
                self.log.info("collection deferred: %s — control plane unavailable (%s); "
                              "will retry shortly (update in progress?)", source, exc)
            except Exception as exc:
                self.log.error("collection failed: %s (%s)", source, exc, exc_info=True)
                self._post_command_result("collect", False, {"error": str(exc)})
            finally:
                # Advance the (persisted) per-source schedule timer whether or not
                # the run succeeded, so a failing collect can't re-trigger every
                # heartbeat and a restart doesn't immediately re-collect — EXCEPT on
                # a transient control-plane outage, which should retry promptly.
                now = time.time()
                with self._collect_state_lock:
                    self._last_collect = now
                    if not deferred:
                        self._last_collect_by_source[source] = now
                        self._save_collect_state()
                with self._queued_lock:
                    self._queued_sources.discard(source)
                self._job_queue.task_done()

    def _run_due_collects(self) -> None:
        """Enqueue a collect for each mapping whose cadence is due. This is the
        push model: the agent (which knows it's online) drives the schedule from
        the mapping config it pulled on heartbeat — the cloud never queues these."""
        now = time.time()
        for m in self._mappings or []:
            source = m.get("source_type")
            if not source:
                continue
            interval_min = int(m.get("interval_minutes") or 0)
            if interval_min <= 0:
                continue  # manual only / disabled
            last = float(self._last_collect_by_source.get(source, 0))
            if now - last < interval_min * 60:
                continue
            self._enqueue_collect({"source_type": source,
                                   "file_config": m.get("file_config") or {}})

    def sync_now(self) -> int:
        """Manually collect every configured source now (menu-bar 'Sync now')."""
        n = 0
        for m in self._mappings or []:
            if self._enqueue_collect({"source_type": m.get("source_type"),
                                      "file_config": m.get("file_config") or {}}):
                n += 1
        if not n and self._enqueue_collect(None):  # no mappings yet → legacy default
            n = 1
        return n

    def _load_collect_state(self) -> dict:
        try:
            return json.loads(self._collect_state_file.read_text())
        except Exception:
            return {}

    def _save_collect_state(self) -> None:
        try:
            self._collect_state_file.write_text(json.dumps(self._last_collect_by_source))
        except Exception as exc:
            self.log.warning("could not persist collect state: %s", exc)

    # -- collection + push --------------------------------------------

    def collect_and_push(self, params: Optional[dict] = None) -> dict:
        """Run a collection. When the collect command targets endpoint files
        (carrying the Data Map's file selection), run the file collector; else run
        the configured collectors (1Password)."""
        params = params or {}
        st = params.get("source_type")
        if st == "imessage":
            return self._collect_imessage(params)
        if st == "outlook_local":
            return self._collect_outlook_local(params)
        if st == "endpoint_files" or params.get("file_config"):
            return self._collect_files(params.get("file_config") or {})
        return self._collect_onepassword()

    def _collectors(self) -> list:
        """Collector source-types this agent can serve (advertised on activate +
        heartbeat so the Data Map only offers what's actually present)."""
        cols = ["onepassword", "endpoint_files"]
        for name, mod in (("imessage", imessage), ("outlook_local", outlook_local)):
            try:
                if mod.available():
                    cols.append(name)
            except Exception:
                pass
        return cols

    def _push_objects(self, source_type: str, objects: list, destinations: list,
                      max_batch_bytes: int = 32 * 1024 * 1024, max_batch: int = 500) -> int:
        """Client-encrypt each object and push in batches bounded by cumulative
        size (files can be large) so no single request is oversized. A batch that
        fails with a PERSISTENT error (a single oversized/malformed object) is
        isolated object-by-object so one bad item never blocks the rest — the good
        objects still land and the bad ones are recorded for retry. A transient
        control-plane outage still defers the whole collect (re-raised)."""
        pushed = 0
        self._push_delivered: set = set()
        self._push_failed: set = set()
        batch: list = []
        batch_bytes = 0

        def _flush(b: list) -> None:
            nonlocal pushed
            if not b:
                return
            try:
                n = self._push_batch(source_type, b, destinations)
                pushed += n
                self._push_delivered.update(o["object_id"] for o in b)
            except ControlPlaneUnavailable:
                raise  # transient — defer the whole collect, retry next cycle
            except httpx.HTTPStatusError as exc:
                # The server RESPONDED with an error status -> a specific object is
                # bad (oversized/malformed). Isolate object-by-object so the rest
                # still land and only the offending item is recorded for retry.
                # (Connection/timeout errors are NOT caught here — they propagate
                # and defer the whole collect, so an outage never drops good data.)
                code = exc.response.status_code if exc.response is not None else "?"
                if len(b) == 1:
                    self._push_failed.add(b[0]["object_id"])
                    self.log.error("push: dropping object %s (%s) rejected by server (HTTP %s): %s",
                                   b[0].get("object_id"), source_type, code, str(exc)[:200])
                    return
                self.log.warning("push: batch of %d rejected (HTTP %s) — isolating the bad "
                                 "object so the rest still land", len(b), code)
                for o in b:
                    _flush([o])

        for o in objects:
            plaintext = base64.b64decode(o["content_b64"])
            o.setdefault("content_hash", hashlib.sha256(plaintext).hexdigest())
            envelope = encrypt_content(self.agent_key, plaintext, o["object_id"])
            o["content_b64"] = base64.b64encode(envelope).decode()
            o["client_encrypted"] = True
            o["size_bytes"] = len(envelope)
            if batch and (batch_bytes + len(envelope) > max_batch_bytes or len(batch) >= max_batch):
                _flush(batch)
                batch, batch_bytes = [], 0
            batch.append(o)
            batch_bytes += len(envelope)
        _flush(batch)
        if self._push_failed:
            self.log.warning("push: %d/%d object(s) could not be delivered (%s) — kept for retry",
                             len(self._push_failed), len(objects), source_type)
        return pushed

    def _push_batch(self, source_type: str, batch: list, destinations: list) -> int:
        base = self._base()
        payload = {"source_type": source_type, "destinations": destinations, "objects": batch}
        last = None
        # Retry briefly on a gateway error (control plane mid-deploy); if it's
        # still down, raise a transient error so the collect defers to next cycle
        # without advancing its cursor (no data loss, no scary traceback).
        for attempt in range(3):
            try:
                r = httpx.post(f"{base}/agent/ingest", json=payload,
                               headers=self._headers(), timeout=180)
                if r.status_code in _GATEWAY_CODES:
                    raise ControlPlaneUnavailable(f"HTTP {r.status_code}")
                r.raise_for_status()
                self.log.info("pushed batch of %d (%s) -> %s snapshot %s", len(batch), source_type,
                              base, r.json().get("snapshot_id", "?"))
                return len(batch)
            except Exception as exc:
                if not _is_cp_unavailable(exc):
                    raise
                last = exc
                if attempt < 2:
                    wait = 5 * (attempt + 1)
                    self.log.info("control plane unavailable (%s) — retry %d/3 in %ds "
                                  "(update in progress?)", exc, attempt + 1, wait)
                    time.sleep(wait)
        raise ControlPlaneUnavailable(str(last))

    def _collect_files(self, file_config: dict) -> dict:
        destinations = self.reg.get("config", {}).get("destinations", ["cv-cloud"])
        roots = file_config.get("roots") or []
        if not roots:
            self.log.info("endpoint_files: no folders selected — nothing to collect")
            self._write_status({"last_collect": _now_iso(), "results": [{"collector": "endpoint_files", "objects": 0}]})
            return {"objects": 0, "results": [{"collector": "endpoint_files", "skipped": "no folders selected"}]}
        self.log.info("endpoint_files: scanning %d root(s)", len(roots))
        known = self._load_files_state()
        objects, new_state, unchanged = files_collector.collect(file_config, known)
        total = self._push_objects("endpoint_files", objects, destinations) if objects else 0
        # Persist state only when everything delivered (files state is path-keyed).
        # Isolation still lands every good file this run; a run with a failing file
        # re-collects + re-pushes next cycle (server dedups) until it clears.
        if not objects or not getattr(self, "_push_failed", set()):
            self._save_files_state(new_state)
        self._last_collect = time.time()
        results = [{"collector": "endpoint_files", "objects": total, "unchanged": unchanged}]
        self._write_status({"last_collect": _now_iso(), "results": results})
        self.log.info("endpoint_files: pushed %d new/changed file(s), %d unchanged (deduped)",
                      total, unchanged)
        return {"objects": total, "unchanged": unchanged, "results": results}

    def _load_files_state(self) -> dict:
        try:
            return json.loads(self._files_state_file.read_text())
        except Exception:
            return {}

    def _save_files_state(self, state: dict) -> None:
        try:
            self._files_state_file.write_text(json.dumps(state))
        except Exception as exc:
            self.log.warning("could not persist endpoint_files state: %s", exc)

    def _collect_imessage(self, params: dict) -> dict:
        destinations = self.reg.get("config", {}).get("destinations", ["cv-cloud"])
        self.log.info("imessage: starting collection → destinations=%s", destinations)
        if not imessage.available():
            if imessage._CHAT_DB.exists():
                # The file is there but macOS TCC blocks the read: Full Disk
                # Access must be granted to THIS exact binary, not Terminal/etc.
                self.log.warning("imessage: %s exists but is unreadable (macOS Full Disk "
                                 "Access). Grant FDA to this binary and restart the agent: %s "
                                 "(System Settings › Privacy & Security › Full Disk Access)",
                                 imessage._CHAT_DB, sys.executable)
                reason = "no Full Disk Access"
            else:
                self.log.warning("imessage: no Messages database at %s (Messages app never "
                                 "used on this Mac?)", imessage._CHAT_DB)
                reason = "no Messages DB"
            return {"objects": 0, "results": [{"collector": "imessage", "skipped": reason}]}
        state = self._load_json_state(self._imessage_state_file)
        cfg = params.get("file_config") or {}
        self.log.info("imessage: draining new messages since ROWID %s…", state.get("last_rowid", 0))
        # Drain the backlog in bounded, resumable chunks: push each chunk, persist
        # its cursor, then continue — so a huge history captures over time and an
        # interruption resumes instead of re-scanning the same messages forever.
        # Bounded per invocation so it never blocks the other collectors.
        total = 0
        chunks = 0
        deadline = time.time() + 20 * 60
        while time.time() < deadline:
            prev_rowid = int(state.get("last_rowid") or 0)
            objects, new_state = imessage.collect(cfg, state)
            pushed = self._push_objects("imessage", objects, destinations) if objects else 0
            total += pushed
            dropped = len(getattr(self, "_push_failed", set()))
            if objects and dropped:
                self.log.warning("imessage: %d message(s) in this chunk could not be delivered "
                                 "and were logged+skipped so the backlog isn't stalled", dropped)
            new_rowid = int(new_state.get("last_rowid") or prev_rowid)
            if new_rowid > prev_rowid:
                # Persist the advanced cursor per chunk so progress is durable.
                state = {"last_rowid": new_rowid}
                self._save_json_state(self._imessage_state_file, state)
                chunks += 1
                self.log.info("imessage: chunk %d done — %d object(s), through ROWID %d",
                              chunks, pushed, new_rowid)
            # Stop when the source is exhausted or the cursor didn't advance
            # (safety against an unexpected non-advancing chunk).
            if not new_state.get("has_more") or new_rowid <= prev_rowid:
                break
        self._last_collect = time.time()
        results = [{"collector": "imessage", "objects": total}]
        self._write_status({"last_collect": _now_iso(), "results": results})
        self.log.info("imessage: pushed %d object(s) across %d chunk(s) (through ROWID %s)",
                      total, chunks, state.get("last_rowid", "?"))
        return {"objects": total, "results": results}

    def _collect_outlook_local(self, params: dict) -> dict:
        destinations = self.reg.get("config", {}).get("destinations", ["cv-cloud"])
        self.log.info("outlook_local: starting collection → destinations=%s", destinations)
        if not outlook_local.available():
            self.log.warning(
                "outlook_local: no Outlook data found for user %s (home %s). Checked "
                "legacy profiles under %s and New Outlook HxStore under %s. If your mail "
                "is elsewhere, the agent is likely running as the wrong user (e.g. root).",
                _local_user(), Path.home(), outlook_local._GROUP,
                " , ".join(str(r) for r in outlook_local._HXSTORE_ROOTS))
            return {"objects": 0, "results": [{"collector": "outlook_local", "skipped": "no Outlook data"}]}
        state = self._load_json_state(self._outlook_state_file)
        self.log.info("outlook_local: reading local profiles…")
        objects, new_state = outlook_local.collect(params.get("file_config") or {}, state)
        self.log.info("outlook_local: collected %d object(s); encrypting + pushing…", len(objects))
        # Surface the capture mode (legacy vs experimental HxStore) to the cloud so
        # the source can show a notice. Reported on the next heartbeat.
        capture = (new_state or {}).get("capture") or {}
        if capture.get("mode") == "experimental-hxstore":
            self._collector_notices["outlook_local"] = {
                "level": "experimental",
                "mode": capture.get("mode"),
                "message": capture.get("notice") or "New Outlook captured via experimental decoder.",
            }
        else:
            self._collector_notices.pop("outlook_local", None)
        total = self._push_objects("outlook_local", objects, destinations) if objects else 0
        if objects:
            # Advance state for delivered objects; drop the few that persistently
            # failed so they're retried next run (never blocks the rest).
            for fid in getattr(self, "_push_failed", set()):
                (new_state.get("sigs") or {}).pop(fid, None)
            self._save_json_state(self._outlook_state_file, new_state)
        self._last_collect = time.time()
        results = [{"collector": "outlook_local", "objects": total}]
        self._write_status({"last_collect": _now_iso(), "results": results})
        self.log.info("outlook_local: pushed %d/%d object(s)", total, len(objects))
        return {"objects": total, "results": results}

    def _load_json_state(self, path: Path) -> dict:
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}

    def _save_json_state(self, path: Path, state: dict) -> None:
        try:
            path.write_text(json.dumps(state))
        except Exception as exc:
            self.log.warning("could not persist %s: %s", path.name, exc)


    def _collect_onepassword(self) -> dict:
        cfg = self.reg.get("config", {})
        collectors = cfg.get("collectors", ["onepassword"])
        destinations = cfg.get("destinations", ["cv-cloud"])
        total = 0
        results = []
        for name in collectors:
            if name != "onepassword":
                continue
            if not onepassword.available():
                self.log.warning("collector %s: op CLI not installed", name)
                results.append({"collector": name, "error": "op CLI not installed"})
                continue
            try:
                objects = onepassword.collect(self.cfg.op_service_account_token)
            except Exception as exc:
                msg = str(exc).lower()
                if "not signed in" in msg or "not authenticated" in msg or "no account" in msg:
                    self.log.info("collector %s skipped: 1Password not signed in "
                                  "(unlock the app + enable CLI integration, or add a token)", name)
                    results.append({"collector": name, "skipped": "not signed in"})
                else:
                    self.log.error("collector %s failed: %s", name, exc)
                    results.append({"collector": name, "error": str(exc)})
                continue
            if not objects:
                results.append({"collector": name, "objects": 0})
                continue
            self.log.info("collector %s: collected %d items from 1Password", name, len(objects))
            total += self._push_objects("onepassword", objects, destinations)
            results.append({"collector": name, "objects": len(objects)})
        self._last_collect = time.time()
        self._write_status({"last_collect": _now_iso(), "results": results})
        self.log.info("pushed %d objects", total)
        return {"objects": total, "results": results}

    # -- self update --------------------------------------------------

    def self_update(self) -> None:
        script = Path(self.cfg.home) / "desktop-agent" / "update.sh"
        if not script.exists():
            script = Path(self.cfg.home) / "update.sh"
        if script.exists():
            self.log.info("self-update: launching %s", script)
            # Detach into its own session so restarting this agent (which the
            # updater does) doesn't kill the update mid-flight.
            subprocess.Popen(["bash", str(script)], start_new_session=True)
        else:
            self.log.warning("no update script found")

    # -- run loop -----------------------------------------------------

    def run(self) -> None:
        if not self.registered:
            if self.cfg.linking_code:
                self.activate(self.cfg.linking_code)
            else:
                self.log.warning("Not registered. Run: arkive-agent link <CODE>")
                return
        self._start_indexer()  # background folder-index builder
        self._start_worker()   # background collection worker
        interval = self.reg.get("heartbeat_interval_seconds", 30)
        idle = min(int(interval or 30), 15)  # cap idle poll so first command lands sooner
        while True:
            try:
                self.heartbeat()  # heartbeat pulls mappings + runs due collects
            except Exception as exc:
                if _is_cp_unavailable(exc):
                    self.log.info("control plane unavailable (update in progress?) — "
                                  "will retry: %s", exc)
                else:
                    self.log.error("loop error: %s", exc)
                    self._write_status({"error": str(exc)})
                self._fast_poll = False
            time.sleep(2 if getattr(self, "_fast_poll", False) else idle)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="arkive-agent")
    sub = parser.add_subparsers(dest="cmd")
    p_link = sub.add_parser("link"); p_link.add_argument("code")
    sub.add_parser("run")
    sub.add_parser("collect")
    sub.add_parser("status")
    args = parser.parse_args(argv)

    cfg = Config()
    agent = Agent(cfg)

    if args.cmd == "link":
        agent.activate(args.code)
    elif args.cmd == "collect":
        if not agent.registered:
            sys.exit("not registered; run: arkive-agent link <CODE>")
        print(json.dumps(agent.collect_and_push(), indent=2))
    elif args.cmd == "status":
        agent._write_status({})
        print(cfg.status_file.read_text())
    elif args.cmd == "run":
        agent.run()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
