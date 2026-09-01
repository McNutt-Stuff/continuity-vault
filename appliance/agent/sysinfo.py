"""Real system, network, storage, and platform telemetry for the appliance.

All probes are best-effort and degrade gracefully so a heartbeat never fails
because a metric is unavailable on a given host/VM.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import time
from typing import Optional

_BOOT = time.time()


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


def local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return ""


def detect_platform() -> dict:
    """Distinguish a physical appliance from a VM and capture model/vendor."""
    virt = _run(["systemd-detect-virt"]) or ""
    # systemd-detect-virt prints 'none' on bare metal and returns 1 (empty here).
    is_vm = bool(virt) and virt not in ("none", "")
    product = _read("/sys/class/dmi/id/product_name")
    vendor = _read("/sys/class/dmi/id/sys_vendor")
    board = _read("/sys/class/dmi/id/board_name")
    # Common VM signatures as a fallback when systemd-detect-virt is absent.
    vm_hints = ("virtual", "vmware", "kvm", "qemu", "xen", "hyper-v", "hyperv",
                "bochs", "amazon ec2", "google", "droplet")
    blob = f"{product} {vendor} {board}".lower()
    if not is_vm and any(h in blob for h in vm_hints):
        is_vm = True
        virt = virt or next((h for h in vm_hints if h in blob), "vm")
    return {
        "kind": "vm" if is_vm else "hardware",
        "virtualization": virt or "none",
        "product": product or "unknown",
        "vendor": vendor or "unknown",
        "board": board or "",
    }


def system_stats() -> dict:
    load1 = load5 = load15 = 0.0
    try:
        load1, load5, load15 = os.getloadavg()
    except Exception:
        pass
    mem_total = mem_avail = 0
    meminfo = _read("/proc/meminfo")
    for line in meminfo.splitlines():
        if line.startswith("MemTotal:"):
            mem_total = int(line.split()[1]) * 1024
        elif line.startswith("MemAvailable:"):
            mem_avail = int(line.split()[1]) * 1024
    uptime = 0.0
    up = _read("/proc/uptime")
    if up:
        try:
            uptime = float(up.split()[0])
        except Exception:
            uptime = time.time() - _BOOT
    return {
        "hostname": socket.gethostname(),
        "os": platform.platform(),
        "kernel": platform.release(),
        "arch": platform.machine(),
        "cpu_count": os.cpu_count() or 0,
        "load_avg": [round(load1, 2), round(load5, 2), round(load15, 2)],
        "mem_total_bytes": mem_total,
        "mem_available_bytes": mem_avail,
        "uptime_seconds": int(uptime),
        "agent_uptime_seconds": int(time.time() - _BOOT),
    }


def disk_stats(path: str) -> dict:
    try:
        total, used, free = shutil.disk_usage(path)
        return {"disk_total_bytes": total, "disk_used_bytes": used,
                "disk_free_bytes": free}
    except Exception:
        return {"disk_total_bytes": 0, "disk_used_bytes": 0, "disk_free_bytes": 0}


def mount_device(path: str) -> str:
    """The block device / filesystem backing ``path`` (best-effort)."""
    out = _run(["findmnt", "-n", "-o", "SOURCE", "--target", path])
    if out:
        return out.splitlines()[0].strip()
    dfl = _run(["df", "-P", path]).splitlines()
    if len(dfl) >= 2:
        return dfl[-1].split()[0]
    return ""


def net_io() -> dict:
    """Cumulative bytes sent/received across physical interfaces (since boot).

    Reads /proc/net/dev and skips loopback + virtual bridges/containers so the
    figure reflects real WAN/LAN traffic."""
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
    return {"bytes_sent": sent, "bytes_recv": recv}


def smart_status() -> dict:
    """Best-effort SMART health for the primary disk (needs smartmontools)."""
    out = _run(["smartctl", "-H", "/dev/sda"])
    if not out:
        return {"enabled": False}
    passed = "PASSED" in out or "OK" in out.upper()
    return {"enabled": True, "status": "passed" if passed else "failing"}


def raid_status() -> dict:
    """Best-effort software-RAID health from /proc/mdstat (Ubuntu mdadm), with the
    per-array detail (device, level, member state) so the UI can show the array."""
    md = _read("/proc/mdstat")
    if not md or "active" not in md:
        return {"enabled": False}
    arrays: list[dict] = []
    lines = md.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("md"):
            continue
        parts = line.split()
        name = parts[0]
        level = next((p for p in parts if p.startswith("raid")), "")
        members = [p.split("[")[0] for p in parts if "[" in p and "]" in p and not p.startswith("[")]
        # The following line carries the [N/M] count and [UU_] member map.
        status_line = lines[i + 1] if i + 1 < len(lines) else ""
        umap = ""
        for seg in status_line.split():
            if seg.startswith("[") and seg.endswith("]") and set(seg[1:-1]) <= {"U", "_"}:
                umap = seg
        degraded = "_" in umap
        recovering = "recovery" in status_line or "resync" in status_line
        arrays.append({
            "name": name, "level": level, "members": members,
            "state": "rebuilding" if recovering else ("degraded" if degraded else "optimal"),
            "member_map": umap,
        })
    degraded = any(a["state"] != "optimal" for a in arrays)
    return {"enabled": True,
            "status": "degraded" if degraded else "optimal",
            "arrays": arrays}


def is_dedicated_mount(path: str) -> bool:
    """True when ``path`` is a separate, writable mount point — i.e. a dedicated
    disk/RAID volume (e.g. /arkive), not just a directory on the system disk."""
    try:
        return (os.path.isdir(path) and os.path.ismount(path)
                and os.access(path, os.W_OK))
    except Exception:
        return False


def drive_temperature_c() -> Optional[int]:
    for zone in ("/sys/class/thermal/thermal_zone0/temp",):
        raw = _read(zone)
        if raw.isdigit():
            return int(raw) // 1000
    return None


def storage_report(path: str, name: str, kind: str, raw_total: int, used_bytes: int) -> list[dict]:
    """Per-storage capacity + health the cloud maps onto ApplianceStorage rows.

    Prototype reports the primary (built-in) volume; extra health probes (SMART,
    RAID, temperature) populate only when the tooling/hardware is present."""
    disk = disk_stats(path)
    total = raw_total or disk["disk_total_bytes"]
    used = used_bytes or disk["disk_used_bytes"]
    health = {
        "drive_health": "healthy",
        "smart": smart_status(),
        "raid": raid_status(),
    }
    temp = drive_temperature_c()
    if temp is not None:
        health["temperature_c"] = temp
    return [{
        "name": name,
        "kind": kind,
        "capacity_bytes": total,
        "used_bytes": used,
        "free_bytes": max(total - used, 0),
        "health": health,
    }]


def pq_available() -> Optional[bool]:
    try:
        from cv_crypto.provider import get_provider
        return get_provider().pq_available
    except Exception:
        return None
