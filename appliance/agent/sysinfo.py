"""Real system, network, storage, and platform telemetry for the appliance.

All probes are best-effort and degrade gracefully so a heartbeat never fails
because a metric is unavailable on a given host/VM.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import socket
import subprocess
import time
from typing import Optional

import json

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


def smart_status(device: str = "/dev/sda") -> dict:
    """Best-effort SMART health for a single device (needs smartmontools)."""
    out = _run(["smartctl", "-H", device])
    if not out:
        return {"enabled": False}
    passed = "PASSED" in out or "OK" in out.upper()
    return {"enabled": True, "status": "passed" if passed else "failing"}


def fs_type(path: str) -> str:
    """Filesystem type backing a path (ext4/xfs/zfs/btrfs…), best-effort."""
    out = _run(["findmnt", "-n", "-o", "FSTYPE", "--target", path])
    return out.strip() if out else ""


def _backing_devices(path: str) -> list[str]:
    """Whole physical disks backing a mount, resolving software-RAID members."""
    src = mount_device(path)
    if not src.startswith("/dev/"):
        return []
    base = src.rsplit("/", 1)[-1]
    if base.startswith("md"):
        devs: list[str] = []
        for line in _read("/proc/mdstat").splitlines():
            if line.startswith(base + " ") or line.startswith(base + ":"):
                for p in line.split():
                    if "[" in p and not p.startswith("["):
                        m = "/dev/" + re.sub(r"\d+$", "", p.split("[")[0])
                        if m not in devs:
                            devs.append(m)
        return devs
    return ["/dev/" + re.sub(r"\d+$", "", base)]


def smart_for_path(path: str) -> dict:
    """Aggregate SMART health across the physical drives backing ``path``."""
    drives: list[dict] = []
    all_ok = True
    for d in _backing_devices(path) or ["/dev/sda"]:
        s = smart_status(d)
        if not s.get("enabled"):
            continue
        ok = s.get("status") == "passed"
        all_ok = all_ok and ok
        drives.append({"device": d, "status": s.get("status", "unknown")})
    if not drives:
        return {"enabled": False}
    return {"enabled": True, "status": "passed" if all_ok else "failing", "drives": drives}


def os_disk() -> dict:
    """Usage + identity of the built-in OS / system disk (root filesystem) — tracked
    separately from the dedicated Arkive storage volume."""
    d = disk_stats("/")
    total, used = d["disk_total_bytes"], d["disk_used_bytes"]
    return {"total_bytes": total, "used_bytes": used, "free_bytes": d["disk_free_bytes"],
            "pct": round(used / total * 100, 1) if total else 0.0,
            "device": mount_device("/"), "filesystem": fs_type("/")}


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
        "smart": smart_for_path(path),
        "raid": raid_status(),
        "filesystem": fs_type(path),
        "device": mount_device(path),
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


def lan_security() -> dict:
    """Best-effort count of recent failed inbound auth attempts (LAN intrusion
    signal). Reads the systemd journal, falling back to auth.log. Never raises —
    returns zero counts when the source is unavailable so telemetry stays clean."""
    window_min = 60
    failed = 0
    out = _run(["journalctl", "-q", "--since", f"-{window_min} min",
                "-u", "ssh", "-u", "sshd", "--no-pager"], timeout=4)
    if not out:
        # Fall back to the classic auth log (Debian/Ubuntu).
        raw = _read("/var/log/auth.log")
        out = raw[-200000:] if raw else ""
    for line in out.splitlines():
        low = line.lower()
        if ("failed password" in low or "invalid user" in low
                or "authentication failure" in low or "connection closed by authenticating" in low):
            failed += 1
    return {"failed_auth": failed, "window_min": window_min}


# --------------------------------------------------------------------------- #
# External / removable block-device detection (read-only)                     #
# --------------------------------------------------------------------------- #
# The label written to Arkive-managed external volumes and the marker file that
# proves a drive is "ours" (carries the store id so a moved drive is recognised).
ARKIVE_LABEL = "ARKIVE"
ARKIVE_MARKER = ".arkive-store.json"


def lsblk_disks() -> list[dict]:
    """Parse ``lsblk -J`` into a list of whole-disk dicts (with partition children).

    Best-effort: returns [] when lsblk is unavailable so callers degrade to "no
    external storage detected" rather than failing a heartbeat."""
    out = _run(["lsblk", "-b", "-J", "-o",
                "NAME,PATH,TYPE,SIZE,MOUNTPOINT,FSTYPE,LABEL,SERIAL,MODEL,TRAN,HOTPLUG,RM,VENDOR"])
    if not out:
        return []
    try:
        return json.loads(out).get("blockdevices", []) or []
    except Exception:
        return []


def _pkname(dev: str) -> str:
    """Parent whole-disk name for a partition (e.g. sda3 -> sda), via lsblk."""
    out = _run(["lsblk", "-ndo", "PKNAME", dev])
    return out.strip().splitlines()[0].strip() if out else ""


def system_disk_device() -> str:
    """The whole disk backing '/' (e.g. /dev/sda) — never a candidate for setup."""
    src = mount_device("/")
    if not src.startswith("/dev/"):
        return ""
    pk = _pkname(src)
    if pk:
        return f"/dev/{pk}"
    base = src.rsplit("/", 1)[-1]
    return f"/dev/{re.sub(r'p?[0-9]+$', '', base)}"


def dedicated_disk_device(dedicated_path: str) -> str:
    """The whole disk backing the dedicated volume (e.g. /arkive), if mounted."""
    if not (os.path.isdir(dedicated_path) and os.path.ismount(dedicated_path)):
        return ""
    src = mount_device(dedicated_path)
    if not src.startswith("/dev/"):
        return ""
    base = src.rsplit("/", 1)[-1]
    if base.startswith("md"):
        return f"/dev/{base}"
    pk = _pkname(src)
    if pk:
        return f"/dev/{pk}"
    return f"/dev/{re.sub(r'p?[0-9]+$', '', base)}"


def resolve_device_by_serial(serial: str) -> str:
    """Re-resolve a whole-disk device path from its stable serial. Used right
    before any destructive op so a changed /dev name can't cause us to touch the
    wrong disk."""
    if not serial:
        return ""
    for d in lsblk_disks():
        if d.get("type") == "disk" and (d.get("serial") or "") == serial:
            return d.get("path") or f"/dev/{d.get('name', '')}"
    return ""


def _first_data_partition(disk: dict) -> dict:
    """The partition Arkive would use (first child) or the disk itself if none."""
    kids = disk.get("children") or []
    return kids[0] if kids else disk


def read_marker(mountpoint: str) -> Optional[dict]:
    """Read the Arkive store marker from a mounted volume, if present."""
    try:
        p = os.path.join(mountpoint, ARKIVE_MARKER)
        if os.path.isfile(p):
            with open(p) as f:
                return json.load(f)
    except Exception:
        pass
    return None


def detect_external_storage(exclude_devices: set[str]) -> list[dict]:
    """Enumerate removable / USB whole disks that are candidates for Arkive
    external storage. The system disk and the dedicated volume's disk are excluded
    so they can never be offered for (destructive) setup.

    Each entry reports stable identity (serial), size, the current filesystem /
    label / mount, whether it already carries any filesystem (so the UI can warn
    before reformatting), and — when one of its partitions is mounted — our marker
    (proving the drive is already ours and which store it is)."""
    exclude = {d for d in exclude_devices if d}
    out: list[dict] = []
    for d in lsblk_disks():
        if d.get("type") != "disk":
            continue
        path = d.get("path") or f"/dev/{d.get('name', '')}"
        if path in exclude:
            continue
        tran = (d.get("tran") or "").lower()
        hotplug = str(d.get("hotplug")).lower() in ("1", "true")
        removable = str(d.get("rm")).lower() in ("1", "true")
        if not (tran == "usb" or hotplug or removable):
            continue  # only external / removable media is ever a setup candidate
        kids = d.get("children") or []
        part = _first_data_partition(d)
        fstype = part.get("fstype") or d.get("fstype") or ""
        label = part.get("label") or d.get("label") or ""
        mount = (d.get("mountpoint")
                 or next((k.get("mountpoint") for k in kids if k.get("mountpoint")), ""))
        has_fs = bool(fstype) or any(k.get("fstype") for k in kids)
        marker = read_marker(mount) if mount else None
        out.append({
            "device": path,
            "serial": d.get("serial") or "",
            "model": (d.get("model") or "").strip(),
            "vendor": (d.get("vendor") or "").strip(),
            "size_bytes": int(d.get("size") or 0),
            "transport": tran or ("hotplug" if hotplug else "removable"),
            "fstype": fstype,
            "label": label,
            "mountpoint": mount or "",
            "has_filesystem": has_fs,
            "is_arkive": (label == ARKIVE_LABEL) or bool(marker),
            "store_id": (marker or {}).get("store_id"),
            "store_name": (marker or {}).get("name"),
        })
    return out
