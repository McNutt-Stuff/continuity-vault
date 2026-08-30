"""
Contact linking — resolve raw phone numbers / email addresses in messages to a
person's name using a directory built from the user's contact sources.

Many messages (iMessage, SMS, some chats) only carry a phone number or bare
address. This module builds a per-user directory (``ContactLink``) that maps a
NORMALIZED identifier to a contact display name gathered from any contact source
(Google Contacts, iCloud, …), so search results and threads can show the name and
indicate it was linked from another source.

Design goals:
- Normalize identifiers so ``+12015771404``, ``2015771404`` and ``201-577-1404``
  all resolve to the same key.
- Source-agnostic + extensible: any source whose contact records expose phone/
  email values, and any message source whose metadata carries a from/to, works
  automatically (identifiers are discovered via the canonical attribute aliases).
- Opt-in per user (``User.contact_linking_enabled``); built by a node scheduler.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .models import ContactLink, SearchDocument, User, Vault
from .taxonomy import canonical_attr

logger = logging.getLogger("cv.contacts")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Contact-record metadata keys that carry identifiers (matched as substrings so
# variations like "mobile_phone" / "home_email" are covered).
_ID_KEY_HINTS = ("phone", "mobile", "cell", "tel", "email", "mail", "number", "handle")
# Message metadata keys (canonical) that carry a sender/recipient identifier.
_MSG_ID_CANON = {"from", "to", "cc", "bcc", "phone"}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_phone(raw: str) -> str | None:
    """Normalize a phone number to a stable match key. Strips formatting and, for
    NANP-style numbers, reduces to the 10-digit national form so international and
    local spellings of the same number collide. Returns None if it can't be a
    phone (too few digits)."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) < 7:
        return None
    # Drop a leading US/Canada country code so +1-201-577-1404 == 201-577-1404.
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    # For other long international numbers, key on the last 10 digits (best-effort
    # cross-format match without a full libphonenumber dependency).
    if len(digits) > 11:
        digits = digits[-10:]
    return digits


def normalize_email(raw: str) -> str | None:
    e = str(raw or "").strip().lower()
    return e if _EMAIL_RE.match(e) else None


def classify(value: str) -> tuple[str, str] | None:
    """Classify a raw value as ('email'|'phone', normalized_key), else None."""
    v = str(value or "").strip()
    if not v:
        return None
    if "@" in v:
        e = normalize_email(v)
        return ("email", e) if e else None
    # Only treat as a phone when it's plausibly a phone (mostly digits/punctuation).
    if re.fullmatch(r"[+()\-.\s\d]+", v):
        p = normalize_phone(v)
        return ("phone", p) if p else None
    return None


def _iter_values(v):
    if isinstance(v, (list, tuple)):
        for x in v:
            yield from _iter_values(x)
    elif isinstance(v, dict):
        for x in v.values():
            yield from _iter_values(x)
    elif v not in (None, ""):
        yield v


def contact_identifiers(meta: dict) -> list[tuple[str, str]]:
    """Extract (type, normalized) identifiers from a CONTACT record's metadata —
    from any key that hints at a phone/email, source-agnostically."""
    out: list[tuple[str, str]] = []
    seen: set = set()
    for k, v in (meta or {}).items():
        kl = str(k).lower()
        if not any(h in kl for h in _ID_KEY_HINTS):
            continue
        for raw in _iter_values(v):
            c = classify(str(raw))
            if c and c not in seen:
                seen.add(c)
                out.append(c)
    return out


def message_identifiers(meta: dict) -> list[tuple[str, str, str]]:
    """(type, normalized, raw) identifiers from a MESSAGE's from/to metadata."""
    out: list[tuple[str, str, str]] = []
    for k, v in (meta or {}).items():
        if canonical_attr(k) not in _MSG_ID_CANON:
            continue
        for raw in _iter_values(v):
            c = classify(str(raw))
            if c:
                out.append((c[0], c[1], str(raw)))
    return out


def _user_vault_ids(db: Session, user: User) -> list[str]:
    rows = db.query(Vault.id).filter(Vault.owner_user_id == user.id).all()
    return [r[0] for r in rows]


def build_directory(db: Session, user: User) -> int:
    """(Re)build the contact directory for one user from their contact-category
    SearchDocuments. Wipe-and-rebuild so removed contacts drop out. Returns the
    number of identifier links written."""
    vids = _user_vault_ids(db, user)
    q = db.query(SearchDocument.title, SearchDocument.source_type,
                 SearchDocument.object_id, SearchDocument.meta).filter(
        SearchDocument.tenant_id == user.tenant_id,
        SearchDocument.category == "contact",
        SearchDocument.is_current.is_(True))
    if vids:
        q = q.filter(SearchDocument.vault_id.in_(vids))
    links: list[ContactLink] = []
    now = _now()
    seen: set = set()  # (type, identifier, source_object_id)
    for title, source_type, object_id, meta in q.all():
        name = (title or "").strip()
        if not name:
            continue
        for ident_type, ident in contact_identifiers(meta or {}):
            key = (ident_type, ident, object_id)
            if key in seen:
                continue
            seen.add(key)
            links.append(ContactLink(
                tenant_id=user.tenant_id, owner_user_id=user.id,
                identifier_type=ident_type, identifier=ident,
                display_name=name, source_type=source_type or "",
                source_object_id=object_id or "", updated_at=now, created_at=now))
    # Replace this user's directory atomically.
    db.query(ContactLink).filter(ContactLink.owner_user_id == user.id).delete()
    if links:
        db.bulk_save_objects(links)
    db.commit()
    logger.info("contact directory rebuilt for %s: %d link(s)", user.id, len(links))
    return len(links)


def resolve(db: Session, tenant_id: str, owner_user_id: str,
            identifiers: list[tuple[str, str]]) -> dict[str, dict]:
    """Batch-resolve normalized identifiers → {identifier: {name, source_type,
    identifier_type}}. Most recently updated link wins for a given identifier."""
    keys = {ident for (_t, ident) in identifiers if ident}
    if not keys:
        return {}
    out: dict[str, dict] = {}
    rows = (db.query(ContactLink)
            .filter(ContactLink.tenant_id == tenant_id,
                    ContactLink.owner_user_id == owner_user_id,
                    ContactLink.identifier.in_(list(keys)))
            .order_by(ContactLink.updated_at.desc()).all())
    for r in rows:
        if r.identifier not in out:  # first (newest) wins
            out[r.identifier] = {"name": r.display_name,
                                 "source_type": r.source_type,
                                 "identifier_type": r.identifier_type}
    return out
