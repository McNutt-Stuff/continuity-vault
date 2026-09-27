"""Endpoint posture collector (macOS).

Gathers metadata-only device posture for the Signal Platform — encryption /
firewall / auto-update state and an application inventory. NEVER content: no
files, messages, browsing history, keystrokes or screen contents. Every probe is
best-effort with a short timeout; an undeterminable value is OMITTED (the cloud
provider treats an absent key as "unknown", which lowers coverage rather than
asserting a clean state). Cached so it doesn't run on every heartbeat.
"""

from __future__ import annotations

import glob
import os
import platform
import plistlib
import subprocess
import time

_CACHE: dict = {}
_CACHE_AT: float = 0.0
_TTL = 3600.0  # posture changes slowly; refresh at most hourly


def _run(cmd: list[str], timeout: float = 6.0) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return (r.stdout or "").strip()
        return (r.stdout or r.stderr or "").strip() or None
    except Exception:  # noqa: BLE001 — a probe must never crash the agent
        return None


def _filevault() -> bool | None:
    out = _run(["/usr/bin/fdesetup", "status"])
    if not out:
        return None
    low = out.lower()
    if "filevault is on" in low:
        return True
    if "filevault is off" in low:
        return False
    return None


def _firewall() -> bool | None:
    out = _run(["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"])
    if out:
        low = out.lower()
        if "enabled" in low:
            return True
        if "disabled" in low:
            return False
    # Fallback: the alf global state preference (0 off, 1 on, 2 block-all).
    out = _run(["/usr/bin/defaults", "read",
                "/Library/Preferences/com.apple.alf", "globalstate"])
    if out and out.strip().isdigit():
        return int(out.strip()) > 0
    return None


def _auto_update() -> bool | None:
    out = _run(["/usr/bin/defaults", "read",
                "/Library/Preferences/com.apple.SoftwareUpdate", "AutomaticCheckEnabled"])
    if out and out.strip() in ("0", "1"):
        return out.strip() == "1"
    return None


def _app_inventory(limit: int = 1500) -> list[dict]:
    apps: list[dict] = []
    seen: set[str] = set()
    for root in ("/Applications", os.path.expanduser("~/Applications"),
                 "/Applications/Utilities"):
        for bundle in glob.glob(os.path.join(root, "*.app")):
            name = os.path.basename(bundle)[:-4]
            if name in seen:
                continue
            seen.add(name)
            version = None
            bundle_id = None
            try:
                with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as fh:
                    info = plistlib.load(fh)
                version = info.get("CFBundleShortVersionString") or info.get("CFBundleVersion")
                bundle_id = info.get("CFBundleIdentifier")
            except Exception:  # noqa: BLE001
                pass
            apps.append({"name": name, "version": version, "bundle_id": bundle_id})
            if len(apps) >= limit:
                return apps
    return apps


def collect() -> dict:
    """Return posture keys present-only (macOS). Cached ~1h. Empty on other OSes."""
    global _CACHE, _CACHE_AT
    if platform.system() != "Darwin":
        return {}
    if _CACHE and (time.time() - _CACHE_AT) < _TTL:
        return _CACHE
    out: dict = {}
    fv = _filevault()
    if fv is not None:
        out["disk_encryption_enabled"] = fv
    fw = _firewall()
    if fw is not None:
        out["firewall_enabled"] = fw
    au = _auto_update()
    if au is not None:
        out["auto_update_enabled"] = au
    # Secure Boot is enforced on Apple Silicon; report true there (best-effort).
    try:
        if platform.machine() == "arm64":
            out["secure_boot_enabled"] = True
    except Exception:  # noqa: BLE001
        pass
    try:
        inv = _app_inventory()
        if inv:
            out["app_inventory"] = inv
    except Exception:  # noqa: BLE001
        pass
    _CACHE = out
    _CACHE_AT = time.time()
    return out
