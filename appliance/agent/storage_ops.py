"""
Destructive disk operations for Arkive external storage onboarding.

Isolated from :mod:`sysinfo` (read-only telemetry) because everything here can
DESTROY data. Every entry point re-resolves the target device by its stable
serial and refuses to touch anything that isn't a removable/USB candidate — the
system disk and the dedicated volume can never be selected. Formatting only ever
happens in response to an explicit, cloud-signed SETUP_STORAGE command that the
user confirmed in the portal.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from . import sysinfo

MARKER = sysinfo.ARKIVE_MARKER
LABEL = sysinfo.ARKIVE_LABEL


class StorageOpError(RuntimeError):
    """A storage setup/mount operation failed (message is user-safe)."""


def _run(cmd: list[str], timeout: float = 120.0) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.returncode == 0, (r.stdout + r.stderr).strip())
    except Exception as exc:  # noqa: BLE001
        return (False, str(exc))


def _guard_target(device: str, serial: str, dedicated_path: str) -> str:
    """Re-resolve the device by serial and prove it is a safe external target.

    Returns the resolved whole-disk path or raises. This is the single choke point
    that prevents ever partitioning the system disk or dedicated volume even if the
    kernel reassigned /dev names between detection and setup."""
    if not serial:
        raise StorageOpError("refusing to format a device with no stable serial")
    resolved = sysinfo.resolve_device_by_serial(serial)
    if not resolved:
        raise StorageOpError("the target drive is no longer connected")
    if device and device != resolved:
        # Trust the serial, not the (possibly stale) /dev name from the portal.
        device = resolved
    system = sysinfo.system_disk_device()
    dedicated = sysinfo.dedicated_disk_device(dedicated_path)
    if resolved in {system, dedicated}:
        raise StorageOpError("refusing to touch the system or dedicated disk")
    candidates = {d["device"]: d for d in
                  sysinfo.detect_external_storage({system, dedicated})}
    if resolved not in candidates:
        raise StorageOpError("target is not a removable/USB external drive")
    return resolved


def _partition_path(device: str) -> str:
    """First partition node for a whole disk (sdb -> sdb1, nvme0n1 -> nvme0n1p1)."""
    base = device.rsplit("/", 1)[-1]
    sep = "p" if base and base[-1].isdigit() else ""
    return f"{device}{sep}1"


def _settle() -> None:
    _run(["udevadm", "settle"], timeout=15)
    time.sleep(1.0)


def setup_device(*, device: str, serial: str, store_id: str, name: str,
                 mount_base: str, dedicated_path: str, mirror_of_id: Optional[str] = None,
                 kind: str = "external") -> dict:
    """Partition, format (ext4, label ARKIVE), mount, and mark an external drive.

    DESTRUCTIVE: wipes the target disk. Guarded by :func:`_guard_target`. Returns a
    dict describing the ready store (mountpoint, capacity, serial, marker)."""
    resolved = _guard_target(device, serial, dedicated_path)

    # 1) Wipe any existing signatures + write a fresh single-partition GPT.
    ok, out = _run(["wipefs", "-a", resolved])
    if not ok:
        raise StorageOpError(f"could not clear the drive: {out[:200]}")
    ok, out = _run(["parted", "-s", resolved, "mklabel", "gpt"])
    if not ok:
        raise StorageOpError(f"could not create a partition table: {out[:200]}")
    ok, out = _run(["parted", "-s", "-a", "optimal", resolved,
                    "mkpart", "primary", "ext4", "0%", "100%"])
    if not ok:
        raise StorageOpError(f"could not create a partition: {out[:200]}")
    _settle()

    # 2) Make the filesystem with our label so the drive is recognisable as ours.
    part = _partition_path(resolved)
    ok, out = _run(["mkfs.ext4", "-F", "-L", LABEL, part])
    if not ok:
        raise StorageOpError(f"could not format the drive: {out[:200]}")

    # 3) Mount at a stable per-store path and write the ownership marker.
    mountpoint = os.path.join(mount_base, store_id)
    return _mount_and_mark(part=part, serial=serial, store_id=store_id, name=name,
                           mountpoint=mountpoint, mirror_of_id=mirror_of_id, kind=kind)


def mount_known(*, serial: str, store_id: str, name: str, mount_base: str,
                mirror_of_id: Optional[str] = None, kind: str = "external") -> Optional[dict]:
    """Mount an already-set-up Arkive drive (by serial) without touching data.

    Returns the ready-store dict, or None when the drive isn't currently present."""
    resolved = sysinfo.resolve_device_by_serial(serial)
    if not resolved:
        return None
    part = _partition_path(resolved)
    if not os.path.exists(part):
        part = resolved  # some drives are formatted whole-disk
    mountpoint = os.path.join(mount_base, store_id)
    return _mount_and_mark(part=part, serial=serial, store_id=store_id, name=name,
                           mountpoint=mountpoint, mirror_of_id=mirror_of_id, kind=kind,
                           rewrite_marker=False)


def _mount_and_mark(*, part: str, serial: str, store_id: str, name: str,
                    mountpoint: str, mirror_of_id: Optional[str], kind: str,
                    rewrite_marker: bool = True) -> dict:
    Path(mountpoint).mkdir(parents=True, exist_ok=True)
    if not os.path.ismount(mountpoint):
        ok, out = _run(["mount", part, mountpoint], timeout=30)
        if not ok:
            raise StorageOpError(f"could not mount the drive: {out[:200]}")
    marker_path = os.path.join(mountpoint, MARKER)
    if rewrite_marker or not os.path.isfile(marker_path):
        marker = {"store_id": store_id, "name": name, "kind": kind,
                  "mirror_of_id": mirror_of_id, "serial": serial,
                  "written_at": int(time.time())}
        try:
            with open(marker_path, "w") as f:
                json.dump(marker, f)
        except Exception as exc:  # noqa: BLE001
            raise StorageOpError(f"could not write the ownership marker: {exc}")
    total = used = 0
    try:
        st = os.statvfs(mountpoint)
        total = st.f_blocks * st.f_frsize
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
    except Exception:
        pass
    return {"store_id": store_id, "name": name, "kind": kind, "serial": serial,
            "mirror_of_id": mirror_of_id, "mountpoint": mountpoint,
            "device": part, "capacity_bytes": total, "used_bytes": used}


def unmount(mountpoint: str) -> None:
    """Best-effort unmount of a store's mountpoint (data is left intact)."""
    if mountpoint and os.path.ismount(mountpoint):
        _run(["umount", mountpoint], timeout=30)
