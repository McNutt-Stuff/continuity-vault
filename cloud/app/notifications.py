"""
User email notifications.

A small, extensible framework: every notification is a *type* in
``NOTIFICATION_TYPES`` (key, label, icon, default, scope). Users and admins toggle
types via ``User.notification_prefs``; ``is_enabled`` resolves a user's effective
preference. Builders render professional, branded HTML (via ``emailer.render``)
with consistent iconography, and ``send_notification`` handles preference checks,
delivery and logging (``NotificationLog`` also powers once-a-day rate limiting).

Because ``emailer`` already resolves the *running node's* email service, calling
these from the node that manages a tenant sends via that node's mail service.
"""

from __future__ import annotations

import html as _html
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_

from . import emailer
from .models import (Collection, ConnectorAccount, IntegrationInstance, IntegrationRun,
                     NotificationLog, SearchDocument, SnapshotReceipt, SystemSetting,
                     Tenant, User, Vault)

logger = logging.getLogger("cv.notify")

# --------------------------------------------------------------------------- #
# Type catalog (extensible)                                                   #
# --------------------------------------------------------------------------- #

NOTIFICATION_TYPES = [
    {"key": "daily_summary", "label": "Daily backup summary", "icon": "grid",
     "default": True, "scope": "user",
     "desc": "A daily digest of what was protected — sources, data, destinations and any issues."},
    {"key": "source_problem", "label": "Source problem alerts", "icon": "alert",
     "default": True, "scope": "user",
     "desc": "Get notified when one of your connected sources needs attention."},
    {"key": "appliance_problem", "label": "Appliance health alerts", "icon": "server",
     "default": True, "scope": "user",
     "desc": "Get notified when an appliance goes offline, a drive or storage fails, capacity runs low, the network is slow, or an intrusion is detected."},
    {"key": "storage_problem", "label": "Storage destination alerts", "icon": "database",
     "default": True, "scope": "user",
     "desc": "Get notified when one of your cloud storage destinations can't be reached, written, or read for recovery."},
    {"key": "plan_change", "label": "Plan & billing changes", "icon": "credit-card",
     "default": True, "scope": "user",
     "desc": "A confirmation whenever your plan, storage or appliances change."},
    {"key": "weekly_org", "label": "Weekly organization summary", "icon": "activity",
     "default": True, "scope": "org",
     "desc": "A weekly roll-up of your organization's protection, by member and source."},
]
_TYPE_DEFAULT = {t["key"]: t["default"] for t in NOTIFICATION_TYPES}
_TYPE_KEYS = {t["key"] for t in NOTIFICATION_TYPES}


def is_enabled(user: User, key: str) -> bool:
    prefs = getattr(user, "notification_prefs", None) or {}
    return bool(prefs.get(key, _TYPE_DEFAULT.get(key, True)))


def normalized_prefs(user: User) -> dict:
    """Every type resolved to an explicit on/off for the settings UI."""
    prefs = getattr(user, "notification_prefs", None) or {}
    return {t["key"]: bool(prefs.get(t["key"], t["default"])) for t in NOTIFICATION_TYPES}


# --------------------------------------------------------------------------- #
# Additional notification recipients                                          #
# --------------------------------------------------------------------------- #

MAX_NOTIFICATION_EMAILS = 5
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def sanitize_emails(raw) -> list[str]:
    """Normalize a list of extra recipient addresses: lowercase, trim, drop
    invalid/duplicate entries, cap the count. No global-uniqueness check — a
    notification address may match another account's login email."""
    out: list[str] = []
    for item in (raw or []):
        addr = str(item or "").strip().lower()
        if addr and _EMAIL_RE.match(addr) and addr not in out:
            out.append(addr)
        if len(out) >= MAX_NOTIFICATION_EMAILS:
            break
    return out


def normalized_emails(user: User) -> list[str]:
    return sanitize_emails(getattr(user, "notification_emails", None) or [])


def _recipients(user: User) -> list[str]:
    """Primary login email plus any additional notification addresses (deduped;
    the primary may not appear in the extras)."""
    out: list[str] = []
    primary = (user.email or "").strip().lower()
    if primary:
        out.append(primary)
    for addr in normalized_emails(user):
        if addr not in out:
            out.append(addr)
    return out


# --------------------------------------------------------------------------- #
# Admin-controlled settings (SystemSetting)                                   #
# --------------------------------------------------------------------------- #

def _setting(db, key: str, default: str = "") -> str:
    row = db.get(SystemSetting, key)
    return row.value if row and row.value not in (None, "") else default


def source_repeat_hours(db) -> int:
    # A configuration profile can override the admin SystemSetting on this node.
    from . import node_config
    v = node_config.get(db, "notif.source_repeat_hours")
    if v is None:
        v = _setting(db, "notif.source_repeat_hours", "24")
    try:
        return max(1, int(v))
    except (TypeError, ValueError):
        return 24


def _int_setting(db, key: str, default: int) -> int:
    from . import node_config
    v = node_config.get(db, key)
    if v is None:
        v = _setting(db, key, str(default))
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def appliance_alert_minutes(db) -> int:
    """How long an appliance may miss heartbeats before it's flagged offline."""
    return max(5, _int_setting(db, "notif.appliance_offline_minutes", 30))


def slow_latency_ms(db) -> int:
    """Cloud round-trip latency (ms) above which an appliance's link is 'slow'."""
    return max(250, _int_setting(db, "notif.slow_latency_ms", 1500))


def breach_attempt_threshold(db) -> int:
    """Blocked LAN intrusion/auth attempts in the window that warrant an alert."""
    return max(1, _int_setting(db, "notif.breach_attempts", 20))


def enabled_insights(db) -> list[str]:
    """Which insight cards the admin allows in the daily summary (comma list).
    A configuration profile can override the SystemSetting on this node."""
    from . import node_config
    v = node_config.get(db, "notif.enabled_insights")
    if v is not None:
        return node_config.get_list(db, "notif.enabled_insights")
    raw = _setting(db, "notif.enabled_insights", "footprint")
    return [x.strip() for x in raw.split(",") if x.strip()]


# --------------------------------------------------------------------------- #
# HTML helpers — professional, email-client-safe                              #
# --------------------------------------------------------------------------- #

# Emoji chips render reliably across every email client (unlike inline SVG, which
# Gmail strips). Mapped to the same concepts as the portal icons.
_ICON = {
    "grid": "🗂️", "source": "🔗", "link": "🔗", "cloud": "☁️", "appliance": "🔒",
    "server": "🔒", "data": "📦", "object": "🧩", "alert": "⚠️", "warn": "⚠️",
    "ok": "✅", "check": "✅", "storage": "💾", "database": "💾", "insight": "💡",
    "credit-card": "💳", "calendar": "📅", "user": "👤", "activity": "📈",
    "clock": "🕒", "email": "✉️", "mail": "✉️", "shield": "🛡️", "sparkle": "✨",
}


def _ic(name: str) -> str:
    return _ICON.get(name, "•")


# Source types with a brand icon synced into the portal (web/public/source-icons).
# Keep in sync with scripts/sync_source_icons.py SOURCE_ICONS.
_BRAND_ICON_TYPES = {
    "gmail", "onepassword", "outlook", "onedrive", "dropbox", "icloud",
    "google_drive", "slack", "notion", "github", "reddit", "facebook",
    "instagram", "linkedin", "evernote", "google_calendar", "google_contacts",
    "google_photos", "imessage", "ubiquiti", "aws", "azure", "gcp",
}


def _source_icon_url(source_type: str) -> str:
    """Absolute URL to the source's brand icon on the portal — the SAME asset the
    UI renders. Empty when there's no synced brand icon for the type."""
    if source_type in _BRAND_ICON_TYPES:
        return f"{_portal_url()}/source-icons/{source_type}.svg"
    return ""


def _esc(s) -> str:
    return _html.escape(str(s if s is not None else ""))


def _fmt_bytes(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or unit == "PB":
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return "0 B"


def _stat_grid(items: list[dict]) -> str:
    """A row of stat cards: [{icon,label,value}]."""
    if not items:
        return ""
    cells = "".join(
        f'<td width="{100 // max(1, len(items))}%" valign="top" '
        f'style="padding:6px;"><table role="presentation" width="100%" '
        f'style="border:1px solid #e6eaf2;border-radius:12px;background:#f9fbff;"><tr>'
        f'<td style="padding:12px 10px;text-align:center;">'
        f'<div style="font-size:20px;line-height:1;margin-bottom:4px;">{_ic(i["icon"])}</div>'
        f'<div style="font-size:19px;font-weight:700;color:#0b1120;">{_esc(i["value"])}</div>'
        f'<div style="font-size:11.5px;color:#8a94a7;margin-top:2px;">{_esc(i["label"])}</div>'
        f'</td></tr></table></td>'
        for i in items)
    return f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr>{cells}</tr></table>'


def _rows(items: list[dict]) -> str:
    """A simple list: [{icon,name,detail}] — an item may carry `icon_url` (brand
    logo <img>, same asset as the UI) which takes precedence over the emoji."""
    if not items:
        return ""

    def _cell(i: dict) -> str:
        url = i.get("icon_url")
        if url:
            return (f'<img src="{_esc(url)}" width="18" height="18" alt="" '
                    f'style="display:inline-block;width:18px;height:18px;'
                    f'border-radius:4px;vertical-align:middle;" />')
        return _ic(i.get("icon", "object"))

    body = "".join(
        f'<tr>'
        f'<td width="26" style="padding:9px 0;border-bottom:1px solid #eef1f7;font-size:16px;">{_cell(i)}</td>'
        f'<td style="padding:9px 0;border-bottom:1px solid #eef1f7;font-size:14px;color:#1a2234;">{_esc(i["name"])}</td>'
        f'<td style="padding:9px 0;border-bottom:1px solid #eef1f7;font-size:13px;color:#8a94a7;text-align:right;">{_esc(i.get("detail",""))}</td>'
        f'</tr>'
        for i in items)
    return f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0">{body}</table>'


def _section(title: str, icon: str = "") -> str:
    tag = f"{_ic(icon)} " if icon else ""
    return (f'<h2 style="font-size:14px;letter-spacing:.2px;color:#0b1120;'
            f'margin:24px 0 6px;font-weight:700;">{tag}{_esc(title)}</h2>')


def _warnbox(title: str, items: list[str]) -> str:
    lis = "".join(f'<li style="margin:4px 0;">{_esc(x)}</li>' for x in items)
    return (f'<div style="margin:16px 0;padding:12px 15px;border:1px solid #f3cfa2;'
            f'background:#fff7ec;border-radius:10px;color:#8a5a1a;font-size:13.5px;line-height:1.55;">'
            f'<b>{_ic("alert")} {_esc(title)}</b>'
            f'<ul style="margin:8px 0 0;padding-left:20px;">{lis}</ul></div>')


def _price_table(rows: list[dict], total_label: str, total: str) -> str:
    body = "".join(
        f'<tr><td style="padding:9px 0;border-bottom:1px solid #eef1f7;font-size:14px;color:#1a2234;">{_esc(r["label"])}</td>'
        f'<td style="padding:9px 0;border-bottom:1px solid #eef1f7;font-size:14px;color:#1a2234;text-align:right;">{_esc(r["amount"])}</td></tr>'
        for r in rows)
    return (f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0">{body}'
            f'<tr><td style="padding:11px 0;font-size:15px;font-weight:700;color:#0b1120;">{_esc(total_label)}</td>'
            f'<td style="padding:11px 0;font-size:15px;font-weight:700;color:#0b1120;text-align:right;">{_esc(total)}</td></tr>'
            f'</table>')


def _portal_url() -> str:
    from .config import get_settings
    s = get_settings()
    base = (getattr(s, "rp_origin", "") or "").rstrip("/")
    return base or f"https://{getattr(s, 'domain', 'vault.arkive.life')}"


# --------------------------------------------------------------------------- #
# Data helpers                                                                #
# --------------------------------------------------------------------------- #

def _day_start() -> datetime:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _user_vault_ids(db, user: User) -> list[str]:
    return [v for (v,) in db.query(Vault.id).filter(Vault.owner_user_id == user.id).all()]


def _tenant_vault_owner(db, tenant_id: str) -> dict:
    return {v: o for v, o in db.query(Vault.id, Vault.owner_user_id)
            .filter(Vault.tenant_id == tenant_id).all()}


def _dest_label(destination: str) -> tuple[str, str]:
    d = destination or ""
    if d == "cv-cloud":
        return "Arkive Cloud", "cloud"
    if d == "customer-s3" or d.startswith("byos:"):
        return "Your cloud storage", "cloud"
    if d.startswith("store:") or d.startswith("appliance"):
        return "Secure appliance", "appliance"
    return d or "Storage", "storage"


def _likely_reauth(msg: str) -> bool:
    s = (msg or "").lower()
    return any(t in s for t in ("401", "403", "unauthorized", "forbidden", "invalid token",
                                "invalid_grant", "reauth", "expired token", "token expired"))


def _source_issues(db, user: User) -> list[dict]:
    """Connector and integration issues requiring user attention.

    These feed the source-problem notification and daily-summary warnings so a
    failure is surfaced to users whether it originated in a source connector or
    an appliance integration run/provisioning flow.
    """
    my_colls = (db.query(Collection.connector_account_id)
                .filter(Collection.tenant_id == user.tenant_id,
                        Collection.vault_id.in_(_user_vault_ids(db, user) or ["-"])).all())
    acct_ids = {a for (a,) in my_colls if a}
    out = []
    if acct_ids:
        for a in (db.query(ConnectorAccount)
                  .filter(ConnectorAccount.id.in_(acct_ids),
                          ConnectorAccount.active.is_(True),
                          or_(ConnectorAccount.last_error.isnot(None),
                              ConnectorAccount.auth_status == "needs-reauth")).all()):
            msg = (a.last_error or "Authorization required — please reconnect.")
            out.append({"id": a.id,
                        "kind": "connector",
                        "name": a.account_label or a.connector_type,
                        "source_type": a.connector_type,
                        "error": msg.splitlines()[0][:160],
                        "at": a.last_error_at,
                        "fails": int(a.fail_count or 0),
                        "reauth": a.auth_status == "needs-reauth" or _likely_reauth(msg)})

    iq = (db.query(IntegrationInstance)
          .filter(IntegrationInstance.tenant_id == user.tenant_id,
                  IntegrationInstance.enabled.is_(True),
                  or_(IntegrationInstance.owner_user_id == user.id,
                      IntegrationInstance.owner_user_id.is_(None)),
                  or_(IntegrationInstance.status == "error",
                      IntegrationInstance.last_error.isnot(None),
                      IntegrationInstance.provision_state == "error")).all())
    for inst in iq:
        msg = (inst.last_error or inst.provision_message
               or "Integration requires attention. Please reconnect or retry setup.")
        runs = (db.query(IntegrationRun.status)
                .filter(IntegrationRun.integration_id == inst.id)
                .order_by(IntegrationRun.created_at.desc()).limit(12).all())
        fails = 0
        for (st,) in runs:
            if st == "error":
                fails += 1
            else:
                break
        if fails == 0 and inst.status == "error":
            fails = 1
        out.append({"id": inst.id,
                    "kind": "integration",
                    "name": inst.label or _source_name(inst.integration_type),
                    "source_type": inst.integration_type,
                    "error": msg.splitlines()[0][:160],
                    "at": inst.last_run_at or inst.updated_at,
                    "fails": fails,
                    "reauth": _likely_reauth(msg)})

    # Reachability: an enabled integration that isn't erroring but has stopped
    # producing fresh data — its LAN controller/endpoint is likely unreachable.
    flagged = {i["id"] for i in out if i.get("kind") == "integration"}
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    healthy = (db.query(IntegrationInstance)
               .filter(IntegrationInstance.tenant_id == user.tenant_id,
                       IntegrationInstance.enabled.is_(True),
                       or_(IntegrationInstance.owner_user_id == user.id,
                           IntegrationInstance.owner_user_id.is_(None))).all())
    for inst in healthy:
        if inst.id in flagged or (inst.provision_state or "idle") not in ("idle", "done"):
            continue
        interval = int(inst.poll_interval_minutes or 60)
        stale_after = timedelta(minutes=max(60, interval * 3))
        last = inst.last_success_at or inst.last_run_at
        if last is not None and last.tzinfo is not None:
            last = last.replace(tzinfo=None)
        if last is None:
            created = inst.created_at
            if created is not None and created.tzinfo is not None:
                created = created.replace(tzinfo=None)
            if not created or (now - created) < stale_after:
                continue  # give a brand-new integration time to first-run
            msg = "No data collected yet — the integration may be unreachable or misconfigured."
        elif (now - last) > stale_after:
            mins = int((now - last).total_seconds() // 60)
            msg = f"No fresh data for {mins} min — the integration endpoint may be unreachable."
        else:
            continue
        out.append({"id": inst.id, "kind": "integration",
                    "name": inst.label or _source_name(inst.integration_type),
                    "source_type": inst.integration_type,
                    "error": msg, "at": last or inst.updated_at,
                    "fails": 0, "reauth": False})

    out.sort(key=lambda i: i.get("at") or datetime.min.replace(tzinfo=None), reverse=True)
    return out


# --------------------------------------------------------------------------- #
# Appliance + storage-destination health                                      #
# --------------------------------------------------------------------------- #

def _is_org_admin_user(db, user: User) -> bool:
    return (user.role in ("owner", "security-admin")
            or bool(getattr(user, "is_platform_admin", False)))


def _visible_appliances(db, user: User):
    """Appliances this user is responsible for: everything in the tenant for org
    admins and shared/personal owners; only assigned units for a plain member."""
    from .models import Appliance, ApplianceAssignment
    tenant = db.get(Tenant, user.tenant_id)
    is_shared = bool(tenant and (tenant.tenant_type or "dedicated") == "shared")
    q = db.query(Appliance).filter(Appliance.tenant_id == user.tenant_id)
    if is_shared or _is_org_admin_user(db, user):
        return q.all()
    ids = [aid for (aid,) in db.query(ApplianceAssignment.appliance_id)
           .filter(ApplianceAssignment.user_id == user.id).all()]
    return q.filter(Appliance.id.in_(ids or ["-"])).all()


def appliance_problem_list(db, a) -> tuple[list[str], str]:
    """Health problems for one appliance as (messages, severity). Central so the
    audit sweep and the per-user notification agree on what 'a problem' is."""
    from .models import ApplianceStorage
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    probs: list[str] = []
    sev = "warning"
    hb = a.last_heartbeat_at
    if hb is not None and hb.tzinfo is not None:
        hb = hb.replace(tzinfo=None)
    offline_after = timedelta(minutes=appliance_alert_minutes(db))
    if hb is None or (now - hb) > offline_after:
        mins = int((now - hb).total_seconds() // 60) if hb else None
        probs.append(f"Offline for {mins} min" if mins else "Offline — no heartbeat received")
    if not a.attestation_ok:
        probs.append("Attestation failed"); sev = "critical"
    if a.tamper_state and a.tamper_state != "normal":
        probs.append(f"Tamper detected ({a.tamper_state})"); sev = "critical"
    if a.state == "QUARANTINED":
        probs.append("Quarantined"); sev = "critical"
    tel = a.telemetry or {}
    for s in db.query(ApplianceStorage).filter(ApplianceStorage.appliance_id == a.id).all():
        cap = s.capacity_bytes or 0
        used = s.used_bytes or 0
        if cap and used / cap >= 0.9:
            probs.append(f"{s.name} nearly full ({int(used / cap * 100)}%)")
        h = s.health or {}
        dh = h.get("drive_health")
        if dh and dh not in ("healthy", "disconnected"):
            probs.append(f"{s.name}: drive {dh}"); sev = "critical"
        if (s.state == "disconnected") and s.kind in ("external", "mirror"):
            probs.append(f"{s.name} disconnected")
        smart = h.get("smart") or {}
        if smart.get("enabled") and smart.get("status") not in (None, "passed"):
            probs.append(f"{s.name}: SMART {smart.get('status')}"); sev = "critical"
        raid = h.get("raid") or {}
        if raid.get("enabled") and raid.get("status") not in (None, "optimal"):
            probs.append(f"{s.name}: RAID {raid.get('status')}"); sev = "critical"
    lat = tel.get("cloud_latency_ms")
    if isinstance(lat, (int, float)) and lat >= slow_latency_ms(db):
        probs.append(f"Slow network link ({int(lat)} ms)")
    secd = tel.get("security") or {}
    breaches = secd.get("failed_auth") or secd.get("breach_attempts") or 0
    if isinstance(breaches, (int, float)) and breaches >= breach_attempt_threshold(db):
        probs.append(f"{int(breaches)} LAN intrusion attempt(s) blocked"); sev = "critical"
    return probs, sev


def _appliance_issues(db, user: User) -> list[dict]:
    out = []
    for a in _visible_appliances(db, user):
        probs, sev = appliance_problem_list(db, a)
        if probs:
            out.append({"id": a.id, "kind": "appliance", "name": a.name,
                        "error": "; ".join(probs[:4]), "at": a.last_heartbeat_at,
                        "severity": sev})
    out.sort(key=lambda i: i.get("at") or datetime.min.replace(tzinfo=None), reverse=True)
    return out


def _storage_issues(db, user: User) -> list[dict]:
    """Bring-your-own-storage destinations that failed their health probe
    (unreachable / not writable / not readable for recovery)."""
    from .models import CustomerStorage
    tenant = db.get(Tenant, user.tenant_id)
    is_shared = bool(tenant and (tenant.tenant_type or "dedicated") == "shared")
    q = (db.query(CustomerStorage)
         .filter(CustomerStorage.tenant_id == user.tenant_id,
                 CustomerStorage.enabled == True))  # noqa: E712
    if is_shared or not _is_org_admin_user(db, user):
        q = q.filter(CustomerStorage.owner_user_id == user.id)
    out = []
    for cs in q.all():
        if cs.last_test_ok is False or (cs.status or "") == "error":
            out.append({"id": cs.id, "kind": "storage", "name": cs.name,
                        "provider": cs.provider,
                        "error": (cs.last_test_error or "Health check failed").splitlines()[0][:200],
                        "at": cs.last_test_at})
    out.sort(key=lambda i: i.get("at") or datetime.min.replace(tzinfo=None), reverse=True)
    return out


# --------------------------------------------------------------------------- #
# Builders — return {subject, title, body_html, text, cta, preheader}         #
# --------------------------------------------------------------------------- #

def build_daily_summary(db, user: User, *, force: bool = False) -> dict | None:
    from .api import billing
    vids = _user_vault_ids(db, user)
    since = _day_start()
    receipts = (db.query(SnapshotReceipt)
                .filter(SnapshotReceipt.tenant_id == user.tenant_id,
                        SnapshotReceipt.vault_id.in_(vids),
                        SnapshotReceipt.created_at >= since).all()) if vids else []
    coll_ids = {r.collection_id for r in receipts if r.collection_id}
    colls = {c.id: c for c in db.query(Collection).filter(Collection.id.in_(coll_ids)).all()} if coll_ids else {}
    by_source: dict[str, int] = {}
    by_dest: dict[str, int] = {}
    dest_icon: dict[str, str] = {}
    total_bytes = 0
    for r in receipts:
        total_bytes += int(r.total_bytes or 0)
        c = colls.get(r.collection_id)
        st = (c.source_type if c else "source")
        by_source[st] = by_source.get(st, 0) + int(r.total_bytes or 0)
        label, ic = _dest_label(r.destination)
        by_dest[label] = by_dest.get(label, 0) + int(r.total_bytes or 0)
        dest_icon[label] = ic
    new_objects = 0
    if vids:
        new_objects = int(db.query(func.count(SearchDocument.id)).filter(
            SearchDocument.tenant_id == user.tenant_id,
            SearchDocument.vault_id.in_(vids),
            SearchDocument.created_at >= since).scalar() or 0)
    try:
        total_objects, used_bytes, _ = billing._user_usage(db, user)
    except Exception:  # noqa: BLE001
        total_objects, used_bytes = 0, 0
    issues = _source_issues(db, user)

    if not force and len(receipts) == 0 and not issues:
        return None  # nothing to report today

    parts: list[str] = []
    quiet = (len(receipts) == 0 and not issues)
    if quiet:
        parts.append('<p style="margin:0 0 14px;">No new backups in the last 24 hours — '
                     'everything already protected stays safe.</p>')
    else:
        parts.append('<p style="margin:0 0 14px;">Here\'s what Arkive protected for you today.</p>')
    parts.append(_stat_grid([
        {"icon": "clock", "label": "Recovery points", "value": len(receipts)},
        {"icon": "object", "label": "New items", "value": f"{new_objects:,}"},
        {"icon": "data", "label": "Data protected", "value": _fmt_bytes(total_bytes)},
        {"icon": "source", "label": "Sources", "value": len(by_source)},
    ]))
    if by_source:
        parts.append(_section("By source", "source"))
        parts.append(_rows([{"icon": "source", "icon_url": _source_icon_url(st),
                             "name": _source_name(st), "detail": _fmt_bytes(b)}
                            for st, b in sorted(by_source.items(), key=lambda x: -x[1])]))
    if by_dest:
        parts.append(_section("Where it was stored", "cloud"))
        parts.append(_rows([{"icon": dest_icon.get(d, "storage"), "name": d, "detail": _fmt_bytes(b)}
                            for d, b in sorted(by_dest.items(), key=lambda x: -x[1])]))
    parts.append(_section("Your protection", "storage"))
    parts.append(_stat_grid([
        {"icon": "object", "label": "Objects protected", "value": f"{total_objects:,}"},
        {"icon": "storage", "label": "Total data", "value": _fmt_bytes(used_bytes)},
    ]))
    # Insights placeholder — admin-selectable which cards appear.
    ins = _daily_insight(db, user, used_bytes, total_objects)
    if ins:
        parts.append(_section("Insight", "insight"))
        parts.append(ins)
    if issues:
        parts.append(_warnbox("Sources that need your attention",
                              [f'{i["name"]} — {i["error"]}' for i in issues]))
    return {
        "subject": f"Your Arkive daily summary — {since.strftime('%b %-d')}",
        "title": "Your daily protection summary",
        "body_html": "".join(parts),
        "text": f"Arkive protected {new_objects} new item(s) across {len(by_source)} source(s) today "
                f"({_fmt_bytes(total_bytes)}). View your dashboard: {_portal_url()}",
        "cta": {"label": "Open your dashboard", "url": _portal_url()},
        "preheader": f"{new_objects} new items protected today",
    }


def _daily_insight(db, user: User, used_bytes: int, total_objects: int) -> str:
    """Selective, admin-controlled insight cards (placeholder framework)."""
    allowed = enabled_insights(db)
    if "footprint" not in allowed or total_objects <= 0:
        return ""
    avg = _fmt_bytes(used_bytes / total_objects) if total_objects else "0 B"
    return (f'<div style="padding:12px 15px;border:1px solid #d9e4ff;background:#f2f6ff;'
            f'border-radius:10px;font-size:13.5px;color:#33405a;line-height:1.55;">'
            f'{_ic("insight")} Your protected footprint is <b>{_esc(f"{total_objects:,}")}</b> items '
            f'(<b>{_fmt_bytes(used_bytes)}</b>), about <b>{avg}</b> each on average.</div>')


def build_weekly_org(db, tenant: Tenant) -> dict | None:
    since = _day_start() - timedelta(days=7)
    vault_owner = _tenant_vault_owner(db, tenant.id)
    vids = list(vault_owner.keys())
    receipts = (db.query(SnapshotReceipt)
                .filter(SnapshotReceipt.tenant_id == tenant.id,
                        SnapshotReceipt.vault_id.in_(vids),
                        SnapshotReceipt.created_at >= since).all()) if vids else []
    if not receipts:
        return None
    coll_ids = {r.collection_id for r in receipts if r.collection_id}
    colls = {c.id: c for c in db.query(Collection).filter(Collection.id.in_(coll_ids)).all()} if coll_ids else {}
    users = {u.id: u for u in db.query(User).filter(User.tenant_id == tenant.id).all()}
    by_user: dict[str, int] = {}
    by_source: dict[str, int] = {}
    by_dest: dict[str, int] = {}
    dest_icon: dict[str, str] = {}
    total_bytes = 0
    for r in receipts:
        total_bytes += int(r.total_bytes or 0)
        owner = vault_owner.get(r.vault_id)
        if owner:
            by_user[owner] = by_user.get(owner, 0) + int(r.total_bytes or 0)
        c = colls.get(r.collection_id)
        st = c.source_type if c else "source"
        by_source[st] = by_source.get(st, 0) + int(r.total_bytes or 0)
        label, ic = _dest_label(r.destination)
        by_dest[label] = by_dest.get(label, 0) + int(r.total_bytes or 0)
        dest_icon[label] = ic

    parts: list[str] = []
    parts.append(f'<p style="margin:0 0 14px;">Here\'s how <b>{_esc(tenant.name)}</b> protected its data this week.</p>')
    parts.append(_stat_grid([
        {"icon": "clock", "label": "Recovery points", "value": len(receipts)},
        {"icon": "data", "label": "Data protected", "value": _fmt_bytes(total_bytes)},
        {"icon": "user", "label": "Active members", "value": len(by_user)},
        {"icon": "source", "label": "Sources", "value": len(by_source)},
    ]))
    if by_user:
        parts.append(_section("By member", "user"))
        parts.append(_rows([{"icon": "user",
                             "name": (users[o].full_name if o in users else "Member"),
                             "detail": _fmt_bytes(b)}
                            for o, b in sorted(by_user.items(), key=lambda x: -x[1])]))
    if by_source:
        parts.append(_section("By source", "source"))
        parts.append(_rows([{"icon": "source", "icon_url": _source_icon_url(st),
                             "name": _source_name(st), "detail": _fmt_bytes(b)}
                            for st, b in sorted(by_source.items(), key=lambda x: -x[1])]))
    if by_dest:
        parts.append(_section("By destination", "cloud"))
        parts.append(_rows([{"icon": dest_icon.get(d, "storage"), "name": d, "detail": _fmt_bytes(b)}
                            for d, b in sorted(by_dest.items(), key=lambda x: -x[1])]))
    return {
        "subject": f"{tenant.name}: your weekly protection summary",
        "title": f"Weekly summary — {tenant.name}",
        "body_html": "".join(parts),
        "text": f"{tenant.name} created {len(receipts)} recovery point(s) protecting "
                f"{_fmt_bytes(total_bytes)} across {len(by_user)} member(s) this week.",
        "cta": {"label": "Open the organization dashboard", "url": _portal_url()},
        "preheader": f"{_fmt_bytes(total_bytes)} protected this week",
    }


def build_source_problem(db, user: User, issues: list[dict] | None = None) -> dict | None:
    issues = issues if issues is not None else _source_issues(db, user)
    if not issues:
        return None
    kinds = {str(i.get("kind") or "connector") for i in issues}
    n = len(issues)
    parts: list[str] = []
    if n == 1:
        parts.append(f'<p style="margin:0 0 12px;">One of your connected services needs attention. '
                     f'Fix it to resume protection and monitoring — your existing history is safe.</p>')
    else:
        parts.append(f'<p style="margin:0 0 12px;"><b>{n} connected services</b> need attention. '
                     f'Fix them to resume protection and monitoring — your existing history is safe.</p>')
    parts.append(_rows([{"icon": "alert",
                         "icon_url": (_source_icon_url(i.get("source_type", ""))
                                      if i.get("kind") == "connector" else ""),
                         "name": (f'{i["name"]} · repeatedly failing' if i.get("fails", 0) > 5 else i["name"]),
                         "detail": i["error"]} for i in issues]))
    persistent = [i for i in issues if i.get("fails", 0) > 5]
    if persistent:
        parts.append(f'<p style="margin:12px 0 0;font-size:13.5px;color:#8a5a1a;">'
                     f'{_ic("warn")} {len(persistent)} source(s) have failed more than 5 times in a row — '
                     f'please re-connect them so protection can resume.</p>')
    cta_url = _portal_url()
    cta_label = "Review issues"
    if kinds == {"connector"}:
        cta_url = f"{_portal_url()}/connectors"
        cta_label = "Fix your sources"
    elif kinds == {"integration"}:
        cta_url = f"{_portal_url()}/integrations"
        cta_label = "Fix your integrations"
    return {
        "subject": (f'Action needed: “{issues[0]["name"]}” needs attention'
                    if n == 1 else f"Action needed: {n} services need attention"),
        "title": "A service needs your attention" if n == 1 else f"{n} services need attention",
        "body_html": "".join(parts),
        "text": "; ".join(f'{i["name"]}: {i["error"]}' for i in issues),
        "cta": {"label": cta_label, "url": cta_url},
        "preheader": f"{n} service(s) need attention",
    }


def build_appliance_problem(db, user: User, issues: list[dict] | None = None) -> dict | None:
    issues = issues if issues is not None else _appliance_issues(db, user)
    if not issues:
        return None
    n = len(issues)
    critical = any(i.get("severity") == "critical" for i in issues)
    parts: list[str] = []
    lead = ("One of your appliances needs attention." if n == 1
            else f"<b>{n} appliances</b> need attention.")
    parts.append(f'<p style="margin:0 0 12px;">{lead} Your protected data stays safe — '
                 f'address these so protection and monitoring continue uninterrupted.</p>')
    parts.append(_rows([{"icon": "server", "name": i["name"], "detail": i["error"]}
                        for i in issues]))
    if critical:
        parts.append(f'<p style="margin:12px 0 0;font-size:13.5px;color:#8a5a1a;">'
                     f'{_ic("warn")} One or more issues are critical (drive/RAID failure, tamper, '
                     f'or intrusion) — please act now.</p>')
    return {
        "subject": (f'Appliance alert: “{issues[0]["name"]}” needs attention'
                    if n == 1 else f"Appliance alert: {n} appliances need attention"),
        "title": "An appliance needs your attention" if n == 1 else f"{n} appliances need attention",
        "body_html": "".join(parts),
        "text": "; ".join(f'{i["name"]}: {i["error"]}' for i in issues),
        "cta": {"label": "Open Appliances", "url": f"{_portal_url()}/appliances"},
        "preheader": f"{n} appliance(s) need attention",
    }


def build_storage_problem(db, user: User, issues: list[dict] | None = None) -> dict | None:
    issues = issues if issues is not None else _storage_issues(db, user)
    if not issues:
        return None
    n = len(issues)
    parts: list[str] = []
    lead = ("One of your storage destinations can't be reached." if n == 1
            else f"<b>{n} storage destinations</b> can't be reached.")
    parts.append(f'<p style="margin:0 0 12px;">{lead} New backups may not be landing there, and '
                 f'recovery from it may fail. Check the destination\'s credentials and access.</p>')
    parts.append(_rows([{"icon": "database", "name": i["name"], "detail": i["error"]}
                        for i in issues]))
    return {
        "subject": (f'Storage alert: “{issues[0]["name"]}” is unreachable'
                    if n == 1 else f"Storage alert: {n} destinations unreachable"),
        "title": "A storage destination needs attention" if n == 1 else f"{n} storage destinations need attention",
        "body_html": "".join(parts),
        "text": "; ".join(f'{i["name"]}: {i["error"]}' for i in issues),
        "cta": {"label": "Open Cloud Storage", "url": f"{_portal_url()}/cloud-storage"},
        "preheader": f"{n} storage destination(s) need attention",
    }


def build_plan_change(db, user: User, change: dict) -> dict:
    """``change`` = {summary:[str], line_items:[{label,amount}], total_label, total,
    effective, plan_name}."""
    parts: list[str] = []
    parts.append(f'<p style="margin:0 0 12px;">Thanks — your Arkive plan has been updated. '
                 f'Here\'s a summary of the changes and your new billing.</p>')
    if change.get("summary"):
        parts.append(_rows([{"icon": "check", "name": s, "detail": ""} for s in change["summary"]]))
    if change.get("plan_name") or change.get("effective"):
        parts.append(_section("Details", "calendar"))
        detail_rows = []
        if change.get("plan_name"):
            detail_rows.append({"icon": "sparkle", "name": "Plan", "detail": change["plan_name"]})
        if change.get("effective"):
            detail_rows.append({"icon": "calendar", "name": "Effective", "detail": change["effective"]})
        parts.append(_rows(detail_rows))
    if change.get("line_items"):
        parts.append(_section("Your new billing", "credit-card"))
        parts.append(_price_table(change["line_items"],
                                  change.get("total_label", "Total / month"),
                                  change.get("total", "")))
    return {
        "subject": "Your Arkive plan has been updated",
        "title": "Your plan is confirmed",
        "body_html": "".join(parts),
        "text": "Your Arkive plan has been updated. "
                + "; ".join(change.get("summary", [])),
        "cta": {"label": "View billing", "url": f"{_portal_url()}/onboarding"},
        "preheader": "Confirmation of your plan changes",
    }


def _source_name(source_type: str) -> str:
    try:
        from .connectors import get_connector
        c = get_connector(source_type)
        if c and getattr(c, "display_name", None):
            return c.display_name
    except Exception:  # noqa: BLE001
        pass
    return (source_type or "Source").replace("_", " ").title()


# --------------------------------------------------------------------------- #
# Dispatch + logging + rate limiting                                          #
# --------------------------------------------------------------------------- #

def _already_sent(db, user_id: str, dedupe_key: str) -> bool:
    if not dedupe_key:
        return False
    return db.query(NotificationLog.id).filter(
        NotificationLog.user_id == user_id,
        NotificationLog.dedupe_key == dedupe_key,
        NotificationLog.ok.is_(True)).first() is not None


def _deliver(db, user: User, key: str, built: dict, dedupe_key: str = "") -> bool:
    """Render + send + log one notification. Returns True if it was sent."""
    recipients = _recipients(user)
    if not recipients:
        logger.warning("notify %s skipped: user %s has no email", key, user.id)
        return False
    footer = ("You're receiving this because email notifications are on for your "
              "Arkive account. Manage them in Settings.")
    extras = normalized_emails(user)
    if extras:
        footer += (" A copy is also sent to your additional notification "
                   f"address{'es' if len(extras) > 1 else ''}: {', '.join(extras)}.")
    html = emailer.render(built["title"], built["body_html"],
                          cta=built.get("cta"), preheader=built.get("preheader", ""),
                          footer_note=footer)
    sent_any = False
    for to_email in recipients:
        channel = emailer.send(to_email, built["subject"], html=html, text=built.get("text", ""),
                               category=f"notification:{key}")
        logger.info("notify %s -> %s via %s (subject=%r)", key, to_email, channel, built["subject"])
        try:
            db.add(NotificationLog(user_id=user.id, tenant_id=user.tenant_id, type=key,
                                   dedupe_key=dedupe_key, subject=built["subject"],
                                   to_email=to_email, ok=channel in ("ses", "smtp", "log")))
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        sent_any = True
    return sent_any


def send_notification(db, user: User, key: str, *, dedupe_key: str = "",
                      force: bool = False, **ctx) -> bool:
    """Build + send a notification if the user has the type enabled (unless
    ``force``). Returns True if an email was sent."""
    if key not in _TYPE_KEYS:
        logger.warning("notify skipped: unknown type %r", key)
        return False
    if not force and not is_enabled(user, key):
        logger.info("notify %s skipped: disabled in prefs for %s", key, user.email or user.id)
        return False
    if not force and _already_sent(db, user.id, dedupe_key):
        logger.info("notify %s skipped: already sent (dedupe=%s) to %s", key, dedupe_key, user.email or user.id)
        return False
    built = _build(db, user, key, ctx)
    if built is None:
        logger.info("notify %s skipped: nothing to send for %s", key, user.email or user.id)
        return False
    return _deliver(db, user, key, built, dedupe_key)


def _build(db, user: User, key: str, ctx: dict) -> dict | None:
    if key == "daily_summary":
        # build_force lets the SCHEDULED daily digest send even on a quiet day
        # (so the configured send time is observable), without bypassing the
        # per-user enable + once-a-day dedupe that send_notification enforces.
        return build_daily_summary(db, user, force=bool(ctx.get("force") or ctx.get("build_force")))
    if key == "source_problem":
        return build_source_problem(db, user, ctx.get("issues"))
    if key == "appliance_problem":
        return build_appliance_problem(db, user, ctx.get("issues"))
    if key == "storage_problem":
        return build_storage_problem(db, user, ctx.get("issues"))
    if key == "plan_change":
        return build_plan_change(db, user, ctx.get("change") or {})
    if key == "weekly_org":
        tenant = db.get(Tenant, user.tenant_id)
        return build_weekly_org(db, tenant) if tenant else None
    return None


def send_test(db, user: User, key: str) -> dict:
    """Admin/debug: force-send a notification to a specific user, using live data
    where available (falls back to representative sample content)."""
    ctx: dict = {"force": True}
    if key == "plan_change":
        ctx["change"] = _sample_plan_change()
    if key == "source_problem":
        # Use live issues, else a representative sample so the email always renders.
        issues = _source_issues(db, user)
        ctx["issues"] = issues or [{"id": "sample", "name": "Gmail (sample)",
                                    "error": "Authorization expired — please re-connect.", "at": None}]
    if key == "appliance_problem":
        issues = _appliance_issues(db, user)
        ctx["issues"] = issues or [{"id": "sample", "name": "Home Appliance (sample)", "kind": "appliance",
                                    "error": "Offline for 45 min; Dedicated Storage nearly full (92%)",
                                    "at": None, "severity": "warning"}]
    if key == "storage_problem":
        issues = _storage_issues(db, user)
        ctx["issues"] = issues or [{"id": "sample", "name": "My S3 bucket (sample)", "kind": "storage",
                                    "error": "write failed: Access Denied — check the write credential.", "at": None}]
    built = _build(db, user, key, ctx)
    if built is None and key == "daily_summary":
        built = build_daily_summary(db, user, force=True)
    if built is None and key == "weekly_org":
        return {"ok": False, "message": "No organization activity to summarize for this user's tenant."}
    if built is None:
        return {"ok": False, "message": "Nothing to send for this type."}
    _deliver(db, user, key, built, dedupe_key="")
    return {"ok": True, "sent_to": user.email, "type": key, "subject": built["subject"]}


def _sample_plan_change() -> dict:
    return {
        "plan_name": "Family / Pro",
        "effective": datetime.now(timezone.utc).strftime("%B %-d, %Y"),
        "summary": ["Upgraded to the Family / Pro plan",
                    "Increased protected storage to 5 TB",
                    "Added 1× Secure Appliance (CV Edge 5)"],
        "line_items": [
            {"label": "Family / Pro — 5 TB protected", "amount": "$30.00 / mo"},
            {"label": "Arkive Cloud storage (5 TB)", "amount": "$50.00 / mo"},
            {"label": "Secure Appliance lease (CV Edge 5)", "amount": "$59.00 / mo"},
        ],
        "total_label": "New monthly total",
        "total": "$139.00 / mo",
    }
