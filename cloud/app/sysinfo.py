"""Real host telemetry for a cloud node (control-plane or customer-tenant).

Best-effort probes that degrade gracefully — CPU / memory / disk / network,
per-process utilization, managed service state, uptime, and TLS certificate
health. Used by the node's own live-telemetry endpoint and by the control plane
for its self node. Mirrors the appliance ``sysinfo`` patterns.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import ssl
import subprocess
import time
from typing import Optional

_BOOT = time.time()

# Managed services we monitor / can control on a node. Whitelisted so a control
# action can never target an arbitrary unit.
SERVICES = {
    "cv-cloud": "Arkive application",
    "postgresql": "PostgreSQL database",
    "caddy": "TLS reverse proxy",
    "cv-node-heartbeat.timer": "Fleet heartbeat",
    "cv-backup.timer": "Infrastructure backup",
    "cv-node-update.timer": "Self-update timer",
    "cv-cloud-update.timer": "Update timer",
}

# CPU/network need deltas between calls; keep the previous sample per process.
_prev_cpu: Optional[tuple[int, int]] = None
_prev_net: Optional[tuple[float, int, int]] = None


def _run(cmd: list[str], timeout: float = 3.0) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return ""


def _cpu_times() -> Optional[tuple[int, int]]:
    line = _read("/proc/stat").splitlines()
    if not line or not line[0].startswith("cpu "):
        return None
    parts = [int(x) for x in line[0].split()[1:]]
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0)  # idle + iowait
    total = sum(parts)
    return total, idle


def cpu_percent() -> float:
    """System-wide CPU utilization % since the previous call (delta-based)."""
    global _prev_cpu
    cur = _cpu_times()
    if cur is None:
        # macOS / no /proc: approximate from load average.
        try:
            return round(min(100.0, (os.getloadavg()[0] / (os.cpu_count() or 1)) * 100), 1)
        except Exception:
            return 0.0
    if _prev_cpu is None:
        _prev_cpu = cur
        time.sleep(0.12)
        cur2 = _cpu_times()
        if cur2:
            cur = cur2
    total_d = cur[0] - _prev_cpu[0]
    idle_d = cur[1] - _prev_cpu[1]
    _prev_cpu = cur
    if total_d <= 0:
        return 0.0
    return round(max(0.0, min(100.0, (1.0 - idle_d / total_d) * 100)), 1)


def mem() -> dict:
    info: dict = {}
    for line in _read("/proc/meminfo").splitlines():
        k, _, rest = line.partition(":")
        p = rest.strip().split()
        if p:
            info[k.strip()] = int(p[0]) * 1024
    total, avail = info.get("MemTotal"), info.get("MemAvailable")
    if total and avail is not None:
        used = total - avail
        return {"total": total, "used": used, "free": avail,
                "pct": round(used / total * 100, 1) if total else 0}
    # Fallback (macOS dev): no /proc/meminfo.
    return {"total": 0, "used": 0, "free": 0, "pct": 0}


def disk(path: str) -> dict:
    try:
        total, used, free = shutil.disk_usage(path if os.path.exists(path) else "/")
        return {"total": total, "used": used, "free": free,
                "pct": round(used / total * 100, 1) if total else 0}
    except Exception:
        return {"total": 0, "used": 0, "free": 0, "pct": 0}


def net_rates() -> dict:
    """Per-second send/recv rates across physical interfaces (delta-based)."""
    global _prev_net
    sent = recv = 0
    for line in _read("/proc/net/dev").splitlines():
        if ":" not in line:
            continue
        iface, _, rest = line.partition(":")
        iface = iface.strip()
        if iface == "lo" or iface.startswith(("veth", "docker", "br-", "virbr")):
            continue
        cols = rest.split()
        if len(cols) >= 9:
            try:
                recv += int(cols[0])
                sent += int(cols[8])
            except ValueError:
                pass
    now = time.time()
    rates = {"sent_rate": 0, "recv_rate": 0, "bytes_sent": sent, "bytes_recv": recv}
    if _prev_net is not None:
        dt = now - _prev_net[0]
        if dt > 0:
            rates["sent_rate"] = max(0, int((sent - _prev_net[1]) / dt))
            rates["recv_rate"] = max(0, int((recv - _prev_net[2]) / dt))
    _prev_net = (now, sent, recv)
    return rates


def uptime_seconds() -> int:
    up = _read("/proc/uptime")
    if up:
        try:
            return int(float(up.split()[0]))
        except Exception:
            pass
    return int(time.time() - _BOOT)


def load_avg() -> list[float]:
    try:
        return [round(x, 2) for x in os.getloadavg()]
    except Exception:
        return [0.0, 0.0, 0.0]


def snapshot(object_store: str = "") -> dict:
    """Compact metrics for heartbeat + history sampling."""
    path = object_store or os.environ.get("CV_OBJECT_STORE") or "/var/lib/continuity-vault"
    return {
        "cpu_pct": cpu_percent(),
        "memory": mem(),
        "storage": disk(path),
        "net": net_rates(),
        "load": load_avg(),
        "cpus": os.cpu_count() or 0,
        "uptime_seconds": uptime_seconds(),
        "hostname": socket.gethostname(),
        "os": platform.platform(),
        "kernel": platform.release(),
        "arch": platform.machine(),
    }


def processes(top: int = 8) -> list[dict]:
    """Top processes by CPU (best-effort via ps)."""
    out = _run(["ps", "-eo", "pid,comm,%cpu,%mem,rss", "--sort=-%cpu"])
    rows: list[dict] = []
    for line in out.splitlines()[1:]:
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        try:
            rows.append({"pid": int(parts[0]), "name": parts[1],
                         "cpu_pct": float(parts[2]), "mem_pct": float(parts[3]),
                         "rss_bytes": int(parts[4]) * 1024})
        except ValueError:
            continue
        if len(rows) >= top:
            break
    return rows


def service_status(unit: str) -> dict:
    """State + resource use of a systemd unit (best-effort)."""
    active = _run(["systemctl", "is-active", unit]) or "unknown"
    enabled = _run(["systemctl", "is-enabled", unit]) or "unknown"
    show = _run(["systemctl", "show", unit, "-p",
                 "MainPID,MemoryCurrent,ActiveEnterTimestampMonotonic,SubState"])
    mem_bytes = 0
    sub = ""
    for line in show.splitlines():
        k, _, v = line.partition("=")
        if k == "MemoryCurrent" and v.isdigit():
            mem_bytes = int(v)
        elif k == "SubState":
            sub = v
    return {"unit": unit, "active": active, "enabled": enabled,
            "sub_state": sub, "memory_bytes": mem_bytes}


def services() -> list[dict]:
    out = []
    for unit, label in SERVICES.items():
        s = service_status(unit)
        s["label"] = label
        out.append(s)
    return out


def cert_info(host: str, port: int = 443) -> dict:
    """TLS certificate validity + expiry for this node's own endpoint."""
    try:
        ctx = ssl.create_default_context()  # verified: exposes the full cert + validity
        with socket.create_connection((host, port), timeout=3) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
                valid = True
        not_after = cert.get("notAfter") if cert else None
        expires = None
        days_left = None
        if not_after:
            t = ssl.cert_time_to_seconds(not_after)
            expires = t
            days_left = int((t - time.time()) / 86400)
        return {"reachable": True, "expires_epoch": expires, "days_left": days_left,
                "valid": valid,
                "subject": dict(x[0] for x in cert.get("subject", [])) if cert else {},
                "issuer": dict(x[0] for x in cert.get("issuer", [])) if cert else {}}
    except Exception as exc:  # noqa: BLE001
        return {"reachable": False, "valid": False, "error": str(exc)[:120]}


def live(object_store: str = "", cert_host: str = "") -> dict:
    """Full instantaneous telemetry for the live view."""
    out = snapshot(object_store)
    out["processes"] = processes()
    out["services"] = services()
    if cert_host:
        out["certificate"] = cert_info(cert_host)
    return out


# --- logs -------------------------------------------------------------------

_LOG_UNITS = {
    "app": "cv-cloud",
    "database": "postgresql",
    "proxy": "caddy",
    "heartbeat": "cv-node-heartbeat",
    "system": "",  # whole-system journal
}


def logs(source: str = "app", lines: int = 200) -> list[dict]:
    """Recent log lines for a source (journald). Returns [{ts, level, text}]."""
    unit = _LOG_UNITS.get(source, "cv-cloud")
    n = min(2000, max(10, int(lines)))
    cmd = ["journalctl", "-n", str(n), "--no-pager", "-o", "short-iso"]
    if unit:
        cmd += ["-u", unit]
    out = _run(cmd, timeout=6)
    rows: list[dict] = []
    for line in out.splitlines():
        low = line.lower()
        level = ("error" if ("error" in low or "traceback" in low or "critical" in low)
                 else "warn" if ("warn" in low)
                 else "info")
        rows.append({"text": line, "level": level})
    return rows


# --- control actions --------------------------------------------------------

_CONTROL_ACTIONS = {"restart", "stop", "start", "enable", "disable"}


def _systemctl(args: list[str]) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=40)
        if r.returncode != 0:
            r = subprocess.run(["sudo", "-n", "systemctl", *args],
                               capture_output=True, text=True, timeout=40)
        return r
    except Exception as exc:  # noqa: BLE001
        return subprocess.CompletedProcess(args, 1, "", str(exc))


def control(action: str, unit: str, role: str = "control-plane") -> dict:
    """Run a whitelisted service control action. Requires the service user to
    have (passwordless) sudo for systemctl on these units."""
    if action == "update":
        svc = "cv-cloud-update.service" if role == "control-plane" else "cv-node-update.service"
        r = _systemctl(["start", svc])
        ok = r.returncode == 0
        return {"ok": ok, "action": action, "unit": svc,
                "error": "" if ok else (r.stderr or "systemctl failed").strip()[:200]}
    if unit not in SERVICES:
        return {"ok": False, "error": f"service '{unit}' is not controllable"}
    if action not in _CONTROL_ACTIONS:
        return {"ok": False, "error": f"unknown action '{action}'"}
    r = _systemctl([action, unit])
    ok = r.returncode == 0
    return {"ok": ok, "action": action, "unit": unit,
            "error": "" if ok else (r.stderr or "systemctl failed").strip()[:200]}
