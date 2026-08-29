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
import json
import logging
import smtplib
import ssl
import time
import urllib.error
import urllib.request
import uuid
from email.message import EmailMessage
from typing import Iterable, Optional

from . import comms
from .config import get_settings

settings = get_settings()
log = logging.getLogger("arkive.email")

# Map an email ServiceObject kind → the transport its sender uses. Adding a new
# provider = register a kind here + a sender in _SENDERS (+ a kind in admin's
# _SERVICE_KINDS). Unknown kinds fall back to the suffix after "email-".
_EMAIL_KIND_PROVIDER = {
    "email-ses": "ses",
    "email-sendgrid": "sendgrid",
    "email-smtp": "smtp",
}


def _provider_for_kind(kind: str) -> str:
    kind = kind or ""
    return _EMAIL_KIND_PROVIDER.get(kind) or kind.split("email-", 1)[-1] or "ses"

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
    # The email service object assigned to the running node is AUTHORITATIVE: any
    # node that sends email uses its assigned service, and the service KIND selects
    # the transport (email-ses → ses, email-sendgrid → sendgrid, email-smtp → smtp).
    try:
        from .services import self_email_service
        svc = self_email_service()
        kind = str((svc or {}).get("kind") or "")
        if svc and kind.startswith("email-"):
            ov = svc.get("config") or {}
            cfg["provider"] = ov.get("provider") or _provider_for_kind(kind)
            cfg["service_kind"] = kind
            cfg["service_name"] = svc.get("name") or ""
            # Pass every non-empty config value through so each sender reads its own
            # keys (api_key, smtp_host/user/password, aws creds, from_email, …).
            for k, v in ov.items():
                if v not in (None, ""):
                    cfg[k] = v
            # SES stores its secret under the historical cfg key name.
            if ov.get("aws_access_key_id") and ov.get("aws_secret_access_key"):
                cfg["aws_access_key_id"] = str(ov["aws_access_key_id"]).strip()
                cfg["aws_secret"] = str(ov["aws_secret_access_key"]).strip()
            cfg["enabled"] = True
    except Exception:
        pass
    # Surface a common misconfiguration (an assigned provider missing its creds) so
    # it doesn't silently fall into log/error mode.
    if cfg.get("enabled"):
        miss = _missing_creds(cfg)
        if miss:
            log.warning("email service '%s' (%s): %s", cfg.get("service_name") or "default",
                        cfg.get("provider"), miss)
    _cfg_cache, _cfg_at = cfg, time.time()
    return cfg


def _missing_creds(cfg: dict) -> str:
    """Human-readable reason the configured provider can't send, or '' if it can."""
    p = cfg.get("provider")
    if p in ("ses", "sendgrid", "smtp") and not cfg.get("from_email"):
        return "no From email configured"
    if p == "ses" and not (cfg.get("aws_access_key_id") and cfg.get("aws_secret")):
        return "SES selected but AWS credentials are missing"
    if p == "sendgrid" and not cfg.get("api_key"):
        return "SendGrid selected but api_key is missing"
    if p == "smtp" and not (cfg.get("smtp_host") or settings.smtp_host):
        return "SMTP selected but smtp_host is missing"
    return ""


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
    # Prefer the service object's own SMTP config (email-smtp), fall back to env.
    host = cfg.get("smtp_host") or settings.smtp_host
    if not host:
        raise RuntimeError("SMTP host not configured")
    port = int(cfg.get("smtp_port") or settings.smtp_port or 587)
    user = cfg.get("smtp_user") or settings.smtp_user
    password = cfg.get("smtp_password") or settings.smtp_password or ""
    starttls = cfg.get("smtp_starttls", settings.smtp_starttls)
    if isinstance(starttls, str):
        starttls = starttls.strip().lower() in ("1", "true", "yes", "on")
    msg = EmailMessage()
    msg["From"] = f'{cfg["from_name"]} <{cfg["from_email"]}>' if cfg.get("from_name") else cfg["from_email"]
    msg["To"] = to
    msg["Subject"] = subject
    if cfg.get("reply_to"):
        msg["Reply-To"] = cfg["reply_to"]
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    with smtplib.SMTP(host, port, timeout=15) as server:
        if starttls:
            server.starttls(context=ssl.create_default_context())
        if user:
            server.login(user, password)
        server.send_message(msg)


def _send_sendgrid(cfg: dict, to: str, subject: str, html: str, text: str) -> None:
    api_key = (cfg.get("api_key") or "").strip()
    if not api_key:
        raise RuntimeError("SendGrid api_key not configured")
    if not cfg.get("from_email"):
        raise RuntimeError("SendGrid from_email not configured")
    sender = ({"email": cfg["from_email"], "name": cfg["from_name"]}
              if cfg.get("from_name") else {"email": cfg["from_email"]})
    payload: dict = {
        "personalizations": [{"to": [{"email": to}]}],
        "from": sender,
        "subject": subject,
        "content": [{"type": "text/plain", "value": text or " "},
                    {"type": "text/html", "value": html}],
    }
    if cfg.get("reply_to"):
        payload["reply_to"] = {"email": cfg["reply_to"]}
    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send", data=json.dumps(payload).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            if r.status >= 300:
                raise RuntimeError(f"SendGrid HTTP {r.status}")
    except urllib.error.HTTPError as e:  # surface SendGrid's error body
        body = e.read().decode("utf-8", "ignore")[:300]
        raise RuntimeError(f"SendGrid HTTP {e.code}: {body}") from e


# Provider → sender. Register a new email provider here (and a kind in
# _EMAIL_KIND_PROVIDER + admin _SERVICE_KINDS) to support it fleet-wide.
_SENDERS = {"ses": _send_ses, "smtp": _send_smtp, "sendgrid": _send_sendgrid}


def send_via_service(kind: str, config: dict, to: str, subject: str, html: str,
                     text: str = "") -> dict:
    """Send through a SPECIFIC email service object's own config (the admin 'send
    test' button), independent of the node-resolved sender. Returns {ok, error}."""
    cfg = dict(config or {})
    cfg["provider"] = cfg.get("provider") or _provider_for_kind(str(kind or ""))
    cfg.setdefault("from_name", cfg.get("from_name") or "Arkive")
    if not cfg.get("region"):
        cfg["region"] = settings.s3_region
    if cfg.get("aws_secret_access_key") and not cfg.get("aws_secret"):
        cfg["aws_secret"] = str(cfg["aws_secret_access_key"]).strip()
    sender = _SENDERS.get(cfg["provider"])
    if not sender:
        return {"ok": False, "error": f"no sender for provider '{cfg['provider']}'"}
    try:
        sender(cfg, to, subject, html, text or "Arkive email service test")
        return {"ok": True, "error": None}
    except Exception as exc:  # noqa: BLE001 — surface to the admin
        return {"ok": False, "error": str(exc)}


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
    sender = _SENDERS.get(provider)
    if sender:
        try:
            sender(cfg, to, subject, html, text)
            channel = provider
        except Exception as exc:  # never raise into request paths
            log.error("email send failed (%s -> %s): %s", provider, to, exc)
            channel, error = "error", str(exc)
    else:
        # Log mode (no live provider): emit the subject plus each body line as its
        # own short, tagged record so sign-in codes are always readable/greppable
        # in the service logs (journalctl -u cv-cloud), regardless of line length.
        if provider != "log":
            log.warning("no sender for email provider '%s' — logging instead", provider)
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
