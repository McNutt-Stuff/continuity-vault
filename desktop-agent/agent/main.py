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
import json
import os
import platform
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
from . import agent_log
from .crypto import encrypt_content, load_or_create_key, wrap_for_recovery


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


class Agent:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.log = agent_log.setup_logging(cfg.log_file)
        self.reg = self._load_registration()
        self._last_collect = 0.0
        self._last_heartbeat = 0.0
        self._last_update_attempt = 0.0
        self._last_telemetry: dict = {}
        self._agent_key: Optional[bytes] = None
        # Cached filesystem folder index (rebuilt in the background so the portal
        # can navigate the tree instantly instead of scanning per-folder).
        self._fs_index: Optional[dict] = None
        self._fs_index_lock = threading.Lock()
        self._fs_index_file = Path(cfg.data_dir) / "fs_index.json"
        self._rebuild_event = threading.Event()
        self._indexer_started = False

    @property
    def agent_key(self) -> bytes:
        if self._agent_key is None:
            self._agent_key = load_or_create_key(self.cfg.data_dir)
        return self._agent_key

    # -- registration -------------------------------------------------

    def _load_registration(self) -> Optional[dict]:
        if self.cfg.registration_file.exists():
            return json.loads(self.cfg.registration_file.read_text())
        return None

    @property
    def registered(self) -> bool:
        return bool(self.reg and self.reg.get("agent_token"))

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.reg['agent_token']}"}

    def activate(self, code: str) -> dict:
        body = {
            "linking_code": code,
            "hostname": socket.gethostname(),
            "platform": "macos",
            "version": self.cfg.version,
            "collectors": ["onepassword", "endpoint_files"],
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
        httpx.post(f"{self.cfg.cloud_base_url}/agent/register-key",
                   json={"wrapped_key": wrapped}, headers=self._headers(), timeout=30)

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
            "collectors": ["onepassword", "endpoint_files"],
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
        tel = self.telemetry()
        self._last_telemetry = tel
        body = {"state": "active", "version": self.cfg.version, "telemetry": tel}
        r = httpx.post(f"{self.cfg.cloud_base_url}/agent/heartbeat",
                       json=body, headers=self._headers(), timeout=30)
        r.raise_for_status()
        self._last_heartbeat = time.time()
        data = r.json()
        self.reg["config"] = data.get("config", self.reg.get("config", {}))
        # Cloud-driven advanced setting: verbose (DEBUG) logging.
        agent_log.set_verbose(bool(self.reg.get("config", {}).get("verbose_logging")))
        self.cfg.registration_file.write_text(json.dumps(self.reg))
        # Auto-update: pull a new bundle when the cloud advertises a newer version.
        self._maybe_self_update(data.get("latest_version"))
        command = data.get("command")
        if command:
            self._handle_command(command)
        return data

    def _maybe_self_update(self, latest: Optional[str]) -> None:
        if not latest or latest == self.cfg.version:
            return
        # Throttle so a failing update doesn't loop every heartbeat.
        if time.time() - self._last_update_attempt < 300:
            return
        self._last_update_attempt = time.time()
        self.log.info("new agent version available (%s -> %s); self-updating",
                      self.cfg.version, latest)
        self.self_update()

    def _handle_command(self, command: dict) -> None:
        ctype = command.get("type")
        params = command.get("params") or {}
        self.log.info("command received: %s", ctype)
        ok, detail = True, {}
        try:
            if ctype == "collect":
                detail = self.collect_and_push(params)
            elif ctype == "scan_fs":
                # Report the filesystem tree for a path so the operator can pick
                # folders in the portal. Reported on a dedicated endpoint.
                self._report_fs_scan(params)
                return
            elif ctype == "update":
                self.self_update()
                detail = {"updating": True}
            elif ctype == "reconfigure":
                detail = {"config": self.reg.get("config")}
            elif ctype == "quarantine":
                detail = {"quarantined": True}
        except Exception as exc:
            ok, detail = False, {"error": str(exc)}
        try:
            httpx.post(f"{self.cfg.cloud_base_url}/agent/command-result",
                       json={"type": ctype, "ok": ok, "detail": detail},
                       headers=self._headers(), timeout=30)
        except Exception:
            pass

    def _report_fs_scan(self, params: dict) -> None:
        """Serve the folder tree from the cached index (fast) and, if asked,
        signal a background rebuild. Never scans inline so the heartbeat can't
        block on a slow filesystem walk."""
        request_id = params.get("request_id", "")
        if params.get("rebuild"):
            self._rebuild_event.set()
        index = self._current_index()
        self._post_fs_index(index, request_id=request_id)

    def _post_fs_index(self, index: dict, request_id: str = "auto-index",
                       ok: bool = True, err: Optional[str] = None) -> None:
        try:
            httpx.post(f"{self.cfg.cloud_base_url}/agent/fs-scan-result", json={
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
        threading.Thread(target=self._indexer_loop, name="fs-indexer", daemon=True).start()

    def _indexer_loop(self) -> None:
        interval = int(self.reg.get("config", {}).get("index_interval_seconds", 900)) if self.reg else 900
        while True:
            try:
                self._rebuild_index()
            except Exception as exc:
                self.log.error("index rebuild failed: %s", exc)
            # Rebuild every interval, or sooner if a rebuild was requested.
            self._rebuild_event.wait(timeout=max(60, interval))
            self._rebuild_event.clear()

    # -- collection + push --------------------------------------------

    def collect_and_push(self, params: Optional[dict] = None) -> dict:
        """Run a collection. When the collect command targets endpoint files
        (carrying the Data Map's file selection), run the file collector; else run
        the configured collectors (1Password)."""
        params = params or {}
        if params.get("source_type") == "endpoint_files" or params.get("file_config"):
            return self._collect_files(params.get("file_config") or {})
        return self._collect_onepassword()

    def _push_objects(self, source_type: str, objects: list, destinations: list,
                      max_batch_bytes: int = 32 * 1024 * 1024, max_batch: int = 500) -> int:
        """Client-encrypt each object and push in batches bounded by cumulative
        size (files can be large) so no single request is oversized."""
        pushed = 0
        batch: list = []
        batch_bytes = 0
        for o in objects:
            plaintext = base64.b64decode(o["content_b64"])
            envelope = encrypt_content(self.agent_key, plaintext, o["object_id"])
            o["content_b64"] = base64.b64encode(envelope).decode()
            o["client_encrypted"] = True
            o["size_bytes"] = len(envelope)
            if batch and (batch_bytes + len(envelope) > max_batch_bytes or len(batch) >= max_batch):
                pushed += self._push_batch(source_type, batch, destinations)
                batch, batch_bytes = [], 0
            batch.append(o)
            batch_bytes += len(envelope)
        if batch:
            pushed += self._push_batch(source_type, batch, destinations)
        return pushed

    def _push_batch(self, source_type: str, batch: list, destinations: list) -> int:
        r = httpx.post(f"{self.cfg.cloud_base_url}/agent/ingest", json={
            "source_type": source_type,
            "destinations": destinations,
            "objects": batch,
        }, headers=self._headers(), timeout=180)
        r.raise_for_status()
        self.log.info("pushed batch of %d (%s) -> snapshot %s", len(batch), source_type,
                      r.json().get("snapshot_id", "?"))
        return len(batch)

    def _collect_files(self, file_config: dict) -> dict:
        destinations = self.reg.get("config", {}).get("destinations", ["cv-cloud"])
        roots = file_config.get("roots") or []
        if not roots:
            self.log.info("endpoint_files: no folders selected — nothing to collect")
            self._write_status({"last_collect": _now_iso(), "results": [{"collector": "endpoint_files", "objects": 0}]})
            return {"objects": 0, "results": [{"collector": "endpoint_files", "skipped": "no folders selected"}]}
        self.log.info("endpoint_files: scanning %d root(s)", len(roots))
        objects = files_collector.collect(file_config)
        total = self._push_objects("endpoint_files", objects, destinations) if objects else 0
        self._last_collect = time.time()
        self._write_status({"last_collect": _now_iso(),
                            "results": [{"collector": "endpoint_files", "objects": total}]})
        self.log.info("endpoint_files: pushed %d files", total)
        return {"objects": total, "results": [{"collector": "endpoint_files", "objects": total}]}

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
        interval = self.reg.get("heartbeat_interval_seconds", 30)
        while True:
            try:
                self.heartbeat()
                schedule_min = self.reg.get("config", {}).get("schedule_minutes", 360)
                if time.time() - self._last_collect >= schedule_min * 60:
                    self.collect_and_push()
            except Exception as exc:
                self.log.error("loop error: %s", exc)
                self._write_status({"error": str(exc)})
            time.sleep(interval)


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
