"""
Support site + ticketing.

Two surfaces:

* Documentation (wiki) — a public, CMS-managed knowledge base. Admins author
  ``SupportDoc`` pages; ``GET /support/content`` returns the published tree,
  which the Public Web Node mirrors to a local ``support.json`` (like the
  marketing site) and serves under ``/support``. No auth on reads.

* Tickets — an auth-protected help desk on the control plane. Customers open and
  track tickets; support staff (platform admins or the ``support-admin`` role)
  triage, respond, and update them. Email notifications go out on create/reply/
  status change.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import audit, emailer, security
from ..db import get_db
from ..models import (SupportDoc, SupportSection, SupportTicket, TicketMessage, Tenant, User)
from ..support_defaults import DEFAULT_SUPPORT_DOCS, DEFAULT_SUPPORT_SECTIONS

# Public docs (no auth) + customer tickets (logged-in) share this prefix.
public_router = APIRouter(prefix="/support", tags=["support"])
tickets_router = APIRouter(prefix="/support", tags=["support-tickets"])
# Admin CMS + ticket triage.
admin_router = APIRouter(prefix="/admin/support", tags=["support-admin"])

CATEGORIES = [
    {"key": "billing", "label": "Billing & subscription", "icon": "credit-card"},
    {"key": "technical", "label": "Technical / trouble", "icon": "alert"},
    {"key": "feature_request", "label": "Feature request", "icon": "sparkle"},
    {"key": "account", "label": "Account & access", "icon": "user"},
    {"key": "other", "label": "Something else", "icon": "help"},
]
_CATEGORY_KEYS = {c["key"] for c in CATEGORIES}
_STATUSES = {"open", "pending", "resolved", "closed"}
_PRIORITIES = {"low", "normal", "high", "urgent"}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# Auth: support staff = platform admin OR the support-admin role               #
# --------------------------------------------------------------------------- #

def require_support_agent(principal: security.Principal = Depends(security.get_principal)
                          ) -> security.Principal:
    if principal.is_platform_admin or principal.role == "support-admin":
        return principal
    raise HTTPException(403, "support staff access required")


# --------------------------------------------------------------------------- #
# Documentation (wiki)                                                         #
# --------------------------------------------------------------------------- #

def _doc_public(d: SupportDoc) -> dict:
    return {
        "slug": d.slug, "title": d.title, "section": d.section,
        "section_order": d.section_order, "nav_order": d.nav_order,
        "icon": d.icon or "book", "summary": d.summary or "", "body": d.body or "",
        "help_routes": d.help_routes or [],
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
    }


def _section_orders(db: Session) -> dict:
    """Map section name -> nav order from the first-class section table."""
    return {s.name: s.order for s in db.query(SupportSection).all()}


def _ensure_section(db: Session, name: str) -> None:
    """Auto-create a section row for an ad-hoc section name so it becomes
    manageable (renamed / reordered) like any other."""
    name = (name or "").strip() or "General"
    if db.query(SupportSection).filter(SupportSection.name == name).first():
        return
    top = db.query(SupportSection).order_by(SupportSection.order.desc()).first()
    db.add(SupportSection(name=name, order=(top.order + 10) if top else 100, icon="book"))


def _build_tree(docs: list[SupportDoc], order_map: dict | None = None) -> list[dict]:
    """Group docs into ordered sections for the nav (section order comes from the
    section table when available, else the doc's own section_order)."""
    order_map = order_map or {}
    sections: dict[str, dict] = {}
    for d in docs:
        name = d.section or "General"
        s = sections.setdefault(name, {"section": name,
                                       "order": order_map.get(name, d.section_order),
                                       "docs": []})
        if name not in order_map:
            s["order"] = min(s["order"], d.section_order)
        s["docs"].append({"slug": d.slug, "title": d.title, "icon": d.icon or "book",
                          "summary": d.summary or "", "nav_order": d.nav_order})
    out = sorted(sections.values(), key=lambda s: (s["order"], s["section"]))
    for s in out:
        s["docs"].sort(key=lambda x: (x["nav_order"], x["title"]))
    return out


@public_router.get("/content")
def support_content(db: Session = Depends(get_db)):
    """Full published knowledge base (nav tree + every page body), for the Public
    Web Node to mirror locally and serve under /support. No auth."""
    docs = (db.query(SupportDoc).filter(SupportDoc.published.is_(True))
            .order_by(SupportDoc.section_order, SupportDoc.nav_order).all())
    return {
        "tree": _build_tree(docs, _section_orders(db)),
        "docs": {d.slug: _doc_public(d) for d in docs},
        "updated_at": max((d.updated_at for d in docs if d.updated_at), default=None).isoformat()
        if docs else None,
    }


@public_router.get("/doc/{slug}")
def support_doc(slug: str, db: Session = Depends(get_db)):
    d = (db.query(SupportDoc)
         .filter(SupportDoc.slug == slug, SupportDoc.published.is_(True)).first())
    if d is None:
        raise HTTPException(404, "not found")
    return _doc_public(d)


# --------------------------------------------------------------------------- #
# Documentation admin (CMS)                                                    #
# --------------------------------------------------------------------------- #

def _doc_admin(d: SupportDoc) -> dict:
    return {**_doc_public(d), "id": d.id, "published": bool(d.published)}


def _section_out(s: SupportSection, count: int) -> dict:
    return {"id": s.id, "name": s.name, "order": s.order, "icon": s.icon or "book", "count": count}


@admin_router.get("/docs", dependencies=[Depends(security.require_platform_admin)])
def admin_list_docs(db: Session = Depends(get_db)):
    docs = (db.query(SupportDoc)
            .order_by(SupportDoc.section_order, SupportDoc.nav_order).all())
    order_map = _section_orders(db)
    counts: dict = {}
    for d in docs:
        counts[d.section or "General"] = counts.get(d.section or "General", 0) + 1
    sections = [_section_out(s, counts.get(s.name, 0))
                for s in db.query(SupportSection).order_by(SupportSection.order, SupportSection.name).all()]
    return {"docs": [_doc_admin(d) for d in docs],
            "tree": _build_tree(docs, order_map), "sections": sections}


class DocIn(BaseModel):
    slug: str | None = None
    title: str
    section: str = "General"
    section_order: int = 100
    nav_order: int = 100
    icon: str = "book"
    summary: str = ""
    body: str = ""
    help_routes: list[str] = []
    published: bool = True


def _slugify(text: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in (text or "").lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-") or "page"


@admin_router.post("/docs", dependencies=[Depends(security.require_platform_admin)])
def admin_create_doc(body: DocIn, principal: security.Principal = Depends(security.require_platform_admin),
                     db: Session = Depends(get_db)):
    slug = _slugify(body.slug or body.title)
    if db.query(SupportDoc).filter(SupportDoc.slug == slug).first():
        slug = f"{slug}-{secrets.token_hex(2)}"
    d = SupportDoc(
        slug=slug, title=body.title, section=body.section,
        section_order=body.section_order, nav_order=body.nav_order, icon=body.icon,
        summary=body.summary, body=body.body, help_routes=body.help_routes,
        published=body.published)
    _ensure_section(db, body.section)
    db.add(d)
    db.commit()
    db.refresh(d)
    audit.record(db, actor=principal.user_id, action="admin.support_doc_created",
                 category="admin", resource=d.slug)
    return _doc_admin(d)


@admin_router.put("/docs/{doc_id}", dependencies=[Depends(security.require_platform_admin)])
def admin_update_doc(doc_id: str, body: DocIn,
                     principal: security.Principal = Depends(security.require_platform_admin),
                     db: Session = Depends(get_db)):
    d = db.get(SupportDoc, doc_id)
    if d is None:
        raise HTTPException(404, "not found")
    if body.slug:
        new_slug = _slugify(body.slug)
        clash = db.query(SupportDoc).filter(SupportDoc.slug == new_slug,
                                            SupportDoc.id != d.id).first()
        d.slug = new_slug if not clash else d.slug
    d.title = body.title
    d.section = body.section
    d.section_order = body.section_order
    d.nav_order = body.nav_order
    d.icon = body.icon
    d.summary = body.summary
    d.body = body.body
    d.help_routes = body.help_routes
    d.published = body.published
    _ensure_section(db, body.section)
    db.commit()
    db.refresh(d)
    audit.record(db, actor=principal.user_id, action="admin.support_doc_updated",
                 category="admin", resource=d.slug)
    return _doc_admin(d)


@admin_router.delete("/docs/{doc_id}", dependencies=[Depends(security.require_platform_admin)])
def admin_delete_doc(doc_id: str, principal: security.Principal = Depends(security.require_platform_admin),
                     db: Session = Depends(get_db)):
    d = db.get(SupportDoc, doc_id)
    if d is None:
        raise HTTPException(404, "not found")
    slug = d.slug
    db.delete(d)
    db.commit()
    audit.record(db, actor=principal.user_id, action="admin.support_doc_deleted",
                 category="admin", resource=slug)
    return {"ok": True}


@admin_router.post("/seed", dependencies=[Depends(security.require_platform_admin)])
def admin_seed_docs(principal: security.Principal = Depends(security.require_platform_admin),
                    db: Session = Depends(get_db)):
    """Create any missing default documentation pages (idempotent — never
    overwrites edits to existing slugs)."""
    created = 0
    for spec in DEFAULT_SUPPORT_SECTIONS:
        if not db.query(SupportSection).filter(SupportSection.name == spec["name"]).first():
            db.add(SupportSection(**spec))
    for spec in DEFAULT_SUPPORT_DOCS:
        if db.query(SupportDoc).filter(SupportDoc.slug == spec["slug"]).first():
            continue
        db.add(SupportDoc(**spec))
        created += 1
    db.commit()
    audit.record(db, actor=principal.user_id, action="admin.support_docs_seeded",
                 category="admin", detail={"created": created})
    return {"ok": True, "created": created}


# --------------------------------------------------------------------------- #
# Documentation admin — sections                                              #
# --------------------------------------------------------------------------- #

@admin_router.get("/sections", dependencies=[Depends(security.require_platform_admin)])
def admin_list_sections(db: Session = Depends(get_db)):
    counts: dict = {}
    for (name,) in db.query(SupportDoc.section).all():
        counts[name or "General"] = counts.get(name or "General", 0) + 1
    return {"sections": [_section_out(s, counts.get(s.name, 0))
                         for s in db.query(SupportSection)
                         .order_by(SupportSection.order, SupportSection.name).all()]}


class SectionIn(BaseModel):
    name: str
    order: int | None = None
    icon: str = "book"


@admin_router.post("/sections", dependencies=[Depends(security.require_platform_admin)])
def admin_create_section(body: SectionIn,
                         principal: security.Principal = Depends(security.require_platform_admin),
                         db: Session = Depends(get_db)):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    if db.query(SupportSection).filter(SupportSection.name == name).first():
        raise HTTPException(400, "a section with that name already exists")
    if body.order is None:
        top = db.query(SupportSection).order_by(SupportSection.order.desc()).first()
        order = (top.order + 10) if top else 100
    else:
        order = body.order
    s = SupportSection(name=name, order=order, icon=body.icon or "book")
    db.add(s)
    db.commit()
    db.refresh(s)
    audit.record(db, actor=principal.user_id, action="admin.support_section_created",
                 category="admin", resource=name)
    return _section_out(s, 0)


class SectionUpdate(BaseModel):
    name: str | None = None
    order: int | None = None
    icon: str | None = None


@admin_router.put("/sections/{section_id}", dependencies=[Depends(security.require_platform_admin)])
def admin_update_section(section_id: str, body: SectionUpdate,
                         principal: security.Principal = Depends(security.require_platform_admin),
                         db: Session = Depends(get_db)):
    s = db.get(SupportSection, section_id)
    if s is None:
        raise HTTPException(404, "not found")
    moved = 0
    if body.name and body.name.strip() and body.name.strip() != s.name:
        new_name = body.name.strip()
        if db.query(SupportSection).filter(SupportSection.name == new_name,
                                           SupportSection.id != s.id).first():
            raise HTTPException(400, "a section with that name already exists")
        old_name = s.name
        s.name = new_name
        # Rename cascades to every doc that referenced the old section name.
        moved = (db.query(SupportDoc).filter(SupportDoc.section == old_name)
                 .update({SupportDoc.section: new_name}, synchronize_session=False))
    if body.order is not None:
        s.order = body.order
        # Keep docs' own section_order aligned so the nav order is consistent
        # even where the section table isn't consulted.
        db.query(SupportDoc).filter(SupportDoc.section == s.name).update(
            {SupportDoc.section_order: body.order}, synchronize_session=False)
    if body.icon:
        s.icon = body.icon
    db.commit()
    db.refresh(s)
    audit.record(db, actor=principal.user_id, action="admin.support_section_updated",
                 category="admin", resource=s.name, detail={"docs_moved": int(moved)})
    n = db.query(SupportDoc).filter(SupportDoc.section == s.name).count()
    return _section_out(s, n)


@admin_router.delete("/sections/{section_id}", dependencies=[Depends(security.require_platform_admin)])
def admin_delete_section(section_id: str,
                         principal: security.Principal = Depends(security.require_platform_admin),
                         db: Session = Depends(get_db)):
    s = db.get(SupportSection, section_id)
    if s is None:
        raise HTTPException(404, "not found")
    n = db.query(SupportDoc).filter(SupportDoc.section == s.name).count()
    if n:
        raise HTTPException(400, f"{n} page(s) are still in this section — move or delete them first")
    name = s.name
    db.delete(s)
    db.commit()
    audit.record(db, actor=principal.user_id, action="admin.support_section_deleted",
                 category="admin", resource=name)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Tickets — shared helpers + email                                            #
# --------------------------------------------------------------------------- #

def _support_email(db: Session) -> str:
    """The support inbox address (from the editable site content, else default)."""
    try:
        from ..models import SiteContent
        row = db.get(SiteContent, "default")
        contact = ((row.content or {}).get("contact") if row else None) or {}
        return contact.get("support") or "support@arkive.life"
    except Exception:  # noqa: BLE001
        return "support@arkive.life"


def _portal_url() -> str:
    from ..config import get_settings
    s = get_settings()
    base = (getattr(s, "rp_origin", "") or "").rstrip("/")
    return base or f"https://{getattr(s, 'domain', 'vault.arkive.life')}"


def _ticket_out(t: SupportTicket, *, with_messages: bool = False) -> dict:
    out = {
        "id": t.id, "ref": t.ref, "subject": t.subject, "category": t.category,
        "priority": t.priority, "status": t.status,
        "requester_email": t.requester_email, "requester_name": t.requester_name,
        "assignee_user_id": t.assignee_user_id,
        "last_activity_at": t.last_activity_at.isoformat() if t.last_activity_at else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "message_count": len(t.messages),
    }
    if with_messages:
        out["messages"] = [{
            "id": m.id, "author_name": m.author_name, "is_staff": bool(m.is_staff),
            "body": m.body, "created_at": m.created_at.isoformat() if m.created_at else None,
        } for m in t.messages]
    return out


def _cat_label(key: str) -> str:
    for c in CATEGORIES:
        if c["key"] == key:
            return c["label"]
    return key


def _email_new_ticket(db: Session, t: SupportTicket, first_body: str) -> None:
    url = f"{_portal_url()}/support/tickets/{t.id}"
    body = emailer.render(
        f"We've received your request — {t.ref}",
        emailer.text_to_html(
            f"Hi {t.requester_name or 'there'},\n\n"
            f"Thanks for contacting Arkive support. We've opened ticket {t.ref} and "
            f"our team will get back to you shortly.\n\n"
            f"Subject: {t.subject}\nCategory: {_cat_label(t.category)}\n\n"
            f"Your message:\n{first_body}"),
        cta={"label": "View your ticket", "url": url},
        preheader=f"Ticket {t.ref} received")
    emailer.send(t.requester_email, f"[{t.ref}] {t.subject}", html=body,
                 text=f"We've opened ticket {t.ref}. View it at {url}", category="support")
    # Notify the support inbox.
    staff = emailer.render(
        f"New support ticket — {t.ref}",
        emailer.text_to_html(
            f"{t.requester_name} <{t.requester_email}> opened a {t.priority} "
            f"{_cat_label(t.category)} ticket.\n\nSubject: {t.subject}\n\n{first_body}"),
        cta={"label": "Open in admin", "url": f"{_portal_url()}/admin"},
        preheader=t.subject)
    try:
        emailer.send(_support_email(db), f"[{t.ref}] {t.subject}", html=staff, text=t.subject)
    except Exception:  # noqa: BLE001
        pass


def _email_staff_reply(t: SupportTicket, msg_body: str) -> None:
    url = f"{_portal_url()}/support/tickets/{t.id}"
    body = emailer.render(
        f"New reply on your ticket — {t.ref}",
        emailer.text_to_html(
            f"Hi {t.requester_name or 'there'},\n\n"
            f"Arkive support replied to your ticket {t.ref} ({t.subject}):\n\n{msg_body}"),
        cta={"label": "View & reply", "url": url},
        preheader="You have a new reply from Arkive support")
    emailer.send(t.requester_email, f"[{t.ref}] {t.subject}", html=body,
                 text=f"New reply on {t.ref}: {url}", category="support")


def _email_status(t: SupportTicket) -> None:
    url = f"{_portal_url()}/support/tickets/{t.id}"
    body = emailer.render(
        f"Your ticket is now {t.status} — {t.ref}",
        emailer.text_to_html(
            f"Hi {t.requester_name or 'there'},\n\n"
            f"The status of your ticket {t.ref} ({t.subject}) is now: {t.status}."),
        cta={"label": "View your ticket", "url": url},
        preheader=f"Ticket {t.ref} is {t.status}")
    emailer.send(t.requester_email, f"[{t.ref}] {t.subject} — {t.status}", html=body,
                 text=f"Ticket {t.ref} is now {t.status}: {url}", category="support")


# --------------------------------------------------------------------------- #
# Tickets — customer                                                          #
# --------------------------------------------------------------------------- #

@tickets_router.get("/meta")
def ticket_meta(principal: security.Principal = Depends(security.get_principal)):
    return {"categories": CATEGORIES, "priorities": sorted(_PRIORITIES)}


@tickets_router.get("/tickets")
def my_tickets(principal: security.Principal = Depends(security.get_principal),
               db: Session = Depends(get_db)):
    rows = (db.query(SupportTicket)
            .filter(SupportTicket.user_id == principal.user_id)
            .order_by(SupportTicket.last_activity_at.desc()).all())
    return {"tickets": [_ticket_out(t) for t in rows]}


class TicketIn(BaseModel):
    subject: str
    category: str = "other"
    priority: str = "normal"
    body: str


@tickets_router.post("/tickets")
def create_ticket(body: TicketIn,
                  principal: security.Principal = Depends(security.get_principal),
                  db: Session = Depends(get_db)):
    subject = (body.subject or "").strip()
    message = (body.body or "").strip()
    if not subject or not message:
        raise HTTPException(400, "subject and message are required")
    category = body.category if body.category in _CATEGORY_KEYS else "other"
    priority = body.priority if body.priority in _PRIORITIES else "normal"
    user = db.get(User, principal.user_id)
    name = (user.full_name if user else "") or (user.email if user else "")
    t = SupportTicket(
        ref=f"ARK-{secrets.token_hex(2).upper()}",
        tenant_id=principal.tenant_id, user_id=principal.user_id,
        subject=subject[:200], category=category, priority=priority, status="open",
        requester_email=(user.email if user else ""), requester_name=name,
        last_activity_at=_now())
    db.add(t)
    db.flush()
    db.add(TicketMessage(ticket_id=t.id, author_user_id=principal.user_id,
                         author_name=name, is_staff=False, body=message))
    db.commit()
    db.refresh(t)
    try:
        _email_new_ticket(db, t, message)
    except Exception:  # noqa: BLE001 — never fail the request on email
        pass
    audit.record(db, actor=principal.user_id, action="support.ticket_opened",
                 tenant_id=principal.tenant_id, resource=t.ref, category="activity")
    return _ticket_out(t, with_messages=True)


def _load_own_ticket(db: Session, ticket_id: str, principal: security.Principal) -> SupportTicket:
    t = db.get(SupportTicket, ticket_id)
    if t is None or t.user_id != principal.user_id:
        raise HTTPException(404, "not found")
    return t


@tickets_router.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: str,
               principal: security.Principal = Depends(security.get_principal),
               db: Session = Depends(get_db)):
    return _ticket_out(_load_own_ticket(db, ticket_id, principal), with_messages=True)


class ReplyIn(BaseModel):
    body: str


@tickets_router.post("/tickets/{ticket_id}/reply")
def reply_ticket(ticket_id: str, body: ReplyIn,
                 principal: security.Principal = Depends(security.get_principal),
                 db: Session = Depends(get_db)):
    t = _load_own_ticket(db, ticket_id, principal)
    message = (body.body or "").strip()
    if not message:
        raise HTTPException(400, "message is required")
    user = db.get(User, principal.user_id)
    name = (user.full_name if user else "") or t.requester_name
    db.add(TicketMessage(ticket_id=t.id, author_user_id=principal.user_id,
                         author_name=name, is_staff=False, body=message))
    # A customer reply re-opens a resolved ticket.
    if t.status in ("resolved", "closed"):
        t.status = "open"
    t.last_activity_at = _now()
    db.commit()
    db.refresh(t)
    return _ticket_out(t, with_messages=True)


@tickets_router.post("/tickets/{ticket_id}/close")
def close_ticket(ticket_id: str,
                 principal: security.Principal = Depends(security.get_principal),
                 db: Session = Depends(get_db)):
    t = _load_own_ticket(db, ticket_id, principal)
    t.status = "closed"
    t.last_activity_at = _now()
    db.add(TicketMessage(ticket_id=t.id, author_user_id=principal.user_id,
                         author_name=t.requester_name, is_staff=False,
                         body="Ticket closed by the customer."))
    db.commit()
    db.refresh(t)
    return _ticket_out(t, with_messages=True)


# --------------------------------------------------------------------------- #
# Tickets — support staff                                                     #
# --------------------------------------------------------------------------- #

@admin_router.get("/tickets", dependencies=[Depends(require_support_agent)])
def admin_list_tickets(status: str | None = None, category: str | None = None,
                       db: Session = Depends(get_db)):
    q = db.query(SupportTicket)
    if status in _STATUSES:
        q = q.filter(SupportTicket.status == status)
    if category in _CATEGORY_KEYS:
        q = q.filter(SupportTicket.category == category)
    rows = q.order_by(SupportTicket.last_activity_at.desc()).limit(500).all()
    counts = {s: 0 for s in _STATUSES}
    for (st,) in db.query(SupportTicket.status).all():
        if st in counts:
            counts[st] += 1
    return {"tickets": [_ticket_out(t) for t in rows], "counts": counts,
            "categories": CATEGORIES}


@admin_router.get("/tickets/{ticket_id}", dependencies=[Depends(require_support_agent)])
def admin_get_ticket(ticket_id: str, db: Session = Depends(get_db)):
    t = db.get(SupportTicket, ticket_id)
    if t is None:
        raise HTTPException(404, "not found")
    return _ticket_out(t, with_messages=True)


@admin_router.post("/tickets/{ticket_id}/reply", dependencies=[Depends(require_support_agent)])
def admin_reply(ticket_id: str, body: ReplyIn,
                principal: security.Principal = Depends(require_support_agent),
                db: Session = Depends(get_db)):
    t = db.get(SupportTicket, ticket_id)
    if t is None:
        raise HTTPException(404, "not found")
    message = (body.body or "").strip()
    if not message:
        raise HTTPException(400, "message is required")
    agent = db.get(User, principal.user_id)
    name = (agent.full_name if agent else "") or "Arkive Support"
    db.add(TicketMessage(ticket_id=t.id, author_user_id=principal.user_id,
                         author_name=name, is_staff=True, body=message))
    if t.status == "open":
        t.status = "pending"  # awaiting customer
    t.last_activity_at = _now()
    db.commit()
    db.refresh(t)
    try:
        _email_staff_reply(t, message)
    except Exception:  # noqa: BLE001
        pass
    audit.record(db, actor=principal.user_id, action="support.staff_reply",
                 resource=t.ref, category="admin")
    return _ticket_out(t, with_messages=True)


class TicketUpdate(BaseModel):
    status: str | None = None
    priority: str | None = None
    assignee_user_id: str | None = None


@admin_router.put("/tickets/{ticket_id}", dependencies=[Depends(require_support_agent)])
def admin_update_ticket(ticket_id: str, body: TicketUpdate,
                        principal: security.Principal = Depends(require_support_agent),
                        db: Session = Depends(get_db)):
    t = db.get(SupportTicket, ticket_id)
    if t is None:
        raise HTTPException(404, "not found")
    status_changed = False
    if body.status and body.status in _STATUSES and body.status != t.status:
        t.status = body.status
        status_changed = True
    if body.priority and body.priority in _PRIORITIES:
        t.priority = body.priority
    if body.assignee_user_id is not None:
        t.assignee_user_id = body.assignee_user_id or None
    t.last_activity_at = _now()
    db.commit()
    db.refresh(t)
    if status_changed:
        try:
            _email_status(t)
        except Exception:  # noqa: BLE001
            pass
    audit.record(db, actor=principal.user_id, action="support.ticket_updated",
                 resource=t.ref, category="admin",
                 detail={"status": t.status, "priority": t.priority})
    return _ticket_out(t, with_messages=True)
