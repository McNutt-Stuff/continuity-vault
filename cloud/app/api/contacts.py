"""Unified Contacts API — "My Circles" personal relationship graph.

Read + curate the deduced people built by ``unified_contacts`` from the search
index. Personal, per-user feature gated by the ``unified_contacts_enabled`` flag.
Federation: contacts + the index they're mined from live on the tenant's node, so
these routes are proxied there (see ``api/node_proxy``).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import or_, func
from sqlalchemy.orm import Session

from .. import security, unified_contacts
from ..db import get_db
from ..models import (ContactIdentity, ContactSuggestion, Tenant, UnifiedContact,
                      User)

logger = logging.getLogger("cv.api.contacts")
router = APIRouter(prefix="/contacts", tags=["contacts"])

# Default customization — the user can override in account settings
# (User.contacts_prefs). Kept here so the feature works out of the box.
DEFAULT_RELATIONSHIPS = ["family", "partner", "friend", "colleague", "client",
                         "acquaintance", "other"]
DEFAULT_LABELS = ["favorite", "emergency", "work", "personal", "vip"]
CIRCLE_ORDER = {"inner": 0, "close": 1, "active": 2, "acquaintance": 3, "dormant": 4}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _guard(principal: security.Principal, tenant: Tenant, db: Session) -> User:
    """Personal feature: require the flag; the view is always the caller's OWN
    contacts (no cross-member access)."""
    user = db.get(User, principal.user_id)
    if not user:
        raise HTTPException(401, "no account")
    from .. import features
    if not features.resolve(user, tenant, "unified_contacts_enabled", db):
        raise HTTPException(403, "Unified Contacts is not enabled for this account.")
    return user


def _prefs(user: User) -> dict:
    p = dict(user.contacts_prefs or {})
    p.setdefault("relationships", DEFAULT_RELATIONSHIPS)
    p.setdefault("labels", DEFAULT_LABELS)
    p.setdefault("auto_link", True)
    return p


def _contact_view(c: UnifiedContact, *, full: bool = False) -> dict:
    out = {
        "id": c.id, "display_name": c.display_name, "nickname": c.nickname,
        "given_name": c.given_name, "family_name": c.family_name,
        "primary_email": c.primary_email, "primary_phone": c.primary_phone,
        "avatar_url": c.avatar_url, "circle": c.circle,
        "pinned_circle": c.pinned_circle, "relationship": c.relationship,
        "labels": c.labels or [], "starred": bool(c.starred), "hidden": bool(c.hidden),
        "custom_name": bool(c.custom_name), "derived_name": c.derived_name or "",
        "interaction_count": int(c.interaction_count or 0),
        "last_interaction_at": c.last_interaction_at.isoformat() if c.last_interaction_at else None,
        "first_interaction_at": c.first_interaction_at.isoformat() if c.first_interaction_at else None,
        "source_types": c.source_types or [], "stats": c.stats or {},
    }
    if full:
        out["notes"] = c.notes or ""
        out["details"] = c.details or {}
        out["important_dates"] = c.important_dates or []
    return out


def _identity_view(i: ContactIdentity) -> dict:
    return {
        "id": i.id, "kind": i.kind, "value": i.value, "raw_value": i.raw_value,
        "label": i.label, "source_type": i.source_type,
        "link_method": i.link_method, "confirmed": bool(i.confirmed),
        "confidence": float(i.confidence or 0),
    }


# --------------------------------------------------------------------------- #
# List + overview                                                             #
# --------------------------------------------------------------------------- #
@router.get("")
def list_contacts(q: str = "", circle: str | None = None, relationship: str | None = None,
                  label: str | None = None, source: str | None = None,
                  starred: bool = False, include_hidden: bool = False,
                  hidden_only: bool = False,
                  sort: str = "circle", limit: int = 500, offset: int = 0,
                  principal: security.Principal = Depends(security.get_principal),
                  tenant: Tenant = Depends(security.get_tenant),
                  db: Session = Depends(get_db)):
    user = _guard(principal, tenant, db)
    query = db.query(UnifiedContact).filter(
        UnifiedContact.tenant_id == tenant.id,
        UnifiedContact.owner_user_id == user.id)
    if hidden_only:
        # The "Ignored" view: ONLY the people the user chose to suppress.
        query = query.filter(UnifiedContact.hidden.is_(True))
    elif not include_hidden:
        query = query.filter(UnifiedContact.hidden.is_(False))
    if circle:
        query = query.filter(UnifiedContact.circle == circle)
    if relationship:
        query = query.filter(UnifiedContact.relationship == relationship)
    if starred:
        query = query.filter(UnifiedContact.starred.is_(True))
    if q:
        like = f"%{q.strip().lower()}%"
        query = query.filter(or_(UnifiedContact.sort_key.like(like),
                                 UnifiedContact.primary_email.like(like),
                                 UnifiedContact.primary_phone.like(like)))
    rows = query.all()
    if label:
        rows = [c for c in rows if label in (c.labels or [])]
    if source:
        rows = [c for c in rows if source in (c.source_types or [])]
    # Sort: circle (closeness) then interaction volume; or by recency / name.
    if sort == "recent":
        rows.sort(key=lambda c: (c.last_interaction_at or datetime.min), reverse=True)
    elif sort == "name":
        rows.sort(key=lambda c: c.sort_key or "")
    elif sort == "frequency":
        rows.sort(key=lambda c: -(c.interaction_count or 0))
    else:  # circle
        rows.sort(key=lambda c: (CIRCLE_ORDER.get(c.circle, 9),
                                 -(c.interaction_count or 0)))
    total = len(rows)
    page = rows[offset:offset + max(1, min(1000, limit))]
    return {"total": total, "contacts": [_contact_view(c) for c in page]}


@router.get("/overview")
def overview(principal: security.Principal = Depends(security.get_principal),
             tenant: Tenant = Depends(security.get_tenant),
             db: Session = Depends(get_db)):
    user = _guard(principal, tenant, db)
    return _overview_payload(db, tenant, user)


def _overview_payload(db: Session, tenant: Tenant, user: User) -> dict:
    rows = db.query(UnifiedContact).filter(
        UnifiedContact.tenant_id == tenant.id,
        UnifiedContact.owner_user_id == user.id,
        UnifiedContact.hidden.is_(False)).all()
    by_circle: dict[str, int] = {k: 0 for k in CIRCLE_ORDER}
    by_source: dict[str, int] = {}
    by_relationship: dict[str, int] = {}
    for c in rows:
        by_circle[c.circle] = by_circle.get(c.circle, 0) + 1
        for s in (c.source_types or []):
            by_source[s] = by_source.get(s, 0) + 1
        if c.relationship:
            by_relationship[c.relationship] = by_relationship.get(c.relationship, 0) + 1
    pending = db.query(ContactSuggestion).filter(
        ContactSuggestion.tenant_id == tenant.id,
        ContactSuggestion.owner_user_id == user.id,
        ContactSuggestion.status == "pending").count()
    ignored = db.query(UnifiedContact).filter(
        UnifiedContact.tenant_id == tenant.id,
        UnifiedContact.owner_user_id == user.id,
        UnifiedContact.hidden.is_(True)).count()
    # When the graph was last (re)built — by a manual Rebuild or the scheduler sweep.
    last_built = db.query(func.max(UnifiedContact.updated_at)).filter(
        UnifiedContact.tenant_id == tenant.id,
        UnifiedContact.owner_user_id == user.id).scalar()
    top = sorted(rows, key=lambda c: -(c.interaction_count or 0))[:8]
    return {"total": len(rows), "by_circle": by_circle, "by_source": by_source,
            "by_relationship": by_relationship, "pending_suggestions": pending,
            "ignored": ignored,
            "last_built_at": last_built.isoformat() if last_built else None,
            "top_contacts": [_contact_view(c) for c in top],
            "prefs": _prefs(user)}


def _default_page(db: Session, tenant: Tenant, user: User, limit: int = 500) -> list[dict]:
    """Default-sorted (closeness) first page — used to seed the UI right after a
    rebuild, so it shows the just-built contacts without waiting for the CP replica."""
    rows = db.query(UnifiedContact).filter(
        UnifiedContact.tenant_id == tenant.id,
        UnifiedContact.owner_user_id == user.id,
        UnifiedContact.hidden.is_(False)).all()
    rows.sort(key=lambda c: (CIRCLE_ORDER.get(c.circle, 9), -(c.interaction_count or 0)))
    return [_contact_view(c) for c in rows[:max(1, min(1000, limit))]]


@router.get("/settings")
def get_settings_ep(principal: security.Principal = Depends(security.get_principal),
                    tenant: Tenant = Depends(security.get_tenant),
                    db: Session = Depends(get_db)):
    user = _guard(principal, tenant, db)
    return {"prefs": _prefs(user), "contact_linking_enabled": bool(user.contact_linking_enabled)}


class SettingsBody(BaseModel):
    relationships: list[str] | None = None
    labels: list[str] | None = None
    auto_link: bool | None = None


@router.put("/settings")
def put_settings(body: SettingsBody,
                 principal: security.Principal = Depends(security.get_principal),
                 tenant: Tenant = Depends(security.get_tenant),
                 db: Session = Depends(get_db)):
    user = _guard(principal, tenant, db)
    p = _prefs(user)
    if body.relationships is not None:
        p["relationships"] = [s.strip() for s in body.relationships if s.strip()]
    if body.labels is not None:
        p["labels"] = [s.strip() for s in body.labels if s.strip()]
    if body.auto_link is not None:
        p["auto_link"] = bool(body.auto_link)
    user.contacts_prefs = p
    db.commit()
    return {"prefs": p}


# --------------------------------------------------------------------------- #
# Circles relationship graph                                                  #
# --------------------------------------------------------------------------- #
@router.get("/graph")
def circles_graph(limit: int = 60, circle: str | None = None, within_days: int = 0,
                  principal: security.Principal = Depends(security.get_principal),
                  tenant: Tenant = Depends(security.get_tenant),
                  db: Session = Depends(get_db)):
    """Nodes + edges for the "My Circles" map: YOU at the center, each contact a
    node placed by its circle tier, edges weighted by interaction volume. Supports
    narrowing by circle tier + recent-activity window so a big graph stays readable."""
    user = _guard(principal, tenant, db)
    query = (db.query(UnifiedContact)
             .filter(UnifiedContact.tenant_id == tenant.id,
                     UnifiedContact.owner_user_id == user.id,
                     UnifiedContact.hidden.is_(False)))
    if circle:
        query = query.filter(UnifiedContact.circle == circle)
    if within_days and within_days > 0:
        cutoff = _now() - timedelta(days=within_days)
        query = query.filter(UnifiedContact.last_interaction_at >= cutoff)
    rows = (query.order_by(UnifiedContact.interaction_count.desc())
            .limit(max(1, min(300, limit))).all())
    me_name = (user.display_name or user.full_name or "You")
    nodes = [{"id": "me", "name": me_name, "circle": "me", "me": True}]
    edges = []
    maxi = max((int(c.interaction_count or 0) for c in rows), default=1) or 1
    for c in rows:
        nodes.append({
            "id": c.id, "name": c.display_name, "circle": c.circle,
            "relationship": c.relationship, "labels": c.labels or [],
            "interaction_count": int(c.interaction_count or 0),
            "weight": round((int(c.interaction_count or 0) / maxi), 3),
            "starred": bool(c.starred),
        })
        edges.append({"source": "me", "target": c.id,
                      "weight": round((int(c.interaction_count or 0) / maxi), 3),
                      "circle": c.circle})
    return {"nodes": nodes, "edges": edges}


@router.get("/sources")
def contact_sources(principal: security.Principal = Depends(security.get_principal),
                    tenant: Tenant = Depends(security.get_tenant),
                    db: Session = Depends(get_db)):
    """Which sources are feeding the contact graph — contacts/interactions/identifiers
    linked per source, plus how many of each source's docs are in the index (so a
    source with indexed docs but few contacts is a visible parsing gap)."""
    user = _guard(principal, tenant, db)
    return {"sources": unified_contacts.sources_breakdown(db, user)}


# --------------------------------------------------------------------------- #
# Suggestions                                                                 #
# --------------------------------------------------------------------------- #
@router.get("/suggestions")
def list_suggestions(principal: security.Principal = Depends(security.get_principal),
                     tenant: Tenant = Depends(security.get_tenant),
                     db: Session = Depends(get_db)):
    user = _guard(principal, tenant, db)

    def _idents(cid: str) -> list[dict]:
        rows = (db.query(ContactIdentity)
                .filter(ContactIdentity.contact_id == cid)
                .order_by(ContactIdentity.kind, ContactIdentity.value).limit(20).all())
        return [{"kind": i.kind, "value": i.value, "source_type": i.source_type,
                 "link_method": i.link_method} for i in rows]

    out = []
    for s in (db.query(ContactSuggestion)
              .filter(ContactSuggestion.tenant_id == tenant.id,
                      ContactSuggestion.owner_user_id == user.id,
                      ContactSuggestion.status == "pending")
              .order_by(ContactSuggestion.confidence.desc()).limit(200).all()):
        primary = db.get(UnifiedContact, s.contact_id)
        other = db.get(UnifiedContact, s.merge_contact_id) if s.merge_contact_id else None
        # Why we think they match: these name-based suggestions share NO identifier
        # (if they did they'd have been auto-unioned), so the basis is the NAME —
        # we return each side's identifiers so the user can see what's in play.
        match = {
            "basis": "name",
            "name": (primary.display_name if primary else "") or (other.display_name if other else ""),
            "confidence": float(s.confidence or 0),
        }
        out.append({
            "id": s.id, "kind": s.kind, "reason": s.reason,
            "confidence": float(s.confidence or 0),
            "match": match,
            "contact": _contact_view(primary) if primary else None,
            "merge_contact": _contact_view(other) if other else None,
            "contact_identifiers": _idents(primary.id) if primary else [],
            "merge_identifiers": _idents(other.id) if other else [],
            "identity": ({"kind": s.identity_kind, "value": s.identity_value,
                          "raw": s.identity_raw, "source": s.identity_source}
                         if s.kind == "link_identity" else None),
        })
    return {"suggestions": out}


@router.post("/suggestions/{sid}/{action}")
def act_suggestion(sid: str, action: str,
                   principal: security.Principal = Depends(security.get_principal),
                   tenant: Tenant = Depends(security.get_tenant),
                   db: Session = Depends(get_db)):
    user = _guard(principal, tenant, db)
    s = db.get(ContactSuggestion, sid)
    if not s or s.owner_user_id != user.id:
        raise HTTPException(404, "suggestion not found")
    if action not in ("accept", "dismiss"):
        raise HTTPException(400, "action must be accept or dismiss")
    if action == "dismiss":
        s.status = "dismissed"
        s.updated_at = _now()
        db.commit()
        return {"ok": True, "status": "dismissed"}
    # accept
    removed_id = None
    primary_id = s.contact_id
    if s.kind == "merge" and s.merge_contact_id:
        unified_contacts.merge(db, user, s.contact_id, s.merge_contact_id)
        removed_id = s.merge_contact_id
    elif s.kind == "link_identity":
        db.add(ContactIdentity(
            tenant_id=tenant.id, owner_user_id=user.id, contact_id=s.contact_id,
            kind=s.identity_kind, value=s.identity_value,
            raw_value=s.identity_raw or s.identity_value,
            source_type=s.identity_source, link_method="manual", confirmed=True,
            confidence=1.0, created_at=_now(), updated_at=_now()))
    s.status = "accepted"
    s.updated_at = _now()
    db.commit()
    # Live result from THIS node so the portal updates instantly (CP replica lags
    # ~30s): the updated target contact + the id removed by a merge + a fresh overview.
    primary = db.get(UnifiedContact, primary_id)
    return {"ok": True, "status": "accepted", "removed_id": removed_id,
            "primary": _contact_detail(db, primary) if primary else None,
            "overview": _overview_payload(db, tenant, user)}


# --------------------------------------------------------------------------- #
# Merge + rebuild                                                             #
# --------------------------------------------------------------------------- #
class MergeBody(BaseModel):
    primary_id: str
    other_id: str


@router.post("/merge")
def merge_contacts(body: MergeBody,
                   principal: security.Principal = Depends(security.get_principal),
                   tenant: Tenant = Depends(security.get_tenant),
                   db: Session = Depends(get_db)):
    user = _guard(principal, tenant, db)
    if body.primary_id == body.other_id:
        raise HTTPException(400, "cannot merge a contact into itself")
    unified_contacts.merge(db, user, body.primary_id, body.other_id)
    # Return the live result from THIS node (the merged target + the id that was
    # removed + a fresh overview/page) so the portal updates immediately instead of
    # reading the CP replica, which only catches up on the next push (~30s).
    primary = db.get(UnifiedContact, body.primary_id)
    return {"ok": True, "removed_id": body.other_id,
            "primary": _contact_detail(db, primary) if primary else None,
            "overview": _overview_payload(db, tenant, user),
            "page": _default_page(db, tenant, user)}


@router.post("/rebuild")
def rebuild_now(principal: security.Principal = Depends(security.get_principal),
                tenant: Tenant = Depends(security.get_tenant),
                db: Session = Depends(get_db)):
    user = _guard(principal, tenant, db)
    n = unified_contacts.rebuild(db, user)
    # Return the freshly-built overview + first page from THIS node, so the portal
    # renders the result immediately instead of reading the CP replica, which only
    # catches up on the next node→CP replication push (~30s → "mapped N but zeros").
    return {"ok": True, "contacts": n,
            "overview": _overview_payload(db, tenant, user),
            "page": _default_page(db, tenant, user)}


# --------------------------------------------------------------------------- #
# Create a contact manually                                                   #
# --------------------------------------------------------------------------- #
class CreateContactBody(BaseModel):
    display_name: str
    emails: list[str] | None = None
    phones: list[str] | None = None
    nickname: str | None = None
    relationship: str | None = None
    labels: list[str] | None = None
    notes: str | None = None


@router.post("")
def create_contact(body: CreateContactBody,
                   principal: security.Principal = Depends(security.get_principal),
                   tenant: Tenant = Depends(security.get_tenant),
                   db: Session = Depends(get_db)):
    """Create a contact by hand. Any email/phone is stored as a MANUAL identity so a
    rebuild preserves it AND matches a future message/card carrying that identifier
    onto this contact (instead of minting a duplicate). If a provided identifier
    already belongs to a contact, that contact is reused (named + returned)."""
    user = _guard(principal, tenant, db)
    from .. import contacts as _c
    name = (body.display_name or "").strip()
    if not name:
        raise HTTPException(400, "a name is required")
    # Normalize the provided identifiers.
    idents: list[tuple[str, str]] = []
    for raw in (body.emails or []):
        n = _c.normalize_email(str(raw))
        if n and ("email", n) not in idents:
            idents.append(("email", n))
    for raw in (body.phones or []):
        n = _c.normalize_phone(str(raw))
        if n and ("phone", n) not in idents:
            idents.append(("phone", n))

    # Reuse an existing contact if any identifier already maps to one.
    contact = None
    for (kind, value) in idents:
        ex = (db.query(ContactIdentity)
              .filter(ContactIdentity.tenant_id == tenant.id,
                      ContactIdentity.owner_user_id == user.id,
                      ContactIdentity.kind == kind,
                      ContactIdentity.value == value).first())
        if ex:
            contact = db.get(UnifiedContact, ex.contact_id)
            if contact and contact.owner_user_id == user.id:
                break
            contact = None

    if contact is None:
        contact = UnifiedContact(
            tenant_id=tenant.id, owner_user_id=user.id, circle="acquaintance",
            created_at=_now())
        db.add(contact)
        db.flush()
    contact.display_name = name[:200]
    contact.sort_key = name.lower()[:200]
    contact.custom_name = True
    parts = name.split()
    contact.given_name = contact.given_name or (parts[0][:120] if parts else "")
    contact.family_name = contact.family_name or (parts[-1][:120] if len(parts) > 1 else "")
    if body.nickname is not None:
        contact.nickname = body.nickname.strip()
    if body.relationship is not None:
        contact.relationship = body.relationship.strip()
    if body.labels is not None:
        contact.labels = sorted({s.strip() for s in body.labels if s.strip()})
    if body.notes is not None:
        contact.notes = body.notes
    contact.updated_at = _now()

    for (kind, value) in idents:
        exists = (db.query(ContactIdentity)
                  .filter(ContactIdentity.contact_id == contact.id,
                          ContactIdentity.kind == kind,
                          ContactIdentity.value == value).first())
        if exists:
            exists.link_method = "manual"
            exists.confirmed = True
            exists.updated_at = _now()
            continue
        db.add(ContactIdentity(
            tenant_id=tenant.id, owner_user_id=user.id, contact_id=contact.id,
            kind=kind, value=value[:255], raw_value=value[:255],
            source_type="manual", link_method="manual", confirmed=True,
            confidence=1.0, last_seen=_now(), created_at=_now(), updated_at=_now()))
    if idents and not contact.primary_email:
        contact.primary_email = next((v for (k, v) in idents if k == "email"), "")
    if idents and not contact.primary_phone:
        contact.primary_phone = next((v for (k, v) in idents if k == "phone"), "")
    db.commit()
    return _contact_detail(db, contact)


# --------------------------------------------------------------------------- #
# Single contact: detail, exchanges, curation, identities                     #
# --------------------------------------------------------------------------- #
@router.get("/{cid}")
def get_contact(cid: str,
                principal: security.Principal = Depends(security.get_principal),
                tenant: Tenant = Depends(security.get_tenant),
                db: Session = Depends(get_db)):
    user = _guard(principal, tenant, db)
    c = db.get(UnifiedContact, cid)
    if not c or c.owner_user_id != user.id:
        raise HTTPException(404, "contact not found")
    return _contact_detail(db, c)


def _contact_detail(db: Session, c: UnifiedContact) -> dict:
    ids = (db.query(ContactIdentity)
           .filter(ContactIdentity.contact_id == c.id)
           .order_by(ContactIdentity.kind, ContactIdentity.value).all())
    return {**_contact_view(c, full=True),
            "identities": [_identity_view(i) for i in ids]}


@router.get("/{cid}/exchanges")
def contact_exchanges(cid: str, limit: int = 50, offset: int = 0,
                      principal: security.Principal = Depends(security.get_principal),
                      tenant: Tenant = Depends(security.get_tenant),
                      db: Session = Depends(get_db)):
    user = _guard(principal, tenant, db)
    c = db.get(UnifiedContact, cid)
    if not c or c.owner_user_id != user.id:
        raise HTTPException(404, "contact not found")
    return unified_contacts.exchanges(db, user, c, limit=limit, offset=offset)


class ContactUpdate(BaseModel):
    display_name: str | None = None
    nickname: str | None = None
    relationship: str | None = None
    labels: list[str] | None = None
    pinned_circle: str | None = None
    starred: bool | None = None
    hidden: bool | None = None
    notes: str | None = None
    details: dict | None = None
    reset_name: bool | None = None  # drop the custom name, revert to the deduced one


@router.put("/{cid}")
def update_contact(cid: str, body: ContactUpdate,
                   principal: security.Principal = Depends(security.get_principal),
                   tenant: Tenant = Depends(security.get_tenant),
                   db: Session = Depends(get_db)):
    user = _guard(principal, tenant, db)
    c = db.get(UnifiedContact, cid)
    if not c or c.owner_user_id != user.id:
        raise HTTPException(404, "contact not found")
    if body.reset_name:
        # Revert to the auto-deduced name; rebuild resumes managing it.
        c.custom_name = False
        if c.derived_name:
            c.display_name = c.derived_name
            c.sort_key = c.derived_name.lower()
    elif body.display_name is not None and body.display_name.strip():
        # A user-set name is pinned (custom_name) so a rebuild never reverts it.
        c.display_name = body.display_name.strip()
        c.sort_key = c.display_name.lower()
        c.custom_name = True
    if body.nickname is not None:
        c.nickname = body.nickname.strip()
    if body.relationship is not None:
        c.relationship = body.relationship.strip()
    if body.labels is not None:
        c.labels = sorted({s.strip() for s in body.labels if s.strip()})
    if body.pinned_circle is not None:
        pin = body.pinned_circle.strip()
        if pin and pin not in CIRCLE_ORDER:
            raise HTTPException(400, "invalid circle")
        c.pinned_circle = pin
        if pin:
            c.circle = pin
    if body.starred is not None:
        c.starred = bool(body.starred)
    if body.hidden is not None:
        c.hidden = bool(body.hidden)
    if body.notes is not None:
        c.notes = body.notes
    if body.details is not None:
        c.details = body.details
    c.updated_at = _now()
    db.commit()
    return _contact_view(c, full=True)


class IdentityBody(BaseModel):
    kind: str
    value: str
    label: str | None = ""


@router.post("/{cid}/identities")
def add_identity(cid: str, body: IdentityBody,
                 principal: security.Principal = Depends(security.get_principal),
                 tenant: Tenant = Depends(security.get_tenant),
                 db: Session = Depends(get_db)):
    """Manually link an identifier to a contact (normalized like the engine does)."""
    user = _guard(principal, tenant, db)
    c = db.get(UnifiedContact, cid)
    if not c or c.owner_user_id != user.id:
        raise HTTPException(404, "contact not found")
    from .. import contacts as _c
    kind = body.kind.strip().lower()
    raw = body.value.strip()
    if kind == "email":
        norm = _c.normalize_email(raw)
    elif kind == "phone":
        norm = _c.normalize_phone(raw)
    else:
        norm = raw.lower()
    if not norm:
        raise HTTPException(400, "could not normalize that identifier")
    existing = (db.query(ContactIdentity)
                .filter(ContactIdentity.tenant_id == tenant.id,
                        ContactIdentity.owner_user_id == user.id,
                        ContactIdentity.kind == kind,
                        ContactIdentity.value == norm).first())
    if existing:
        existing.contact_id = cid
        existing.link_method = "manual"
        existing.confirmed = True
        existing.updated_at = _now()
    else:
        db.add(ContactIdentity(
            tenant_id=tenant.id, owner_user_id=user.id, contact_id=cid,
            kind=kind, value=norm, raw_value=raw, label=(body.label or "").strip(),
            link_method="manual", confirmed=True, confidence=1.0,
            created_at=_now(), updated_at=_now()))
    db.commit()
    db.refresh(c)
    return _contact_detail(db, c)


@router.delete("/{cid}/identities/{iid}")
def remove_identity(cid: str, iid: str,
                    principal: security.Principal = Depends(security.get_principal),
                    tenant: Tenant = Depends(security.get_tenant),
                    db: Session = Depends(get_db)):
    user = _guard(principal, tenant, db)
    i = db.get(ContactIdentity, iid)
    if not i or i.owner_user_id != user.id or i.contact_id != cid:
        raise HTTPException(404, "identity not found")
    db.delete(i)
    db.commit()
    c = db.get(UnifiedContact, cid)
    if not c or c.owner_user_id != user.id:
        raise HTTPException(404, "contact not found")
    return _contact_detail(db, c)
