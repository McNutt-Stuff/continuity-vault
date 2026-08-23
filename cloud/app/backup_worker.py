"""
Infrastructure backup worker.

Runs OUTSIDE the API process (a systemd timer, ``cv-backup.timer``) on every node
and the control plane. It backs this node's core state up to its assigned backup
storage service objects, and — on a non-control-plane node — reports the result
to the control plane so the fleet-wide admin Backups view stays authoritative.

Usage:  python -m app.backup_worker           # one backup, then exit
        python -m app.backup_worker --loop    # back up on an interval, forever
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

from .config import get_settings
from .db import WorkerSessionLocal as SessionLocal


def _report_to_control_plane(run: dict) -> None:
    """Non-CP nodes push their run up so the control plane aggregates it."""
    s = get_settings()
    if (s.node_role or "control-plane") == "control-plane":
        return  # already recorded in the CP database
    if not s.control_plane_url or not s.node_secret:
        return
    url = s.control_plane_url.rstrip("/") + "/api/nodes/backup-report"
    req = urllib.request.Request(
        url, data=json.dumps(run).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {s.node_secret}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
    except urllib.error.HTTPError as e:
        print(f"[backup] control plane rejected report: {e.code}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"[backup] could not report to control plane: {e}", file=sys.stderr)


def run_once() -> dict | None:
    from . import backup_service
    with SessionLocal() as db:
        run = backup_service.run_backup_once(db)
    if run:
        print(f"[backup] {run['node_name']}: {run['status']} "
              f"({run.get('total_bytes', 0)} bytes)")
        _report_to_control_plane(run)
    return run


def main(argv=None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    loop = "--loop" in argv
    # Default cadence: daily. Override with CV_BACKUP_INTERVAL_MINUTES.
    settings = get_settings()
    interval = max(5, int(getattr(settings, "backup_interval_minutes", 1440) or 1440)) * 60
    if not loop:
        run_once()
        return
    while True:
        try:
            run_once()
        except Exception as exc:  # noqa: BLE001 - never let the loop die
            print(f"[backup] run failed: {exc}", file=sys.stderr)
        time.sleep(interval)


if __name__ == "__main__":
    main()
