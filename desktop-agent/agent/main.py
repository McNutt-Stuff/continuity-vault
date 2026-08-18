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
import json
import platform
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from .config import Config
from .collectors import onepassword
from .crypto import encrypt_content, load_or_create_key, wrap_for_recovery


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Agent:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.reg = self._load_registration()
        self._last_collect = 0.0
        self._agent_key: Optional[bytes] = None

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
            "collectors": ["onepassword"],
        }
        r = httpx.post(f"{self.cfg.cloud_base_url}/agent/activate", json=body, timeout=30)
        r.raise_for_status()
        self.reg = r.json()
        self.cfg.registration_file.write_text(json.dumps(self.reg))
        self.cfg.registration_file.chmod(0o600)
        print(f"Activated as agent {self.reg['agent_id']}")
        try:
            self._escrow_key()
        except Exception as exc:
            print(f"[escrow] {exc}")
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
        return {
            "hostname": socket.gethostname(),
            "os": platform.platform(),
            "op_available": onepassword.available(),
            "version": self.cfg.version,
            "last_collection_at": (self.reg or {}).get("last_collection_at"),
            "reported_at": _now_iso(),
        }

    def _write_status(self, extra: dict) -> None:
        status = {"registered": self.registered,
                  "agent_id": (self.reg or {}).get("agent_id"),
                  "cloud": self.cfg.cloud_base_url, "telemetry": self.telemetry(),
                  **extra}
        self.cfg.status_file.write_text(json.dumps(status, indent=2))

    # -- heartbeat + commands -----------------------------------------

    def heartbeat(self) -> dict:
        body = {"state": "active", "version": self.cfg.version,
                "telemetry": self.telemetry()}
        r = httpx.post(f"{self.cfg.cloud_base_url}/agent/heartbeat",
                       json=body, headers=self._headers(), timeout=30)
        r.raise_for_status()
        data = r.json()
        self.reg["config"] = data.get("config", self.reg.get("config", {}))
        self.cfg.registration_file.write_text(json.dumps(self.reg))
        command = data.get("command")
        if command:
            self._handle_command(command)
        return data

    def _handle_command(self, command: dict) -> None:
        ctype = command.get("type")
        print(f"[command] {ctype}")
        ok, detail = True, {}
        try:
            if ctype == "collect":
                detail = self.collect_and_push()
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

    # -- collection + push --------------------------------------------

    def collect_and_push(self) -> dict:
        cfg = self.reg.get("config", {})
        collectors = cfg.get("collectors", ["onepassword"])
        destinations = cfg.get("destinations", ["cv-cloud"])
        total = 0
        results = []
        for name in collectors:
            if name != "onepassword":
                continue
            if not onepassword.available():
                results.append({"collector": name, "error": "op CLI not installed"})
                continue
            objects = onepassword.collect(self.cfg.op_service_account_token)
            if not objects:
                results.append({"collector": name, "objects": 0})
                continue
            # Client-side encryption: encrypt each object locally so the cloud
            # never receives plaintext secrets.
            for o in objects:
                plaintext = base64.b64decode(o["content_b64"])
                envelope = encrypt_content(self.agent_key, plaintext, o["object_id"])
                o["content_b64"] = base64.b64encode(envelope).decode()
                o["client_encrypted"] = True
                o["size_bytes"] = len(envelope)
            # Push in batches to keep requests reasonable.
            for i in range(0, len(objects), 50):
                batch = objects[i:i + 50]
                r = httpx.post(f"{self.cfg.cloud_base_url}/agent/ingest", json={
                    "source_type": "onepassword",
                    "destinations": destinations,
                    "objects": batch,
                }, headers=self._headers(), timeout=120)
                r.raise_for_status()
            total += len(objects)
            results.append({"collector": name, "objects": len(objects)})
        self._last_collect = time.time()
        self._write_status({"last_collect": _now_iso(), "results": results})
        print(f"Pushed {total} objects")
        return {"objects": total, "results": results}

    # -- self update --------------------------------------------------

    def self_update(self) -> None:
        script = Path(self.cfg.home) / "desktop-agent" / "update.sh"
        if not script.exists():
            script = Path(self.cfg.home) / "update.sh"
        if script.exists():
            subprocess.Popen(["bash", str(script)])
        else:
            print("no update script found")

    # -- run loop -----------------------------------------------------

    def run(self) -> None:
        if not self.registered:
            if self.cfg.linking_code:
                self.activate(self.cfg.linking_code)
            else:
                print("Not registered. Run: arkive-agent link <CODE>")
                return
        interval = self.reg.get("heartbeat_interval_seconds", 30)
        while True:
            try:
                self.heartbeat()
                schedule_min = self.reg.get("config", {}).get("schedule_minutes", 360)
                if time.time() - self._last_collect >= schedule_min * 60:
                    self.collect_and_push()
            except Exception as exc:
                print(f"[loop] error: {exc}")
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
