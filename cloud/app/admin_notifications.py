"""
Admin (platform-operator) email notifications.

A small, extensible framework that alerts a selectable list of platform admins to
operationally significant platform events: platform health, software updates /
upgrades processing, node alerts, customer alerts, billing failures and new
customer signups.

Design mirrors the per-user ``notifications`` module but is *platform-scoped*:

* Types live in ``ADMIN_NOTIFICATION_TYPES`` (key, label, icon, default, severity).
* Config is stored in ``SystemSetting`` (JSON) and administered from the Admin UI:
  a master on/off, the selected recipient admins (or "all platform admins"), any
  extra always-notify addresses, and a per-type on/off map.
* ``emit(...)`` resolves recipients, renders the shared branded email
  (``emailer.render``), sends via the running node's mail service, and records an
  ``AdminNotificationLog`` row (which also powers dedupe so a persistent condition
  — e.g. an offline node — doesn't email on every sweep).

Emitting is always best-effort: a delivery/logging failure must never break the
caller (a signup, a billing charge, a health sweep).
"""

from __future__ import annotations

import html as _html
import json
import logging
import re
from datetime import datetime, timedelta, timezone

from . import emailer
from .models import AdminAlertEvent, AdminNotificationLog, SystemSetting, User

logger = logging.getLogger("cv.admin_notify")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# --------------------------------------------------------------------------- #
# Type catalog (extensible)                                                   #
# --------------------------------------------------------------------------- #

ADMIN_NOTIFICATION_TYPES = [
    {"key": "platform_health", "label": "Platform health", "icon": "activity",
     "default": True, "severity": "critical",
     "desc": "Node/appliance/storage health problems and other platform-health alerts."},
    {"key": "updates", "label": "Updates & upgrades", "icon": "sparkle",
     "default": True, "severity": "info",
     "desc": "Software update jobs being dispatched, completing, or failing."},
    {"key": "node_alert", "label": "Node alerts", "icon": "server",
     "default": True, "severity": "critical",
     "desc": "A control-plane / customer node going offline or coming back online."},
    {"key": "customer_alert", "label": "Customer alerts", "icon": "user",
     "default": True, "severity": "warning",
     "desc": "Per-customer problems worth an operator's attention (e.g. failing storage)."},
    {"key": "billing_failure", "label": "Billing failures", "icon": "credit-card",
     "default": True, "severity": "warning",
     "desc": "Failed charges, dunning escalations and cancelled-for-nonpayment accounts."},
    {"key": "new_signup", "label": "New customer signups", "icon": "user",
     "default": True, "severity": "info",
     "desc": "A new customer completes signup."},
]
_TYPE_KEYS = {t["key"] for t in ADMIN_NOTIFICATION_TYPES}
_TYPE_DEFAULT = {t["key"]: t["default"] for t in ADMIN_NOTIFICATION_TYPES}
_TYPE_BY_KEY = {t["key"]: t for t in ADMIN_NOTIFICATION_TYPES}


# --------------------------------------------------------------------------- #
# Settings (SystemSetting-backed)                                             #
# --------------------------------------------------------------------------- #

_S_ENABLED = "admin_notify.enabled"           # "1" / "0" master switch
_S_RECIPIENTS = "admin_notify.recipient_ids"  # JSON list of User.id (empty = all admins)
_S_EXTRA = "admin_notify.extra_emails"        # JSON list of always-notify addresses
_S_TYPES = "admin_notify.types"               # JSON {type_key: bool}


def _get(db, key: str, default: str = "") -> str:
    row = db.get(SystemSetting, key)
    return row.value if row and row.value not in (None, "") else default


def _set(db, key: str, value: str) -> None:
    row = db.get(SystemSetting, key)
    if row is None:
        db.add(SystemSetting(key=key, value=value))
    else:
        row.value = value


def _json_list(raw: str) -> list:
    try:
        v = json.loads(raw or "[]")
        return list(v) if isinstance(v, list) else []
    except (TypeError, ValueError):
        return []


def _json_dict(raw: str) -> dict:
    try:
        v = json.loads(raw or "{}")
        return dict(v) if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def get_config(db) -> dict:
    """Resolved admin-notification configuration."""
    enabled = _get(db, _S_ENABLED, "1") != "0"
    recipient_ids = [str(x) for x in _json_list(_get(db, _S_RECIPIENTS, "[]"))]
    extra = _sanitize_emails(_json_list(_get(db, _S_EXTRA, "[]")))
    types_raw = _json_dict(_get(db, _S_TYPES, "{}"))
    types = {t["key"]: bool(types_raw.get(t["key"], t["default"])) for t in ADMIN_NOTIFICATION_TYPES}
    return {"enabled": enabled, "recipient_ids": recipient_ids,
            "extra_emails": extra, "types": types}


def save_config(db, *, enabled: bool | None = None, recipient_ids=None,
                extra_emails=None, types=None) -> dict:
    if enabled is not None:
        _set(db, _S_ENABLED, "1" if enabled else "0")
    if recipient_ids is not None:
        ids = [str(x) for x in recipient_ids if str(x).strip()]
        _set(db, _S_RECIPIENTS, json.dumps(ids))
    if extra_emails is not None:
        _set(db, _S_EXTRA, json.dumps(_sanitize_emails(extra_emails)))
    if types is not None:
        clean = {t["key"]: bool(types.get(t["key"], t["default"])) for t in ADMIN_NOTIFICATION_TYPES}
        _set(db, _S_TYPES, json.dumps(clean))
    db.commit()
    return get_config(db)


def _sanitize_emails(raw) -> list[str]:
    out: list[str] = []
    for item in (raw or []):
        addr = str(item or "").strip().lower()
        if addr and _EMAIL_RE.match(addr) and addr not in out:
            out.append(addr)
        if len(out) >= 25:
            break
    return out


# --------------------------------------------------------------------------- #
# Recipients                                                                  #
# --------------------------------------------------------------------------- #

def available_admins(db) -> list[dict]:
    """Platform admins that can be selected as recipients (from the local DB)."""
    admins = (db.query(User)
              .filter(User.is_platform_admin.is_(True), User.status == "active")
              .order_by(User.email).all())
    out = []
    for u in admins:
        if not (u.email or "").strip():
            continue
        out.append({"id": u.id, "email": u.email,
                    "name": (u.display_name or u.email)})
    return out


def resolved_recipients(db, config: dict | None = None) -> list[str]:
    """Email addresses to notify: the selected admins (or ALL platform admins when
    none are selected) plus any always-notify extra addresses. Deduped."""
    config = config or get_config(db)
    admins = available_admins(db)
    sel = set(config.get("recipient_ids") or [])
    chosen = [a for a in admins if a["id"] in sel] if sel else admins
    out: list[str] = []
    for a in chosen:
        addr = (a["email"] or "").strip().lower()
        if addr and addr not in out:
            out.append(addr)
    for addr in config.get("extra_emails") or []:
        if addr not in out:
            out.append(addr)
    return out


def type_enabled(db, key: str, config: dict | None = None) -> bool:
    config = config or get_config(db)
    return bool(config.get("types", {}).get(key, _TYPE_DEFAULT.get(key, True)))


# --------------------------------------------------------------------------- #
# Dispatch                                                                    #
# --------------------------------------------------------------------------- #

def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _recently_sent(db, key: str, dedupe_key: str, within_hours: int) -> bool:
    if not dedupe_key or within_hours <= 0:
        return False
    cutoff = _now() - timedelta(hours=within_hours)
    return db.query(AdminNotificationLog.id).filter(
        AdminNotificationLog.type == key,
        AdminNotificationLog.dedupe_key == dedupe_key,
        AdminNotificationLog.ok.is_(True),
        AdminNotificationLog.created_at >= cutoff).first() is not None


def _sev_badge(severity: str) -> str:
    color = {"critical": "#c0392b", "warning": "#b8860b"}.get(severity, "#3a6df0")
    label = {"critical": "Critical", "warning": "Warning"}.get(severity, "Info")
    return (f'<span style="display:inline-block;padding:2px 10px;border-radius:999px;'
            f'background:{color};color:#fff;font-size:11px;font-weight:700;'
            f'letter-spacing:.3px;">{label}</span>')


def emit(db, key: str, *, subject: str, title: str, intro: str = "",
         rows: list[dict] | None = None, body_html: str = "",
         severity: str | None = None, cta: dict | None = None,
         dedupe_key: str = "", dedupe_within_hours: int = 0,
         force: bool = False) -> bool:
    """Build + send one admin alert if the framework and this type are enabled.

    Best-effort: never raises. Returns True if at least one email was sent.

    * ``rows`` — [{icon,name,detail}] rendered as a clean list (reuses the user
      notification renderer for a consistent look).
    * ``body_html`` — additional trusted HTML appended after the rows.
    * ``dedupe_key`` + ``dedupe_within_hours`` — suppress a repeat of the same
      condition within the window (e.g. an offline node re-detected each sweep).
    """
    try:
        if key not in _TYPE_KEYS:
            logger.warning("admin-notify skipped: unknown type %r", key)
            return False
        config = get_config(db)
        if not force and not config["enabled"]:
            return False
        if not force and not type_enabled(db, key, config):
            return False
        if _recently_sent(db, key, dedupe_key, dedupe_within_hours):
            logger.info("admin-notify %s suppressed (dedupe=%s within %dh)",
                        key, dedupe_key, dedupe_within_hours)
            return False
        recipients = resolved_recipients(db, config)
        if not recipients:
            logger.info("admin-notify %s skipped: no recipients configured", key)
            return False

        sev = severity or _TYPE_BY_KEY[key]["severity"]
        from .notifications import _rows as _render_rows  # consistent list styling
        parts = [f'<p style="margin:0 0 14px;">{_sev_badge(sev)}</p>']
        if intro:
            parts.append(f'<p style="margin:0 0 12px;">{_html.escape(intro)}</p>')
        if rows:
            parts.append(_render_rows(rows))
        if body_html:
            parts.append(body_html)
        html = emailer.render(title, "".join(parts), cta=cta,
                              preheader=intro or title,
                              footer_note="You're receiving this because you're a "
                                          "platform admin with Admin Notifications enabled. "
                                          "Manage recipients and types in Admin → Notifications.")
        text = f"{title}\n\n{intro}".strip()
        sent_any = False
        ok_all = True
        for to_email in recipients:
            channel = emailer.send(to_email, subject, html=html, text=text,
                                   category=f"admin-notify:{key}")
            ok = channel in ("ses", "smtp", "log")
            ok_all = ok_all and ok
            sent_any = True
            logger.info("admin-notify %s -> %s via %s (subject=%r)", key, to_email, channel, subject)

        # Record on an ISOLATED session so we never commit/rollback the caller's
        # transaction (e.g. an in-flight billing dunning update).
        try:
            from .db import WorkerSessionLocal
            with WorkerSessionLocal() as s:
                s.add(AdminNotificationLog(type=key, severity=sev, dedupe_key=dedupe_key,
                                           subject=subject, summary=(intro or title)[:2000],
                                           recipients=recipients, ok=ok_all))
                s.commit()
        except Exception:  # noqa: BLE001
            logger.exception("admin-notify %s log write failed", key)
        return sent_any
    except Exception:  # noqa: BLE001 — emitting must never break the caller
        logger.exception("admin-notify %s failed to send", key)
        return False


def _is_federated_node() -> bool:
    """True when running on a federated customer-tenant node (not the control
    plane / a standalone deployment). Mirrors the worker-start logic in main.py."""
    from .config import get_settings
    s = get_settings()
    role = s.node_role or "control-plane"
    return bool(getattr(s, "node_sync_scope", "")) and role != "control-plane"


def raise_alert(db, key: str, *, subject: str, title: str, intro: str = "",
                rows: list[dict] | None = None, cta: dict | None = None,
                severity: str | None = None, dedupe_key: str = "",
                dedupe_within_hours: int = 0, tenant_id: str | None = None) -> bool:
    """Federation-aware entry point for events detected where a resource lives.

    On the control plane (or a standalone deployment) this emails platform admins
    immediately. On a federated customer-tenant node — where platform admins and
    the authoritative mail service are NOT local — it records an ``AdminAlertEvent``
    that ``workers/node_replication`` pushes to the control plane, which then emits
    it. Best-effort: never raises.
    """
    try:
        if not _is_federated_node():
            return emit(db, key, subject=subject, title=title, intro=intro, rows=rows,
                        cta=cta, severity=severity, dedupe_key=dedupe_key,
                        dedupe_within_hours=dedupe_within_hours)
        sev = severity or _TYPE_BY_KEY.get(key, {}).get("severity", "info")
        from .config import get_settings
        from .db import WorkerSessionLocal
        s = get_settings()
        origin = (s.node_name or s.domain or "").strip()
        with WorkerSessionLocal() as ses:
            ses.add(AdminAlertEvent(type=key, severity=sev, subject=subject, title=title,
                                    intro=intro, rows=rows or [], cta=cta or {},
                                    dedupe_key=dedupe_key,
                                    dedupe_within_hours=int(dedupe_within_hours or 0),
                                    tenant_id=tenant_id, origin_node=origin))
            ses.commit()
        logger.info("admin-alert %s queued for control plane (node=%s)", key, origin)
        return True
    except Exception:  # noqa: BLE001 — raising an alert must never break the caller
        logger.exception("admin-alert %s could not be raised", key)
        return False


def emit_pushed(db, event: dict) -> bool:
    """Control-plane handler for a node-pushed ``AdminAlertEvent`` — emit it to the
    platform admins here. Dedupe is enforced by ``emit`` via AdminNotificationLog."""
    key = event.get("type") or ""
    return emit(
        db, key,
        subject=event.get("subject") or "Arkive platform alert",
        title=event.get("title") or "Platform alert",
        intro=event.get("intro") or "",
        rows=event.get("rows") or [],
        cta=event.get("cta") or None,
        severity=event.get("severity"),
        dedupe_key=event.get("dedupe_key") or "",
        dedupe_within_hours=int(event.get("dedupe_within_hours") or 0))


def recent_log(db, limit: int = 50) -> list[dict]:
    rows = (db.query(AdminNotificationLog)
            .order_by(AdminNotificationLog.created_at.desc())
            .limit(min(200, max(1, limit))).all())
    return [{"id": r.id, "type": r.type, "severity": r.severity,
             "subject": r.subject, "summary": r.summary,
             "recipients": r.recipients or [], "ok": bool(r.ok),
             "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in rows]


def send_test(db, key: str) -> dict:
    """Force-send a representative sample of one admin alert type (ignores the
    per-type enable + dedupe) so an operator can confirm delivery."""
    if key not in _TYPE_KEYS:
        return {"ok": False, "message": "Unknown notification type."}
    recipients = resolved_recipients(db)
    if not recipients:
        return {"ok": False, "message": "No recipients configured. Select at least one "
                                        "admin or add an extra address first."}
    samples = {
        "new_signup": dict(subject="[Arkive] New signup — Sample Customer",
                           title="New customer signup",
                           intro="A new customer just completed signup (sample).",
                           rows=[{"icon": "user", "name": "Sample Customer", "detail": "Family plan"},
                                 {"icon": "activity", "name": "Region", "detail": "nam-east"}]),
        "billing_failure": dict(subject="[Arkive] Billing failure — Sample Customer",
                                title="Billing charge failed",
                                intro="A recurring charge failed (sample).",
                                rows=[{"icon": "credit-card", "name": "Sample Customer", "detail": "$24.00 declined"},
                                      {"icon": "alert", "name": "Attempt", "detail": "1 of 3"}]),
        "node_alert": dict(subject="[Arkive] Node offline — sample-node-1",
                           title="Node offline",
                           intro="A node stopped sending heartbeats (sample).",
                           rows=[{"icon": "server", "name": "sample-node-1", "detail": "offline 6m"}]),
        "customer_alert": dict(subject="[Arkive] Customer alert — Sample Customer",
                               title="Customer storage failing",
                               intro="A customer's storage failed its health probe (sample).",
                               rows=[{"icon": "alert", "name": "Sample Customer", "detail": "S3 access denied"}]),
        "platform_health": dict(subject="[Arkive] Platform health — appliance problem",
                                title="Platform health alert",
                                intro="An appliance reported a health problem (sample).",
                                rows=[{"icon": "appliance", "name": "Vault-01", "detail": "drive SMART warning"}]),
        "updates": dict(subject="[Arkive] Update dispatched — appliance 1.4.2",
                        title="Update dispatched",
                        intro="A software update was dispatched (sample).",
                        rows=[{"icon": "sparkle", "name": "appliance → 1.4.2", "detail": "3 targets"}]),
    }
    s = samples.get(key, dict(subject=f"[Arkive] Test — {key}", title="Admin notification test",
                              intro="This is a test admin notification.", rows=[]))
    ok = emit(db, key, force=True, severity=_TYPE_BY_KEY[key]["severity"], **s)
    return {"ok": ok, "recipients": recipients,
            "message": "Test alert sent." if ok else "Could not send the test alert."}
