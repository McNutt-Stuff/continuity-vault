"""
Fleet heartbeat client. Runs on customer-tenant and public-web nodes (via a
systemd timer) to report health + detected cloud environment to the control
plane and receive this node's settings (from its bound configuration profiles).

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

SETTINGS_PATH = "/etc/arkive/node-settings.json"


def _telemetry() -> dict:
    """Host telemetry for the heartbeat snapshot (best-effort). Rich live metrics
    (per-process, net rates, services) come from the node's live endpoint."""
    try:
        from . import sysinfo
        return sysinfo.snapshot()
    except Exception:
        # Minimal fallback so a heartbeat never fails on telemetry.
        tel: dict = {}
        try:
            import shutil
            usage = shutil.disk_usage("/")
            tel["storage"] = {"total": usage.total, "used": usage.used, "free": usage.free}
        except Exception:
            pass
        try:
            import os
            tel["load"] = [round(x, 2) for x in os.getloadavg()]
            tel["cpus"] = os.cpu_count()
        except Exception:
            pass
        return tel


def _version() -> str:
    # Fleet nodes self-update from the control plane's bundle; report that bundle
    # version (matches the CP's served version) so the admin can flag out-of-date
    # nodes. Fall back to the git version stamp.
    for path in ("/etc/arkive/bundle-version", "/etc/arkive/version"):
        try:
            with open(path, "r") as fh:
                v = fh.read().strip()
                if v:
                    return v
        except Exception:
            continue
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
        "endpoint": s.api_base_url,
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

    # Persist the settings this node was assigned (via its bound configuration
    # profiles) so the running processes (scheduler, notifications) apply them.
    _apply_settings(data.get("settings") or {})
    # Public-web nodes mirror the published site content locally so the site is
    # served same-origin and survives control-plane downtime.
    if s.node_role == "public-web":
        _mirror_site_content(s)
        _mirror_support_content(s)
    return data


def _mirror_support_content(s) -> None:
    """Mirror the published support docs to a local JSON file (support.json) so the
    Public Web Node serves /support same-origin and offline. Defaults next to the
    site mirror when a dedicated path isn't configured."""
    dest = getattr(s, "support_content_path", None) or ""
    if not dest:
        site_dest = getattr(s, "site_content_path", None) or ""
        if not site_dest:
            return
        import os
        dest = os.path.join(os.path.dirname(site_dest), "support.json")
    url = s.control_plane_url.rstrip("/") + "/api/support/content"
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
        print(f"[heartbeat] could not mirror support content: {e}", file=sys.stderr)


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
        _inline_into_index(os.path.dirname(dest), body.decode("utf-8", "replace"))
    except Exception as e:
        print(f"[heartbeat] could not mirror site content: {e}", file=sys.stderr)


def _inline_into_index(webroot: str, body_text: str) -> None:
    """Embed the published content into index.html as window.__ARKIVE_CMS__ so
    the page renders real content on first paint without any runtime fetch."""
    import os
    import re
    index = os.path.join(webroot, "index.html")
    try:
        html = open(index, "r", encoding="utf-8").read()
    except Exception:
        return
    start, end = "<!--ARKIVE_CMS_START-->", "<!--ARKIVE_CMS_END-->"
    safe = body_text.replace("</", "<\\/")  # never break out of the <script>
    block = f'{start}<script>window.__ARKIVE_CMS__={safe}</script>{end}'
    if start in html and end in html:
        new = re.sub(re.escape(start) + ".*?" + re.escape(end),
                     lambda _m: block, html, count=1, flags=re.S)
    else:
        new = html.replace("</head>", block + "</head>", 1)
    if new == html:
        return
    try:
        tmp = index + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(new)
        os.replace(tmp, index)
    except Exception as e:
        print(f"[heartbeat] could not inline site content: {e}", file=sys.stderr)


def _apply_settings(settings: dict) -> None:
    """Persist the settings this node received so node_config can resolve them for
    the scheduler / notifications (config profiles reconfigure live behavior).
    Logs a verbose diff whenever the applied configuration actually changes."""
    settings = settings or {}
    try:
        import os
        # Diff against what we already have so a profile roll-out is visible in logs.
        current: dict = {}
        try:
            with open(SETTINGS_PATH) as fh:
                current = json.load(fh) or {}
        except (FileNotFoundError, ValueError):
            current = {}
        if current != settings:
            added = {k: settings[k] for k in settings if k not in current}
            removed = [k for k in current if k not in settings]
            changed = {k: f"{current[k]} → {settings[k]}"
                       for k in settings if k in current and current[k] != settings[k]}
            print(f"[heartbeat] applying config profile settings ({len(settings)} keys): "
                  f"added={added or '{}'} changed={changed or '{}'} removed={removed or '[]'}",
                  file=sys.stderr)
        os.makedirs("/etc/arkive", exist_ok=True)
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(settings, fh, indent=2)
        os.replace(tmp, SETTINGS_PATH)
    except Exception as e:
        print(f"[heartbeat] could not write node settings: {e}", file=sys.stderr)



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
