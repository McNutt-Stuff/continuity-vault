"""
Privileged external-storage helper for the appliance.

The agent runs sandboxed (systemd ``NoNewPrivileges=true`` + ``ProtectSystem=strict``
as ``cvagent``) so it CANNOT partition/format/mount a USB/removable drive itself —
and it can't ``sudo`` (NoNewPrivileges blocks escalation). So the portal "Set up
detected drive" flow can't run inside the agent process.

This helper runs as ROOT (a tiny oneshot triggered by ``cv-appliance-storage.path``
whenever the agent drops a request into the queue directory). It performs the
destructive disk operation, hands the volume to the agent's service account, and
writes a result file the agent polls for. All the same serial-matched, external-
only safety guards in :mod:`agent.storage_ops` apply.

Queue protocol (files under ``<data_dir>/storage-queue``, group-writable by both):
  * ``req-<id>.json``  {action: setup|forget, ...params}   (written by the agent)
  * ``res-<id>.json``  {ok, ...result} | {error}           (written by this helper)
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from .config import get_settings
from . import storage_ops

settings = get_settings()
DATA = Path(settings.data_dir)
EXT_BASE = DATA / "ext"
QUEUE = DATA / "storage-queue"
SERVICE_USER = os.environ.get("ARKIVE_SERVICE_USER", "cvagent")


def _write_result(req_id: str, payload: dict) -> None:
    res = QUEUE / f"res-{req_id}.json"
    tmp = QUEUE / f".res-{req_id}.tmp"
    tmp.write_text(json.dumps(payload))
    tmp.replace(res)
    # The agent (cvagent) must be able to read + clean up the result.
    try:
        subprocess.run(["chown", f"{SERVICE_USER}:{SERVICE_USER}", str(res)],
                       capture_output=True, timeout=10)
    except Exception:  # noqa: BLE001
        pass


def _handle(req: dict) -> dict:
    action = req.get("action")
    params = req.get("params") or {}
    if action == "setup":
        store_id = params.get("storeId")
        if not store_id:
            return {"error": "missing storeId"}
        res = storage_ops.setup_device(
            device=params.get("device", ""), serial=params.get("serial", ""),
            store_id=store_id, name=params.get("name", "External Storage"),
            mount_base=str(EXT_BASE), dedicated_path=settings.dedicated_path,
            mirror_of_id=params.get("mirrorOfId"),
            kind=params.get("kind", "external"))
        # Hand the fresh volume to the sandboxed agent's service account so it can
        # write vault data (it can't chown/mount itself).
        mp = res["mountpoint"]
        try:
            subprocess.run(["chown", "-R", f"{SERVICE_USER}:{SERVICE_USER}", mp],
                           capture_output=True, timeout=60)
            os.chmod(mp, 0o750)
        except Exception as exc:  # noqa: BLE001
            return {**res, "ok": True, "chown_warning": str(exc)}
        return {**res, "ok": True}
    if action == "forget":
        store_id = params.get("storeId")
        mp = params.get("mountpoint") or str(EXT_BASE / (store_id or ""))
        storage_ops.unmount(mp)
        return {"ok": True, "store_id": store_id, "forgotten": True}
    return {"error": f"unknown action {action!r}"}


def process_once() -> int:
    """Process every queued request. Returns the number handled."""
    if not QUEUE.is_dir():
        return 0
    handled = 0
    for req_path in sorted(QUEUE.glob("req-*.json")):
        req_id = req_path.stem[len("req-"):]
        try:
            req = json.loads(req_path.read_text())
        except Exception as exc:  # noqa: BLE001
            _write_result(req_id, {"error": f"unreadable request: {exc}"})
            req_path.unlink(missing_ok=True)
            continue
        try:
            result = _handle(req)
        except storage_ops.StorageOpError as exc:
            result = {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            result = {"error": f"{type(exc).__name__}: {exc}"}
        _write_result(req_id, result)
        req_path.unlink(missing_ok=True)
        handled += 1
    return handled


def main(argv=None) -> int:
    # Process now, then briefly re-scan so a request that lands during processing
    # (the .path unit may not re-trigger while we're still running) isn't missed.
    total = process_once()
    deadline = time.time() + 3
    while time.time() < deadline:
        if process_once():
            deadline = time.time() + 3
        else:
            time.sleep(0.3)
    print(json.dumps({"handled": total}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
