"""Native macOS status modal for the desktop agent (AppKit NSAlert).

Shown from the menu bar. Uses PyObjC (already present via rumps) to render a real
native modal with a scrollable log accessory — no web page/browser involved.
Returns which button was pressed so the caller can act (Sync / Open logs).
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

    return "\n".join([
        f"Status:      {'Connected' if online else 'Reconnecting…'}",
        f"Version:     {snap.get('version', '—')}",
        f"Cloud:       {snap.get('cloud_url', '—')}",
        f"Heartbeat:   {_ago(hb)}",
        f"Collection:  {_ago(snap.get('last_collect_epoch', 0) or 0)}",
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
    """Show the native modal. Returns 'sync', 'logs', or 'close'."""
    import AppKit  # provided by pyobjc (rumps dependency)

    alert = AppKit.NSAlert.alloc().init()
    alert.setMessageText_(f"Arkive Agent — {snap.get('name', 'status')}")
    alert.setInformativeText_(_summary(snap))
    alert.setAlertStyle_(AppKit.NSAlertStyleInformational)

    logs = "\n".join((snap.get("telemetry", {}) or {}).get("recent_logs", []) or []) \
        or "No logs yet."
    text = AppKit.NSTextView.alloc().initWithFrame_(((0, 0), (460, 190)))
    text.setEditable_(False)
    text.setDrawsBackground_(True)
    text.setFont_(AppKit.NSFont.userFixedPitchFontOfSize_(10.5))
    text.setString_(logs)
    text.scrollRangeToVisible_((len(logs), 0))  # pyobjc bridges the 2-tuple to NSRange
    scroll = AppKit.NSScrollView.alloc().initWithFrame_(((0, 0), (460, 190)))
    scroll.setDocumentView_(text)
    scroll.setHasVerticalScroller_(True)
    scroll.setBorderType_(AppKit.NSBezelBorder)
    alert.setAccessoryView_(scroll)

    alert.addButtonWithTitle_("Close")       # 1000
    alert.addButtonWithTitle_("Sync now")    # 1001
    alert.addButtonWithTitle_("Open logs")   # 1002

    resp = alert.runModal()
    if resp == AppKit.NSAlertSecondButtonReturn:
        return "sync"
    if resp == AppKit.NSAlertThirdButtonReturn:
        return "logs"
    return "close"
