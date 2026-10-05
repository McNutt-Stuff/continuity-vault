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


# Gmail (and its googlemail.com alias) ignore dots in the local part and treat a
# "+tag" suffix as the same mailbox, so andrea.boylston@gmail.com,
# andreaboy.lston@gmail.com and andreaboylston+news@googlemail.com are ONE inbox.
# Collapsing them is what lets a Google Contacts card match a hand-linked address.
_GMAIL_DOMAINS = {"gmail.com", "googlemail.com"}
# Providers where a "+tag" is an alias of the base mailbox (dots are only dropped
# for Gmail). Conservative list of well-known plus-addressing hosts.
_PLUS_ALIAS_DOMAINS = _GMAIL_DOMAINS | {
    "outlook.com", "hotmail.com", "live.com", "msn.com",
    "icloud.com", "me.com", "mac.com", "fastmail.com",
    "proton.me", "protonmail.com", "pm.me", "yahoo.com",
}


def normalize_email(raw: str) -> str | None:
    """Normalize an email to a stable match key. Lowercases/trims, then canonicalizes
    provider aliases of the SAME mailbox: Gmail/googlemail ignore dots in the local
    part and a '+tag' suffix (and googlemail.com == gmail.com); other well-known
    providers ignore the '+tag' alias."""
    e = str(raw or "").strip().lower()
    if not _EMAIL_RE.match(e):
        return None
    local, _, domain = e.rpartition("@")
    if domain == "googlemail.com":
        domain = "gmail.com"
    if domain in _PLUS_ALIAS_DOMAINS and "+" in local:
        local = local.split("+", 1)[0]
    if domain in _GMAIL_DOMAINS:
        local = local.replace(".", "")
    if not local:  # e.g. "+tag@gmail.com" or "...@gmail.com" of only dots
        return e
    return f"{local}@{domain}"


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


# A social handle identity must be PLATFORM-NAMESPACED ("instagram:john") so the
# same username on two networks never collides into one person. Bare handles
# (no namespace) are intentionally NOT matched — only connectors that emit a
# namespaced handle participate, which keeps contact merging conservative.
_HANDLE_RE = re.compile(r"^[a-z0-9_]+:[a-z0-9_.]{1,40}$")


def normalize_handle(raw: str) -> str | None:
    """Normalize a namespaced social handle to a stable match key, else None."""
    s = str(raw or "").strip().lower().lstrip("@")
    if ":" not in s:
        return None
    platform, _, handle = s.partition(":")
    handle = handle.strip().lstrip("@")
    if not platform or not handle:
        return None
    key = f"{platform}:{handle}"
    return key if _HANDLE_RE.match(key) else None



# Pull an email out of a value that may carry a display name ("Name <e@x.com>").
_EMAIL_FIND_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_ANGLE_RE = re.compile(r'^\s*"?([^"<>]*?)"?\s*<\s*([^>]+?)\s*>\s*$')


def parse_party(value: str) -> tuple[str, str, str] | None:
    """Parse a message from/to value that may be ``Name <email>``, ``"Name" <+phone>``
    or a bare identifier. Returns ``(type, normalized, display_name)`` or None — so
    the display name a mailbox carries isn't lost (``classify`` drops it, which left
    an email sender un-named and therefore unlinkable to the same person's phone)."""
    v = str(value or "").strip()
    if not v:
        return None
    name, inner = "", v
    m = _ANGLE_RE.match(v)
    if m:
        name = m.group(1).strip().strip('"').strip()
        inner = m.group(2).strip()
    em = _EMAIL_FIND_RE.search(inner) or _EMAIL_FIND_RE.search(v)
    if em:
        e = normalize_email(em.group(0))
        if e:
            # Outlook/Exchange internal pseudo-addresses (IPM.Note.*, meeting
            # responses) are never real people — don't make them contacts.
            local = e.split("@", 1)[0]
            if local.startswith("ipm.") or "schedule.meeting" in local:
                return None
            # A "name" that's itself an address/number isn't a real display name.
            if name and ("@" in name or re.fullmatch(r"[+()\-.\s\d]+", name)):
                name = ""
            return ("email", e, name)
    # Phone ONLY when the value is phone-shaped (digits/punctuation) — never strip
    # digits out of free text like "Me, +1201…" (a group-chat participant blob),
    # which would mint a phone whose raw blob then became a contact's display name.
    if re.fullmatch(r"[+()\-.\s\d]+", inner or ""):
        p = normalize_phone(inner)
        if p:
            return ("phone", p, name)
    # A platform-namespaced social handle ("instagram:john") is a handle identity.
    h = normalize_handle(inner)
    if h:
        return ("handle", h, name)
    return None


def split_addresses(value) -> list[str]:
    """Split a from/to value that may be a comma/semicolon-separated participant
    list ("Me, +1201…, John <j@x>") into individual addresses, WITHOUT splitting a
    comma inside a quoted display name ("Doe, John" <j@x>) or inside <>."""
    s = str(value or "")
    if "," not in s and ";" not in s:
        return [s] if s.strip() else []
    out: list[str] = []
    buf: list[str] = []
    depth = 0          # inside <>
    inq = False        # inside "..."
    for ch in s:
        if ch == '"':
            inq = not inq
            buf.append(ch)
        elif ch == "<":
            depth += 1
            buf.append(ch)
        elif ch == ">":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch in ",;" and depth == 0 and not inq:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return [p.strip() for p in out if p.strip()]



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
            if c is None:
                # A namespaced social handle (e.g. meta "instagram_handle") is a
                # handle identity even though it's neither an email nor a phone.
                nh = normalize_handle(str(raw))
                c = ("handle", nh) if nh else None
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
            if c is None:
                nh = normalize_handle(str(raw))
                c = ("handle", nh) if nh else None
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
