"""
Email delivery for verification / sign-in codes.

Uses SMTP when configured; otherwise logs the message so the platform can still
bootstrap (read the code from `journalctl -u cv-cloud`). In development the API
also returns the code directly for convenience.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from .config import get_settings

settings = get_settings()
log = logging.getLogger("arkive.email")


def send_email(to: str, subject: str, body: str) -> str:
    """Return the delivery channel used: 'smtp' or 'log'."""
    if not settings.smtp_host:
        log.warning("EMAIL (no SMTP configured) -> %s | %s\n%s", to, subject, body)
        return "log"
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as server:
        if settings.smtp_starttls:
            server.starttls(context=ssl.create_default_context())
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password or "")
        server.send_message(msg)
    return "smtp"
