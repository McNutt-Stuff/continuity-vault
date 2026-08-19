"""Native macOS status modal for the desktop agent (AppKit NSAlert).

Shown from the menu bar. Uses PyObjC (already present via rumps) to render a real
native modal. Returns which button was pressed so the caller can act (Sync).
"""

from __future__ import annotations

import time


def _ago(epoch: float) -> str:
    if not epoch:
        return "never"
    d = time.time() - epoch
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)}m ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    return f"{int(d // 86400)}d ago"


def _ago_iso(iso: str) -> str:
    if not iso:
        return "never"
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return _ago(dt.timestamp())
    except Exception:
        return "recently"


def _summary(snap: dict) -> str:
    t = snap.get("telemetry", {}) or {}
    crypto = t.get("crypto", {}) or {}
    hb = snap.get("last_heartbeat_epoch", 0) or 0
    online = bool(hb) and (time.time() - hb) < 120

    op_auth = t.get("op_auth")
    op_line = {
        "service-account": "1Password: service account",
        "interactive": "1Password: app integration",
        "unauthenticated": "1Password: interactive only",
        "absent": "1Password: CLI not installed",
    }.get(op_auth, f"1Password: {op_auth or 'unknown'}")

    pq = "ML-KEM / ML-DSA" if crypto.get("pq_available") else "classical fallback"
    escrow = crypto.get("recovery_escrow", "pending")

    fsi = t.get("fs_index", {}) or {}
    folders = fsi.get("folders", 0) or 0
    if folders:
        idx_line = f"File index:  {folders:,} folders · {_ago_iso(fsi.get('built_at'))}"
    elif fsi.get("building"):
        idx_line = "File index:  building…"
    else:
        idx_line = "File index:  not built yet"

    return "\n".join([
        f"Status:      {'Connected' if online else 'Reconnecting…'}",
        f"Version:     {snap.get('version', '—')}",
        f"Cloud:       {snap.get('cloud_url', '—')}",
        f"Heartbeat:   {_ago(hb)}",
        f"Collection:  {_ago(snap.get('last_collect_epoch', 0) or 0)}",
        idx_line,
        op_line,
        "",
        f"Encryption:  {crypto.get('content_alg', 'AES-256-GCM')} (client-side)",
        f"Quantum-safe:{pq}",
        f"Recovery:    {escrow}",
        "",
        f"Host:        {t.get('hostname', '—')}  ·  {t.get('local_ip', '—')}",
        f"User:        {t.get('local_user', '—')}",
        f"OS:          {t.get('os', '—')}",
    ])


def show(snap: dict) -> str:
    """Show the native modal. Returns 'sync' or 'close'."""
    import AppKit  # provided by pyobjc (rumps dependency)

    alert = AppKit.NSAlert.alloc().init()
    alert.setMessageText_(f"Arkive Agent — {snap.get('name', 'status')}")
    alert.setInformativeText_(_summary(snap))
    alert.setAlertStyle_(AppKit.NSAlertStyleInformational)
    # Use a security shield rather than the generic app/folder icon.
    try:
        icon = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "lock.shield.fill", "Arkive")
        if icon is not None:
            alert.setIcon_(icon)
    except Exception:
        pass

    alert.addButtonWithTitle_("Close")       # 1000
    alert.addButtonWithTitle_("Sync now")    # 1001

    resp = alert.runModal()
    if resp == AppKit.NSAlertSecondButtonReturn:
        return "sync"
    return "close"

