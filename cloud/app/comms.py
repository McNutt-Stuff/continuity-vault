"""Global communications history + email open tracking.

Every outbound email flows through ``emailer.send`` → ``send_verbose``, which
gives the message an id, injects a 1x1 tracking pixel, and records a
``Communication`` row here (resilient — recording never breaks a send). The
pixel always points at the CONTROL PLANE so opens are recorded centrally; on a
federated node the initial row is written locally and pushed to the control plane
by the replication loop, which never clobbers the CP-owned open fields.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func

from .config import get_settings

logger = logging.getLogger("cv.comms")
settings = get_settings()

# 1x1 transparent GIF returned by the open-tracking pixel.
PIXEL_GIF = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
             b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00"
             b"\x02\x02D\x01\x00;")


def _cp_api_base() -> str:
    """Public API base of the CONTROL PLANE (where opens are recorded)."""
    s = settings
    if (s.node_role or "control-plane") != "control-plane" and s.control_plane_url:
        return s.control_plane_url.rstrip("/") + "/api"
    return (s.api_base_url or "").rstrip("/")


def pixel_url(cid: str) -> str:
    return f"{_cp_api_base()}/c/o/{cid}.gif"


def inject_pixel(html: str, cid: str) -> str:
    img = (f'<img src="{pixel_url(cid)}" width="1" height="1" alt="" '
           f'style="display:none;width:1px;height:1px;opacity:0" />')
    if "</body>" in html:
        return html.replace("</body>", img + "</body>", 1)
    return html + img


def record(cid: str, *, to_email: str, subject: str, body_html: str, body_text: str,
           category: str, channel: str, provider: str, error: str | None) -> None:
    """Persist one outbound email. Never raises into the send path."""
    try:
        from .db import SessionLocal
        from .models import Communication, User
        status = "failed" if channel == "error" else ("logged" if channel == "log" else "sent")
        with SessionLocal() as db:
            uid = tid = None
            if to_email:
                u = (db.query(User)
                     .filter(func.lower(User.email) == to_email.strip().lower()).first())
                if u:
                    uid, tid = u.id, u.tenant_id
            db.add(Communication(
                id=cid, user_id=uid, tenant_id=tid, to_email=to_email or "",
                category=category or "email", subject=subject or "",
                body_html=body_html or "", body_text=body_text or "",
                channel=channel or "", status=status, provider=provider or "",
                error=error or "", node_name=settings.node_name or settings.domain))
            db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("failed to record communication to %s", to_email)


def mark_opened(cid: str, ip: str = "") -> None:
    """Record an email open (pixel hit). Creates a stub if the row hasn't
    replicated from the sending node yet (matched later by id on push)."""
    try:
        from .db import SessionLocal
        from .models import Communication
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with SessionLocal() as db:
            c = db.get(Communication, cid)
            if c is None:
                c = Communication(id=cid, category="email", status="sent")
                db.add(c)
            if c.opened_at is None:
                c.opened_at = now
            c.open_count = (c.open_count or 0) + 1
            if ip:
                c.last_opened_ip = ip[:64]
            db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("failed to record open for %s", cid)
