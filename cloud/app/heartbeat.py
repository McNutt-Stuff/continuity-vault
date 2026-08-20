"""
Fleet heartbeat client. Runs on customer-tenant and public-web nodes (via a
systemd timer) to report health + detected cloud environment to the control
plane and receive this node's role blueprint (target version + config/settings).

Usage:  python -m app.heartbeat            # one heartbeat, exit
        python -m app.heartbeat --loop     # heartbeat forever (interval from CP)

Auth is a shared secret (CV_NODE_SECRET) configured on this node and the
control plane. Configuration comes from settings / environment:
    CV_NODE_ROLE, CV_NODE_NAME, CV_NODE_SECRET, CV_CONTROL_PLANE_URL
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
import urllib.error

from .config import get_settings

BLUEPRINT_PATH = "/etc/arkive/blueprint.json"


def _telemetry() -> dict:
    """Lightweight host telemetry (best-effort, no heavy deps)."""
    tel: dict = {}
    try:
        import shutil
        usage = shutil.disk_usage("/")
        tel["storage"] = {"total": usage.total, "used": usage.used, "free": usage.free}
    except Exception:
        pass
    try:
        import os
        load = os.getloadavg()
        tel["load"] = [round(x, 2) for x in load]
        tel["cpus"] = os.cpu_count()
    except Exception:
        pass
    return tel


def _version() -> str:
    try:
        with open("/etc/arkive/version", "r") as fh:
            return fh.read().strip()
    except Exception:
        return ""


def send_heartbeat() -> dict | None:
    s = get_settings()
    if not s.control_plane_url or not s.node_secret:
        print("[heartbeat] control_plane_url / node_secret not configured; skipping",
              file=sys.stderr)
        return None

    try:
        from . import cloud_detect
        cloud = cloud_detect.detect()
    except Exception:
        cloud = {}

    payload = {
        "name": s.node_name or "",
        "role": s.node_role,
        "version": _version(),
        "endpoint": "",
        "telemetry": _telemetry(),
        "cloud": cloud,
    }
    url = s.control_plane_url.rstrip("/") + "/api/nodes/heartbeat"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {s.node_secret}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"[heartbeat] control plane rejected heartbeat: {e.code}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[heartbeat] failed to reach control plane: {e}", file=sys.stderr)
        return None

    bp = data.get("blueprint") or {}
    _apply_blueprint(bp)
    # Public-web nodes mirror the published site content locally so the site is
    # served same-origin and survives control-plane downtime.
    if s.node_role == "public-web":
        _mirror_site_content(s)
    return data


def _mirror_site_content(s) -> None:
    dest = getattr(s, "site_content_path", None) or ""
    if not dest:
        return
    url = s.control_plane_url.rstrip("/") + "/api/site"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            body = resp.read()
        import os
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(body)
        os.replace(tmp, dest)  # atomic swap so the site never reads a partial file
    except Exception as e:
        print(f"[heartbeat] could not mirror site content: {e}", file=sys.stderr)


def _apply_blueprint(bp: dict) -> None:
    """Persist the received blueprint so the node's updater/config can consume
    it. Applying the target version is handled by the role-aware updater."""
    if not bp:
        return
    try:
        import os
        os.makedirs("/etc/arkive", exist_ok=True)
        with open(BLUEPRINT_PATH, "w") as fh:
            json.dump(bp, fh, indent=2)
    except Exception as e:
        print(f"[heartbeat] could not write blueprint: {e}", file=sys.stderr)


def main() -> int:
    loop = "--loop" in sys.argv
    if not loop:
        return 0 if send_heartbeat() is not None else 1
    while True:
        data = send_heartbeat()
        interval = (data or {}).get("heartbeat_interval_seconds", 60)
        time.sleep(max(15, int(interval)))


if __name__ == "__main__":
    raise SystemExit(main())
