"""
Outbound email service (platform-wide, reusable).

One entry point — ``send()`` — used for every notification the platform sends
(sign-in codes, alerts, admin broadcasts…). Delivery provider is chosen from the
admin-managed EmailConfig: AWS SES in production, SMTP if configured, otherwise
the message is logged so the platform still works in development.

Content is always wrapped in the shared branded template (``render``) so every
email has a consistent, polished look. Caller-supplied text is HTML-escaped by
the helpers that build bodies, so notification content can't inject markup.
"""

from __future__ import annotations

import html as _html
import logging
import smtplib
import ssl
import time
import uuid
from email.message import EmailMessage
from typing import Iterable, Optional

from . import comms
from .config import get_settings

settings = get_settings()
log = logging.getLogger("arkive.email")

_ACCENT = "#4f7cff"
_DARK = "#0b1120"

# Small in-process cache of the DB email config (refreshed periodically) so hot
# paths like sign-in codes don't hit the database on every send.
_cfg_cache: dict = {}
_cfg_at: float = 0.0
_CFG_TTL = 30.0


def _config() -> dict:
    """Load the EmailConfig row (cached), falling back to settings defaults."""
    global _cfg_cache, _cfg_at
    if _cfg_cache and time.time() - _cfg_at < _CFG_TTL:
        return _cfg_cache
    cfg = {
        "provider": "smtp" if settings.smtp_host else "log",
        "enabled": bool(settings.smtp_host),
        "from_email": settings.smtp_from,
        "from_name": "Arkive",
        "reply_to": "",
        "region": settings.s3_region,
        "aws_access_key_id": settings.aws_access_key_id or "",
        "aws_secret": settings.aws_secret_access_key or "",
    }
    try:
        from .db import SessionLocal
        from .models import EmailConfig
        from . import credstore
        with SessionLocal() as db:
            row = db.get(EmailConfig, "default")
            if row is not None:
                secret = settings.aws_secret_access_key or ""
                if row.aws_secret_encrypted:
                    try:
                        secret = credstore.decrypt("platform", row.aws_secret_encrypted).get("s", "")
                    except Exception:
                        pass
                cfg = {
                    "provider": row.provider,
                    "enabled": bool(row.enabled),
                    "from_email": row.from_email,
                    "from_name": row.from_name,
                    "reply_to": row.reply_to or "",
                    "region": row.region or settings.s3_region,
                    "aws_access_key_id": row.aws_access_key_id or settings.aws_access_key_id or "",
                    "aws_secret": secret,
                }
    except Exception as exc:  # DB not ready (e.g. first boot) — use fallback
        log.debug("email config load failed, using fallback: %s", exc)
    # Admin Config Object linked to the "ses" source overrides credentials/routing.
    try:
        from .platform_config import source_values
        ov = source_values("ses")
        for k in ("from_email", "from_name", "reply_to", "region"):
            if ov.get(k):
                cfg[k] = ov[k]
        # AWS creds must be taken as a matched pair from the SAME layer — an access
        # key from one place + a secret from another → SignatureDoesNotMatch.
        if ov.get("aws_access_key_id") and ov.get("aws_secret_access_key"):
            cfg["aws_access_key_id"] = ov["aws_access_key_id"].strip()
            cfg["aws_secret"] = ov["aws_secret_access_key"].strip()
    except Exception:
        pass
    # The email service object selected on the running node wins over all of the
    # above, so mail routing scales per-node (kind "email-ses").
    try:
        from .services import self_email_service
        svc = self_email_service()
        if svc and svc.get("kind") == "email-ses":
            ov = svc.get("config") or {}
            for k in ("provider", "from_email", "from_name", "reply_to", "region"):
                if ov.get(k):
                    cfg[k] = ov[k]
            if ov.get("aws_access_key_id") and ov.get("aws_secret_access_key"):
                cfg["aws_access_key_id"] = ov["aws_access_key_id"].strip()
                cfg["aws_secret"] = ov["aws_secret_access_key"].strip()
            cfg["enabled"] = True
    except Exception:
        pass
    _cfg_cache, _cfg_at = cfg, time.time()
    return cfg


def invalidate_config_cache() -> None:
    global _cfg_at
    _cfg_at = 0.0


# --------------------------------------------------------------------------- #
# Branded template                                                            #
# --------------------------------------------------------------------------- #

def render(title: str, body_html: str, *, preheader: str = "",
           cta: Optional[dict] = None, footer_note: str = "") -> str:
    """Wrap body HTML in the shared Arkive email shell. ``cta`` = {label, url}."""
    cfg = _config()
    button = ""
    if cta and cta.get("url"):
        button = (
            f'<tr><td style="padding:8px 0 4px;">'
            f'<a href="{_html.escape(cta["url"])}" '
            f'style="display:inline-block;background:{_ACCENT};color:#ffffff;'
            f'text-decoration:none;font-weight:600;font-size:14px;padding:11px 22px;'
            f'border-radius:8px;">{_html.escape(cta.get("label", "Open Arkive"))}</a>'
            f'</td></tr>'
        )
    note = ""
    if footer_note:
        note = (f'<p style="margin:0 0 10px;color:#8a94a7;font-size:12px;'
                f'line-height:1.5;">{footer_note}</p>')
    year = time.strftime("%Y")
    return f"""\
<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<title>{_html.escape(title)}</title></head>
<body style="margin:0;padding:0;background:#eef1f7;">
<span style="display:none;visibility:hidden;opacity:0;height:0;width:0;">{_html.escape(preheader)}</span>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef1f7;padding:28px 12px;">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;border-radius:14px;overflow:hidden;box-shadow:0 4px 24px rgba(11,17,32,.08);">
  <tr><td style="background:{_DARK};padding:20px 28px;">
    <table role="presentation" width="100%"><tr>
      <td style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#ffffff;font-size:18px;font-weight:700;letter-spacing:.2px;">
        <span style="display:inline-block;width:22px;height:22px;line-height:22px;text-align:center;background:{_ACCENT};color:#fff;border-radius:6px;font-size:13px;margin-right:8px;">A</span>Arkive
      </td>
      <td align="right" style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#8a94a7;font-size:11px;">Digital Continuity</td>
    </tr></table>
  </td></tr>
  <tr><td style="height:3px;background:{_ACCENT};"></td></tr>
  <tr><td style="padding:28px;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1a2234;">
    <h1 style="margin:0 0 14px;font-size:20px;font-weight:700;color:#0b1120;">{_html.escape(title)}</h1>
    <div style="font-size:14px;line-height:1.6;color:#33405a;">{body_html}</div>
    <table role="presentation" cellpadding="0" cellspacing="0" style="margin-top:6px;">{button}</table>
  </td></tr>
  <tr><td style="padding:18px 28px;border-top:1px solid #e6eaf2;">
    {note}
    <p style="margin:0 0 6px;color:#8a94a7;font-size:12px;line-height:1.5;">
      For your security, Arkive will never ask for your password, recovery keys, or 2FA codes by email.
    </p>
    <p style="margin:0;color:#aab2c3;font-size:11px;">© {year} Arkive · arkive.life</p>
  </td></tr>
</table>
</td></tr></table></body></html>"""


def text_to_html(text: str) -> str:
    """Escape plain text and turn blank-line-separated blocks into paragraphs."""
    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
    return "".join(
        f'<p style="margin:0 0 12px;">{_html.escape(b).replace(chr(10), "<br>")}</p>'
        for b in blocks
    ) or f'<p style="margin:0;">{_html.escape(text)}</p>'


# --------------------------------------------------------------------------- #
# Delivery                                                                    #
# --------------------------------------------------------------------------- #

def _send_ses(cfg: dict, to: str, subject: str, html: str, text: str) -> None:
    import boto3  # lazy so dev without boto3 still runs
    kwargs = {"region_name": cfg["region"]}
    key = (cfg.get("aws_access_key_id") or settings.aws_access_key_id or "").strip()
    secret = (cfg.get("aws_secret") or settings.aws_secret_access_key or "").strip()
    if key and secret:
        kwargs["aws_access_key_id"] = key
        kwargs["aws_secret_access_key"] = secret
    client = boto3.client("ses", **kwargs)
    source = f'{cfg["from_name"]} <{cfg["from_email"]}>' if cfg["from_name"] else cfg["from_email"]
    params = {
        "Source": source,
        "Destination": {"ToAddresses": [to]},
        "Message": {
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {"Html": {"Data": html, "Charset": "UTF-8"},
                     "Text": {"Data": text, "Charset": "UTF-8"}},
        },
    }
    if cfg.get("reply_to"):
        params["ReplyToAddresses"] = [cfg["reply_to"]]
    client.send_email(**params)


def _send_smtp(cfg: dict, to: str, subject: str, html: str, text: str) -> None:
    msg = EmailMessage()
    msg["From"] = f'{cfg["from_name"]} <{cfg["from_email"]}>' if cfg["from_name"] else cfg["from_email"]
    msg["To"] = to
    msg["Subject"] = subject
    if cfg.get("reply_to"):
        msg["Reply-To"] = cfg["reply_to"]
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as server:
        if settings.smtp_starttls:
            server.starttls(context=ssl.create_default_context())
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password or "")
        server.send_message(msg)


def send_verbose(to: str, subject: str, *, html: str, text: str = "",
                 category: str = "email") -> dict:
    """Send one branded email, returning {channel, error, provider}. ``channel``
    is 'ses'|'smtp'|'log'|'error'; ``error`` holds the provider message on failure
    (surfaced by the admin test so misconfig — sandbox, unverified sender — is
    diagnosable instead of silently swallowed). Every message is recorded in the
    communications history and carries a 1x1 open-tracking pixel."""
    cfg = _config()
    text = text or "Please view this message in an HTML-capable email client."
    provider = cfg["provider"] if cfg["enabled"] else "log"
    # Communications history + open tracking: id the message, inject the pixel,
    # and record the send (status/provider/body) globally.
    cid = uuid.uuid4().hex
    error: Optional[str] = None
    try:
        html = comms.inject_pixel(html, cid)
    except Exception:  # noqa: BLE001
        pass
    if provider in ("ses", "smtp") and not (provider == "smtp" and not settings.smtp_host):
        try:
            if provider == "ses":
                _send_ses(cfg, to, subject, html, text)
            else:
                _send_smtp(cfg, to, subject, html, text)
            channel = provider
        except Exception as exc:  # never raise into request paths
            log.error("email send failed (%s -> %s): %s", provider, to, exc)
            channel, error = "error", str(exc)
    else:
        # Log mode (no live provider): emit the subject plus each body line as its
        # own short, tagged record so sign-in codes are always readable/greppable
        # in the service logs (journalctl -u cv-cloud), regardless of line length.
        log.warning("EMAIL (provider=log) -> %s | %s", to, subject)
        for line in text.splitlines():
            line = line.strip()
            if line:
                log.warning("EMAIL body> %s", line)
        channel = "log"
    comms.record(cid, to_email=to, subject=subject, body_html=html, body_text=text,
                 category=category, channel=channel, provider=provider, error=error)
    return {"channel": channel, "error": error, "provider": provider}


def send(to: str, subject: str, *, html: str, text: str = "",
         category: str = "email") -> str:
    """Send one branded email. Returns the delivery channel used ('ses'|'smtp'|'log')."""
    return send_verbose(to, subject, html=html, text=text, category=category)["channel"]


def send_bulk(recipients: Iterable[str], subject: str, *, html: str,
              text: str = "", category: str = "broadcast") -> dict:
    """Send the same branded email individually to each recipient (no shared To/
    CC, so addresses are never disclosed to each other). Returns a summary."""
    sent = 0
    failed = 0
    channel = "log"
    for addr in recipients:
        ch = send(addr, subject, html=html, text=text, category=category)
        if ch in ("ses", "smtp", "log"):
            sent += 1
            if ch != "log":
                channel = ch
        else:
            failed += 1
        time.sleep(0.05)  # gentle pacing under SES send-rate limits
    return {"sent": sent, "failed": failed, "channel": channel}


# --------------------------------------------------------------------------- #
# Back-compat helper for the auth code emails                                 #
# --------------------------------------------------------------------------- #

def send_email(to: str, subject: str, body: str, *, category: str = "email") -> str:
    """Legacy plain-body entry point — now branded. Used by auth code delivery."""
    return send(to, subject, html=render(subject, text_to_html(body)), text=body,
                category=category)
