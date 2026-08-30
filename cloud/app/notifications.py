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

from sqlalchemy import func

from . import emailer
from .models import (Collection, ConnectorAccount, NotificationLog, SearchDocument,
                     SnapshotReceipt, SystemSetting, Tenant, User, Vault)

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


def _source_issues(db, user: User) -> list[dict]:
    """Sources belonging to the user's collections that currently have an error."""
    my_colls = (db.query(Collection.connector_account_id)
                .filter(Collection.tenant_id == user.tenant_id,
                        Collection.vault_id.in_(_user_vault_ids(db, user) or ["-"])).all())
    acct_ids = {a for (a,) in my_colls if a}
    if not acct_ids:
        return []
    out = []
    for a in (db.query(ConnectorAccount)
              .filter(ConnectorAccount.id.in_(acct_ids),
                      ConnectorAccount.last_error.isnot(None)).all()):
        out.append({"id": a.id, "name": a.account_label or a.connector_type,
                    "source_type": a.connector_type,
                    "error": (a.last_error or "").splitlines()[0][:160],
                    "at": a.last_error_at, "fails": int(a.fail_count or 0),
                    "reauth": a.auth_status == "needs-reauth"})
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
    parts.append(f'<p style="margin:0 0 14px;">Here\'s what Arkive protected for you today.</p>')
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
    n = len(issues)
    parts: list[str] = []
    if n == 1:
        parts.append(f'<p style="margin:0 0 12px;">One of your connected sources needs attention. '
                     f'Re-connect it to resume protection — your existing history is safe.</p>')
    else:
        parts.append(f'<p style="margin:0 0 12px;"><b>{n} of your sources</b> need attention. '
                     f'Re-connect them to resume protection — your existing history is safe.</p>')
    parts.append(_rows([{"icon": "alert", "icon_url": _source_icon_url(i.get("source_type", "")),
                         "name": (f'{i["name"]} · repeatedly failing' if i.get("fails", 0) > 5 else i["name"]),
                         "detail": i["error"]} for i in issues]))
    persistent = [i for i in issues if i.get("fails", 0) > 5]
    if persistent:
        parts.append(f'<p style="margin:12px 0 0;font-size:13.5px;color:#8a5a1a;">'
                     f'{_ic("warn")} {len(persistent)} source(s) have failed more than 5 times in a row — '
                     f'please re-connect them so protection can resume.</p>')
    return {
        "subject": (f'Action needed: “{issues[0]["name"]}” needs attention'
                    if n == 1 else f"Action needed: {n} sources need attention"),
        "title": "A source needs your attention" if n == 1 else f"{n} sources need attention",
        "body_html": "".join(parts),
        "text": "; ".join(f'{i["name"]}: {i["error"]}' for i in issues),
        "cta": {"label": "Fix your sources", "url": f"{_portal_url()}/connectors"},
        "preheader": f"{n} source(s) need attention",
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
    html = emailer.render(built["title"], built["body_html"],
                          cta=built.get("cta"), preheader=built.get("preheader", ""),
                          footer_note="You're receiving this because email notifications are on for your "
                                      "Arkive account. Manage them in Settings.")
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
        return build_daily_summary(db, user, force=bool(ctx.get("force")))
    if key == "source_problem":
        return build_source_problem(db, user, ctx.get("issues"))
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
