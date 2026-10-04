"""Unified Contacts engine — "My Circles" personal relationship graph.

Builds a deduced, cross-source view of the PEOPLE in a user's life by MINING THE
EXISTING SEARCH INDEX. It never runs a new extractor and never hooks ingest: it
reads the ``SearchDocument`` rows the connectors already produced —
``category == "contact"`` records (people from Google/iCloud/Outlook/LinkedIn/…)
and ``category in (message, social)`` / email docs whose ``meta`` carries
from/to/cc/bcc — exactly like ``contacts.build_directory`` does. COLLECT ONCE,
NORMALIZE ONCE, REUSE EVERYWHERE.

For each deduced person it links every identifier/handle/source-object we can
attribute to them (``ContactIdentity``), computes interaction analytics
(frequency, timeline, direction, methods/sources, volume), derives a closeness
"circle" tier, and raises conservative link/merge ``ContactSuggestion``s for
anything ambiguous. User curation (relationship, labels, pinned circle, notes,
rich details, starred/hidden, and manual/confirmed links) is PRESERVED across
rebuilds.

Runs per-user on the box that owns the tenant (customer node in federated mode,
else the control plane) — the same place the index lives — driven by a scheduler
sweep, opt-in via ``User.contact_linking_enabled``.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import contacts
from .models import (ConnectorAccount, ContactIdentity, ContactSuggestion,
                     SearchDocument, UnifiedContact, User, Vault)
from .taxonomy import canonical_attr

logger = logging.getLogger("cv.unified_contacts")

# Categories we mine. Contact records seed people; message/social/email docs
# contribute interactions (and surface people we only ever messaged).
_PERSON_CATEGORIES = ("contact",)
_INTERACTION_CATEGORIES = ("message", "social")
# Email lives under the "message" category in the taxonomy, but guard doc_type too
# so a future split still works.
_INTERACTION_DOC_TYPES = ("email", "message", "post", "comment", "dm", "chat")

# Closeness tiers, widest → tightest. Derived from interaction volume + recency,
# overridable per contact via ``pinned_circle``.
CIRCLES = ("inner", "close", "active", "acquaintance", "dormant")

# Common English given-name diminutives → canonical, so "Rob" matches "Robert",
# "Bill" matches "William", etc. Bidirectional lookup is built below. Deliberately
# high-signal only (used to SUGGEST, not silently merge).
_NICKNAMES: dict[str, str] = {
    "rob": "robert", "robbie": "robert", "bob": "robert", "bobby": "robert",
    "bill": "william", "billy": "william", "will": "william", "willie": "william",
    "liam": "william", "jim": "james", "jimmy": "james", "jamie": "james",
    "mike": "michael", "mick": "michael", "mikey": "michael",
    "dave": "david", "davey": "david", "tom": "thomas", "tommy": "thomas",
    "dick": "richard", "rick": "richard", "ricky": "richard", "rich": "richard",
    "rich ": "richard", "chuck": "charles", "charlie": "charles", "chas": "charles",
    "joe": "joseph", "joey": "joseph", "tony": "anthony", "ant": "anthony",
    "steve": "steven", "stevie": "steven", " steph": "stephen", "chris": "christopher",
    "topher": "christopher", "matt": "matthew", "matty": "matthew",
    "dan": "daniel", "danny": "daniel", "ben": "benjamin", "benny": "benjamin",
    "sam": "samuel", "sammy": "samuel", "nick": "nicholas", "nicky": "nicholas",
    "andy": "andrew", "drew": "andrew", "ed": "edward", "eddie": "edward",
    "ted": "edward", "teddy": "edward", "ron": "ronald", "ronnie": "ronald",
    "pat": "patrick", "paddy": "patrick", "greg": "gregory", "jeff": "jeffrey",
    "ken": "kenneth", "kenny": "kenneth", "larry": "lawrence", "gabe": "gabriel",
    "nate": "nathaniel", "zach": "zachary", "zack": "zachary", "alex": "alexander",
    "xander": "alexander", "fred": "frederick", "freddie": "frederick",
    "phil": "philip", "vince": "vincent", "vinny": "vincent", "gus": "augustus",
    "art": "arthur", "artie": "arthur", "frank": "franklin", "hank": "henry",
    "harry": "henry", "jack": "john", "johnny": "john", "jon": "jonathan",
    # Female
    "liz": "elizabeth", "lizzie": "elizabeth", "beth": "elizabeth", "betty": "elizabeth",
    "eliza": "elizabeth", "kate": "katherine", "katie": "katherine", "kathy": "katherine",
    "kat": "katherine", "cathy": "catherine", "sue": "susan", "susie": "susan",
    "maggie": "margaret", "meg": "margaret", "peggy": "margaret", "marge": "margaret",
    "jen": "jennifer", "jenny": "jennifer", "becky": "rebecca", "becca": "rebecca",
    "cindy": "cynthia", "deb": "deborah", "debbie": "deborah", "chris ": "christine",
    "chrissy": "christine", "pam": "pamela", "pammy": "pamela", "trish": "patricia",
    "patty": "patricia", "val": "valerie", "vicky": "victoria", "vick": "victoria",
    "steph": "stephanie", "steffi": "stephanie", "tina": "christina",
    "nancy": "ann", "annie": "ann", "fran": "frances", "gail": "abigail",
    "abby": "abigail", "allie": "alison", "andi": "andrea", "angie": "angela",
    "barb": "barbara", "babs": "barbara", "carrie": "caroline", "connie": "constance",
    "dot": "dorothy", "dottie": "dorothy", "ginny": "virginia", "josie": "josephine",
    "mandy": "amanda", "mel": "melissa", "missy": "melissa", "nat": "natalie",
    "sandy": "sandra", "terri": "theresa", "wendy": "gwendolyn",
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _canon_given(token: str) -> str:
    """Canonicalize a given name through the nickname map (Rob→robert)."""
    t = (token or "").strip().lower().strip(".")
    return _NICKNAMES.get(t, t)


def _norm_name(name: str) -> str:
    return " ".join((name or "").strip().lower().split())


def _sort_key(name: str) -> str:
    return _norm_name(name)


def _name_similarity(a: str, b: str) -> float:
    """Loose person-name match → confidence 0..1 (0 = no match). Requires the same
    SURNAME, then matches the given name exactly, via a nickname/diminutive
    (Rob↔Robert), as a prefix (Rob↔Robert), or as an initial (R.↔Robert). Keeps
    middle names out of the way by comparing first + last tokens only."""
    ta = [t for t in _norm_name(a).replace(",", " ").split() if t]
    tb = [t for t in _norm_name(b).replace(",", " ").split() if t]
    if len(ta) < 2 or len(tb) < 2:
        return 0.0
    # Surnames must match (allow a surname that's an initial only if the other is 1 char).
    sa, sb = ta[-1].strip("."), tb[-1].strip(".")
    if sa != sb:
        return 0.0
    ga, gb = ta[0].strip("."), tb[0].strip(".")
    if not ga or not gb:
        return 0.0
    if ga == gb:
        return 0.95
    ca, cb = _canon_given(ga), _canon_given(gb)
    if ca == cb:                              # Rob↔Robert (both → "robert")
        return 0.9
    # One is an initial of the other (R ↔ Robert).
    if (len(ga) == 1 and gb.startswith(ga)) or (len(gb) == 1 and ga.startswith(gb)):
        return 0.6
    # One a clear prefix of the other (min 3 chars) — "Rob" ⊂ "Robert".
    if len(ga) >= 3 and cb.startswith(ca):
        return 0.78
    if len(gb) >= 3 and ca.startswith(cb):
        return 0.78
    return 0.0


class _Union:
    """Tiny union-find over identity values so people sharing any identifier
    collapse into one person."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:  # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _user_vault_ids(db: Session, user: User) -> list[str]:
    return [r[0] for r in db.query(Vault.id).filter(Vault.owner_user_id == user.id).all()]


def _self_identifiers(db: Session, user: User) -> set[str]:
    """The user's OWN identifiers, so message direction (inbound/outbound) and
    self-exclusion work. From the account profile PLUS the identities of the
    user's connected source accounts (e.g. a Gmail account's own address), so a
    login email that differs from the mailbox address still resolves 'self'."""
    out: set[str] = set()
    e = contacts.normalize_email(getattr(user, "email", "") or "")
    if e:
        out.add(e)
    for extra in (getattr(user, "notification_emails", None) or []):
        ee = contacts.normalize_email(str(extra))
        if ee:
            out.add(ee)
    p = contacts.normalize_phone(getattr(user, "phone", "") or "")
    if p:
        out.add(p)
    # Connected accounts the user owns identify the user across their own sources.
    try:
        for (uname,) in (db.query(ConnectorAccount.account_username)
                         .filter(ConnectorAccount.tenant_id == user.tenant_id,
                                 ConnectorAccount.owner_user_id == user.id).all()):
            for norm in (contacts.normalize_email(uname or ""),
                         contacts.normalize_phone(uname or "")):
                if norm:
                    out.add(norm)
    except Exception:  # noqa: BLE001 — never let self-id enrichment break a rebuild
        logger.exception("unified_contacts: self-identifier enrichment failed")
    return out


def _message_parties(meta: dict) -> tuple[list[tuple[str, str, str, str]], bool, bool]:
    """Split a message doc's identifiers into (parties, has_from, has_to) where
    each party is (ident_type, normalized, raw, display_name). ``has_from``/
    ``has_to`` note which role fields were present so we can infer direction against
    the user's self ids. Index-only: reads the meta the connector already wrote."""
    parties: list[tuple[str, str, str, str]] = []
    has_from = has_to = False
    for k, v in (meta or {}).items():
        canon = canonical_attr(k)
        if canon not in ("from", "to", "cc", "bcc", "phone"):
            continue
        if canon == "from":
            has_from = True
        elif canon in ("to", "cc", "bcc"):
            has_to = True
        for raw in contacts._iter_values(v):
            parsed = contacts.parse_party(str(raw))
            if parsed:
                parties.append((parsed[0], parsed[1], str(raw), parsed[2], canon))  # type: ignore
    return parties, has_from, has_to  # type: ignore


def _meta_direction(meta: dict) -> str | None:
    """An explicit inbound/outbound hint the collector may have written (e.g. the
    iMessage collector's is_from_me / direction), used before falling back to
    inferring direction from the user's self identifiers."""
    if not meta:
        return None
    fm = meta.get("is_from_me")
    if isinstance(fm, bool):
        return "out" if fm else "in"
    d = str(meta.get("direction") or meta.get("message_direction") or "").strip().lower()
    if d in ("out", "sent", "outgoing", "outbound", "from_me"):
        return "out"
    if d in ("in", "received", "incoming", "inbound", "to_me"):
        return "in"
    return None


# Address local-parts that are roles/systems, never a person's name.
_ROLE_LOCALS = {"info", "support", "admin", "noreply", "no-reply", "donotreply",
                "do-not-reply", "sales", "hello", "contact", "team", "notifications",
                "notification", "mailer-daemon", "postmaster", "help", "billing",
                "accounts", "office", "mail", "news", "newsletter", "updates",
                "service", "alerts", "security", "abuse", "webmaster", "marketing"}


def _name_from_email(email: str) -> str:
    """Derive a display name from a ``firstname.lastname@`` address so an email-only
    person gets a real name (and can name-match a contact card). Returns "" for role
    addresses, Outlook/Exchange internal pseudo-addresses, or single-token locals."""
    local = (email or "").split("@", 1)[0].lower()
    if not local or local in _ROLE_LOCALS:
        return ""
    if local.startswith("ipm.") or "schedule.meeting" in local:
        return ""
    parts = [p for p in re.split(r"[._\-+]+", local) if len(p) >= 2 and not p.isdigit()]
    if len(parts) < 2 or len(parts) > 4 or parts[0] in _ROLE_LOCALS:
        return ""
    return " ".join(p.capitalize() for p in parts)


class _Person:
    """In-memory accumulator for one deduced person during a rebuild."""

    __slots__ = ("key", "names", "identities", "interactions", "sources",
                 "in_count", "out_count", "bytes", "first_at", "last_at", "by_month")

    def __init__(self, key: str) -> None:
        self.key = key
        self.names: dict[str, int] = defaultdict(int)       # name -> weight
        # (kind, value) -> {raw, label, source_type, source_object_id}
        self.identities: dict[tuple[str, str], dict] = {}
        self.interactions = 0
        self.sources: dict[str, int] = defaultdict(int)
        self.in_count = 0
        self.out_count = 0
        self.bytes = 0
        self.first_at: datetime | None = None
        self.last_at: datetime | None = None
        self.by_month: dict[str, int] = defaultdict(int)

    def add_identity(self, kind: str, value: str, *, raw: str = "", label: str = "",
                     source_type: str = "", source_object_id: str = "") -> None:
        cur = self.identities.get((kind, value))
        if cur is None:
            self.identities[(kind, value)] = {
                "raw": raw or value, "label": label, "source_type": source_type,
                "source_object_id": source_object_id}
        else:
            cur["raw"] = cur["raw"] or raw
            cur["label"] = cur["label"] or label
            cur["source_type"] = cur["source_type"] or source_type
            cur["source_object_id"] = cur["source_object_id"] or source_object_id

    def note_interaction(self, *, direction: str, source_type: str,
                         when: datetime | None, size: int) -> None:
        self.interactions += 1
        if source_type:
            self.sources[source_type] += 1
        if direction == "in":
            self.in_count += 1
        elif direction == "out":
            self.out_count += 1
        self.bytes += int(size or 0)
        if when is not None:
            if self.first_at is None or when < self.first_at:
                self.first_at = when
            if self.last_at is None or when > self.last_at:
                self.last_at = when
            self.by_month[when.strftime("%Y-%m")] += 1

    def best_name(self) -> str:
        if self.names:
            return max(self.names.items(), key=lambda kv: (kv[1], len(kv[0])))[0]
        # Derive a human name from a firstname.lastname@ address so an email-only
        # person gets a real name (and can name-match a contact card → merge).
        for (kind, value), d in self.identities.items():
            if kind == "email":
                nm = _name_from_email(value)
                if nm:
                    return nm
        # Fall back to a human-ish label from the strongest identity.
        for (kind, value), d in self.identities.items():
            return d.get("raw") or value
        return "Unknown"


def _closeness_score(p: "_Person", now: datetime) -> float:
    """A balanced closeness score: interaction volume (log-compressed so a heavy
    texter doesn't dwarf everyone), weighted by recency and two-way reciprocity.
    One-way volume (newsletters, you→them only) is discounted so it can't buy an
    inner-circle seat."""
    if not p.last_at or p.interactions <= 0:
        return 0.0
    days = max(0, (now - p.last_at).days)
    recency = math.exp(-days / 120.0)                    # ~1 now, ~0.37 at 120d, ~0.05 at 365d
    reciprocity = 1.0 if (p.in_count > 0 and p.out_count > 0) else 0.55
    return math.log1p(p.interactions) * recency * reciprocity


def _assign_tiers(people: dict[str, "_Person"], now: datetime) -> dict[str, str]:
    """Assign each person a closeness tier by RELATIVE rank (not absolute volume),
    with recency gates, so 'inner circle' stays the few recent, frequent, two-way
    relationships even for a heavy-messaging account. The user can still pin a tier."""
    tiers: dict[str, str] = {}
    scored: list[tuple[str, float, int]] = []
    for p in people.values():
        days = (now - p.last_at).days if p.last_at else 10 ** 9
        if p.interactions <= 0 or days > 365:
            tiers[p.key] = "dormant"
            continue
        scored.append((p.key, _closeness_score(p, now), days))
    scored.sort(key=lambda t: t[1], reverse=True)
    active = len(scored)
    # Small, capped bands scaled to the network size. Inner is tight + recent.
    inner_n = min(12, max(4, round(active * 0.04)))
    close_n = inner_n + min(30, max(8, round(active * 0.12)))
    active_n = close_n + min(120, max(20, round(active * 0.30)))
    for rank, (key, score, days) in enumerate(scored):
        if rank < inner_n and days <= 45 and score > 0:
            tiers[key] = "inner"
        elif rank < close_n and days <= 150:
            tiers[key] = "close"
        elif rank < active_n and days <= 300:
            tiers[key] = "active"
        else:
            tiers[key] = "acquaintance"
    return tiers


def rebuild(db: Session, user: User) -> int:
    """(Re)build the unified contacts for one user by mining their search index.
    Preserves user curation + manual links. Returns the number of contacts."""
    tid = user.tenant_id
    vids = _user_vault_ids(db, user)
    self_ids = _self_identifiers(db, user)
    now = _now()
    uf = _Union()

    # Preserve manual/confirmed links: seed the union with them so a user's
    # explicit "these are the same person" survives and forces grouping.
    existing_manual: dict[str, list[ContactIdentity]] = defaultdict(list)
    for ci in (db.query(ContactIdentity)
               .filter(ContactIdentity.tenant_id == tid,
                       ContactIdentity.owner_user_id == user.id,
                       ContactIdentity.link_method.in_(("manual", "suggested"))).all()):
        if ci.value:
            existing_manual[ci.contact_id].append(ci)
    for cid, cis in existing_manual.items():
        vals = [f"{c.kind}:{c.value}" for c in cis if c.value]
        for v in vals[1:]:
            uf.union(vals[0], v)

    # --- Pass 1: CONTACT records (people) from the index ---------------------
    cq = db.query(SearchDocument.title, SearchDocument.source_type,
                  SearchDocument.object_id, SearchDocument.meta).filter(
        SearchDocument.tenant_id == tid,
        SearchDocument.category.in_(_PERSON_CATEGORIES),
        SearchDocument.is_current.is_(True))
    if vids:
        cq = cq.filter(SearchDocument.vault_id.in_(vids))
    # value-key -> {name, source_type, object_id, idents:[(kind,val,raw,label)]}
    contact_seeds: list[dict] = []
    for title, source_type, object_id, meta in cq.all():
        name = (title or "").strip()
        idents = contacts.contact_identifiers(meta or {})
        if not idents and not name:
            continue
        keys = [f"{t}:{v}" for (t, v) in idents]
        # Union all identifiers on one contact record together.
        for k in keys[1:]:
            uf.union(keys[0], k)
        contact_seeds.append({"name": name, "source_type": source_type or "",
                              "object_id": object_id or "", "idents": idents})

    # --- Pass 2: MESSAGE / SOCIAL / EMAIL docs (interactions) ----------------
    mq = db.query(SearchDocument.source_type, SearchDocument.doc_type,
                  SearchDocument.object_id, SearchDocument.meta,
                  SearchDocument.modified_at, SearchDocument.size_bytes).filter(
        SearchDocument.tenant_id == tid,
        SearchDocument.is_current.is_(True),
        SearchDocument.category.in_(_INTERACTION_CATEGORIES))
    if vids:
        mq = mq.filter(SearchDocument.vault_id.in_(vids))
    # Buffer per-message parsed parties so we can union + attribute after seeding.
    message_rows: list[tuple] = []
    for source_type, doc_type, object_id, meta, modified_at, size in mq.all():
        parties, has_from, has_to = _message_parties(meta or {})
        if not parties:
            continue
        # Counterparties = everyone who isn't the user.
        counter = [(t, v, raw, name, role) for (t, v, raw, name, role) in parties if v not in self_ids]
        if not counter:
            continue
        self_in_from = any(v in self_ids and role == "from" for (_t, v, _r, _n, role) in parties)
        self_in_to = any(v in self_ids and role in ("to", "cc", "bcc")
                         for (_t, v, _r, _n, role) in parties)
        direction = (_meta_direction(meta or {})
                     or ("out" if self_in_from else "in" if self_in_to else "unknown"))
        # Union counterparties on the same message? No — different recipients of a
        # group email aren't the same person. Only union a single counterparty's
        # own identifiers is N/A here (one value each). Keep them separate.
        message_rows.append((source_type or "", counter, direction, modified_at, int(size or 0)))

    # --- Assemble persons by union root --------------------------------------
    people: dict[str, _Person] = {}

    def _person(valkey: str) -> _Person:
        root = uf.find(valkey)
        p = people.get(root)
        if p is None:
            p = _Person(root)
            people[root] = p
        return p

    for seed in contact_seeds:
        idents = seed["idents"]
        if not idents:
            continue
        p = _person(f"{idents[0][0]}:{idents[0][1]}")
        if seed["name"]:
            p.names[seed["name"]] += 3  # a real contact card is a strong name signal
        for (t, v) in idents:
            p.add_identity(t, v, raw=v, source_type=seed["source_type"],
                           source_object_id=seed["object_id"])

    for source_type, counter, direction, modified_at, size in message_rows:
        # One interaction per message per distinct counterparty person.
        seen_people: set[str] = set()
        for (t, v, raw, name, role) in counter:
            p = _person(f"{t}:{v}")
            p.add_identity(t, v, raw=raw, source_type=source_type)
            # The mailbox's display name ("Kashif Javaid <k@x>") is a solid name
            # signal — it's what lets an email-only person match their phone/handle
            # contact by name. Fall back to a bare non-identifier raw otherwise.
            if name:
                p.names[name.strip()] += 2
            elif raw and raw != v and "@" not in raw and not raw.replace("+", "").isdigit():
                p.names[raw.strip()] += 1
            if p.key not in seen_people:
                seen_people.add(p.key)
                p.note_interaction(direction=direction, source_type=source_type,
                                   when=modified_at, size=size)

    # Fold in manual-link contact ids: ensure their identities exist as identities
    # on the right person even if the index no longer carries them.
    for cid, cis in existing_manual.items():
        if not cis:
            continue
        p = _person(f"{cis[0].kind}:{cis[0].value}")
        for ci in cis:
            p.add_identity(ci.kind, ci.value, raw=ci.raw_value, label=ci.label,
                           source_type=ci.source_type, source_object_id=ci.source_object_id)

    n = _persist(db, user, people, existing_manual, now)
    _suggest(db, user, people, now)
    db.commit()
    logger.info("unified contacts rebuilt for %s: %d person(s)", user.id, n)
    return n


def _persist(db: Session, user: User, people: dict[str, _Person],
             existing_manual: dict[str, list[ContactIdentity]], now: datetime) -> int:
    """Upsert deduced people into UnifiedContact/ContactIdentity, matching to
    existing rows by identity overlap so user curation is preserved."""
    tid = user.tenant_id
    # Map every known identity value -> existing contact_id (for matching).
    val_to_contact: dict[str, str] = {}
    existing_contacts: dict[str, UnifiedContact] = {}
    for c in (db.query(UnifiedContact)
              .filter(UnifiedContact.tenant_id == tid,
                      UnifiedContact.owner_user_id == user.id).all()):
        existing_contacts[c.id] = c
    for ci in (db.query(ContactIdentity)
               .filter(ContactIdentity.tenant_id == tid,
                       ContactIdentity.owner_user_id == user.id).all()):
        if ci.value:
            val_to_contact[f"{ci.kind}:{ci.value}"] = ci.contact_id

    # Relative closeness tiering (rank-based, recency-gated) computed over the
    # whole population so 'inner' is the few closest, not everyone over a threshold.
    tier_by_person = _assign_tiers(people, now)

    kept_contact_ids: set[str] = set()
    for person in people.values():
        # Match to an existing contact by any overlapping identity value.
        match_id = None
        for (kind, value) in person.identities:
            cid = val_to_contact.get(f"{kind}:{value}")
            if cid and cid in existing_contacts:
                match_id = cid
                break
        if match_id:
            contact = existing_contacts[match_id]
        else:
            contact = UnifiedContact(tenant_id=tid, owner_user_id=user.id,
                                     created_at=now)
            db.add(contact)
            db.flush()
            existing_contacts[contact.id] = contact
        kept_contact_ids.add(contact.id)

        name = person.best_name()
        contact.display_name = name
        contact.sort_key = _sort_key(name)
        parts = name.split()
        if parts and not contact.given_name:
            contact.given_name = parts[0]
        if len(parts) > 1 and not contact.family_name:
            contact.family_name = parts[-1]
        # Interaction analytics.
        contact.interaction_count = person.interactions
        contact.first_interaction_at = person.first_at
        contact.last_interaction_at = person.last_at
        # Sources = where you INTERACT + where an identifier/name came from (so an
        # address book like Google Contacts/iCloud shows on the people it named).
        id_sources = {d.get("source_type") for d in person.identities.values() if d.get("source_type")}
        contact.source_types = sorted(set(person.sources.keys()) | id_sources)
        top_source = (max(person.sources.items(), key=lambda kv: kv[1])[0]
                      if person.sources else "")
        contact.stats = {
            "by_source": dict(person.sources),
            "by_direction": {"in": person.in_count, "out": person.out_count},
            "by_month": [{"m": m, "count": c} for m, c in sorted(person.by_month.items())],
            "bytes": person.bytes, "top_source": top_source,
            "identity_count": len(person.identities),
        }
        # Primary email/phone for the list.
        contact.primary_email = next((v for (k, v) in person.identities if k == "email"),
                                     contact.primary_email or "")
        contact.primary_phone = next((v for (k, v) in person.identities if k == "phone"),
                                     contact.primary_phone or "")
        # Computed circle unless the user pinned one.
        auto_circle = tier_by_person.get(person.key, "acquaintance")
        contact.circle = contact.pinned_circle or auto_circle
        contact.updated_at = now

        # Identities: replace AUTO ones, preserve manual/suggested (curation).
        manual = {(ci.kind, ci.value): ci
                  for ci in existing_manual.get(contact.id, [])}
        db.query(ContactIdentity).filter(
            ContactIdentity.contact_id == contact.id,
            ContactIdentity.link_method == "auto").delete(synchronize_session=False)
        for (kind, value), d in person.identities.items():
            if (kind, value) in manual:
                continue  # keep the user's manual/confirmed link as-is
            db.add(ContactIdentity(
                tenant_id=tid, owner_user_id=user.id, contact_id=contact.id,
                kind=kind, value=value, raw_value=d.get("raw") or value,
                label=d.get("label") or "", source_type=d.get("source_type") or "",
                source_object_id=d.get("source_object_id") or "",
                link_method="auto", confirmed=True, confidence=1.0,
                last_seen=now, created_at=now, updated_at=now))

    # Drop purely-auto contacts that no longer appear (no curation, no manual link).
    for cid, contact in list(existing_contacts.items()):
        if cid in kept_contact_ids:
            continue
        has_manual = bool(existing_manual.get(cid))
        curated = bool(contact.labels or contact.relationship or contact.notes
                       or (contact.details or {}) or contact.starred
                       or contact.pinned_circle)
        if has_manual or curated:
            continue  # keep user-touched contacts even if the index went quiet
        db.query(ContactIdentity).filter(
            ContactIdentity.contact_id == cid).delete(synchronize_session=False)
        db.delete(contact)
    return len(kept_contact_ids)


def _suggest(db: Session, user: User, people: dict[str, _Person], now: datetime) -> None:
    """Raise conservative LINK + MERGE suggestions for people who look like the
    same person but share no identifier — using LOOSE NAME MATCHING (Rob↔Robert,
    initials, prefixes). A loose message-only contact that name-matches a real
    contact card becomes a LINK suggestion (attach its identifier); two substantive
    contacts become a MERGE suggestion. Always surfaced for the user to accept —
    never merged silently."""
    tid = user.tenant_id
    db.query(ContactSuggestion).filter(
        ContactSuggestion.tenant_id == tid,
        ContactSuggestion.owner_user_id == user.id,
        ContactSuggestion.status == "pending").delete(synchronize_session=False)
    dismissed = {s.fingerprint for s in db.query(ContactSuggestion.fingerprint).filter(
        ContactSuggestion.tenant_id == tid,
        ContactSuggestion.owner_user_id == user.id,
        ContactSuggestion.status == "dismissed").all()}

    rows = (db.query(UnifiedContact)
            .filter(UnifiedContact.tenant_id == tid,
                    UnifiedContact.owner_user_id == user.id,
                    UnifiedContact.hidden.is_(False)).all())
    multi = [c for c in rows if len(_norm_name(c.display_name).split()) >= 2]
    # Identity count per contact (to decide link-vs-merge + which side is "loose").
    idn_count: dict[str, int] = defaultdict(int)
    one_identity: dict[str, tuple] = {}  # contact_id -> (kind, value, raw, source)
    for ci in (db.query(ContactIdentity)
               .filter(ContactIdentity.tenant_id == tid,
                       ContactIdentity.owner_user_id == user.id).all()):
        idn_count[ci.contact_id] += 1
        one_identity[ci.contact_id] = (ci.kind, ci.value, ci.raw_value, ci.source_type)

    def _loose(c: UnifiedContact) -> bool:
        return (idn_count.get(c.id, 0) <= 1 and not c.labels and not c.relationship
                and not c.notes and not (c.details or {}) and not c.starred
                and not c.pinned_circle)

    def _curation_score(c: UnifiedContact) -> tuple:
        # Prefer the richer record as the primary / link target.
        return (0 if _loose(c) else 1, idn_count.get(c.id, 0),
                int(c.interaction_count or 0), len(c.source_types or []))

    # Bucket by surname so we only compare plausible pairs (O(n²) within a surname).
    by_surname: dict[str, list[UnifiedContact]] = defaultdict(list)
    for c in multi:
        by_surname[_norm_name(c.display_name).split()[-1].strip(".")].append(c)

    emitted: set[str] = set()
    for surname, group in by_surname.items():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                sim = _name_similarity(a.display_name, b.display_name)
                if sim <= 0:
                    continue
                # Primary = the richer record; other = the one we'd fold in.
                primary, other = (a, b) if _curation_score(a) >= _curation_score(b) else (b, a)
                pair = f"{min(primary.id, other.id)}:{max(primary.id, other.id)}"
                if pair in emitted:
                    continue
                emitted.add(pair)
                # LINK when the "other" is a loose single-identifier contact and the
                # primary is a real record — attach the identifier rather than merge.
                oid = one_identity.get(other.id)
                if _loose(other) and not _loose(primary) and oid:
                    fp = hashlib.sha256(f"link:{primary.id}:{oid[0]}:{oid[1]}".encode()).hexdigest()[:24]
                    if fp in dismissed:
                        continue
                    db.add(ContactSuggestion(
                        tenant_id=tid, owner_user_id=user.id, kind="link_identity",
                        contact_id=primary.id, identity_kind=oid[0],
                        identity_value=oid[1], identity_raw=oid[2] or oid[1],
                        identity_source=oid[3] or "",
                        reason=f"“{other.display_name}” looks like {primary.display_name}",
                        confidence=round(sim, 2), status="pending", fingerprint=fp,
                        created_at=now, updated_at=now))
                else:
                    fp = hashlib.sha256(f"merge:{pair}".encode()).hexdigest()[:24]
                    if fp in dismissed:
                        continue
                    db.add(ContactSuggestion(
                        tenant_id=tid, owner_user_id=user.id, kind="merge",
                        contact_id=primary.id, merge_contact_id=other.id,
                        reason=f"“{other.display_name}” may be the same person as {primary.display_name}",
                        confidence=round(sim, 2), status="pending", fingerprint=fp,
                        created_at=now, updated_at=now))


# --------------------------------------------------------------------------- #
# Read helpers — drill-down into a contact's exchanges by MINING THE INDEX.    #
# --------------------------------------------------------------------------- #
def identity_values(db: Session, tenant_id: str, user_id: str,
                    contact_id: str) -> dict[str, set[str]]:
    """All normalized identifier values for a contact, grouped by kind."""
    out: dict[str, set[str]] = defaultdict(set)
    for ci in (db.query(ContactIdentity)
               .filter(ContactIdentity.tenant_id == tenant_id,
                       ContactIdentity.owner_user_id == user_id,
                       ContactIdentity.contact_id == contact_id).all()):
        if ci.value:
            out[ci.kind].add(ci.value)
    return out


def sources_breakdown(db: Session, user: User) -> list[dict]:
    """Per-source contribution to the contact graph: how many contacts + interactions
    each source produced, how many identifiers it linked, and how many of its docs
    are in the index — so a source with indexed docs but few contacts is a visible gap."""
    from sqlalchemy import func
    tid = user.tenant_id
    vids = _user_vault_ids(db, user)
    agg: dict[str, dict] = {}

    def row(st: str) -> dict:
        return agg.setdefault(st or "unknown", {
            "source_type": st or "unknown", "contacts": 0, "interactions": 0,
            "identities": 0, "indexed": 0})

    for st, n in (db.query(ContactIdentity.source_type, func.count())
                  .filter(ContactIdentity.tenant_id == tid,
                          ContactIdentity.owner_user_id == user.id)
                  .group_by(ContactIdentity.source_type).all()):
        if st:
            row(st)["identities"] = int(n or 0)
    for src_types, stats in (db.query(UnifiedContact.source_types, UnifiedContact.stats)
                             .filter(UnifiedContact.tenant_id == tid,
                                     UnifiedContact.owner_user_id == user.id,
                                     UnifiedContact.hidden.is_(False)).all()):
        for st in (src_types or []):
            row(st)["contacts"] += 1
        for st, cnt in ((stats or {}).get("by_source") or {}).items():
            row(st)["interactions"] += int(cnt or 0)
    iq = db.query(SearchDocument.source_type, func.count()).filter(
        SearchDocument.tenant_id == tid,
        SearchDocument.is_current.is_(True),
        SearchDocument.category.in_(_INTERACTION_CATEGORIES + ("contact",)))
    if vids:
        iq = iq.filter(SearchDocument.vault_id.in_(vids))
    for st, n in iq.group_by(SearchDocument.source_type).all():
        if st:
            row(st)["indexed"] = int(n or 0)
    return sorted(agg.values(),
                  key=lambda r: (-r["interactions"], -r["contacts"], r["source_type"]))


def exchanges(db: Session, user: User, contact: UnifiedContact, *,
              limit: int = 50, offset: int = 0, scan_cap: int = 20000) -> dict:
    """The communication exchanges with a contact, mined from the search index
    (message/social/email docs whose from/to/cc/bcc matches ANY of the contact's
    identifiers). Returns lightweight rows the UI links straight into Unified
    Search (source_type + object_id). Never re-extracts — reads the index only.

    Narrows to docs whose search_blob contains one of the contact's identifiers so
    we scan THE PERSON'S messages across the whole index, not just the most-recent
    slice (a heavy account has far more than scan_cap total messages, so the old
    recent-only scan missed most of a contact's history)."""
    from sqlalchemy import or_
    tid = user.tenant_id
    vids = _user_vault_ids(db, user)
    self_ids = _self_identifiers(db, user)
    vals = identity_values(db, tid, user.id, contact.id)
    want = set().union(*vals.values()) if vals else set()
    if not want:
        return {"total": 0, "items": []}
    q = db.query(SearchDocument.source_type, SearchDocument.doc_type,
                 SearchDocument.object_id, SearchDocument.title,
                 SearchDocument.preview, SearchDocument.meta,
                 SearchDocument.modified_at, SearchDocument.size_bytes).filter(
        SearchDocument.tenant_id == tid,
        SearchDocument.is_current.is_(True),
        SearchDocument.category.in_(_INTERACTION_CATEGORIES))
    if vids:
        q = q.filter(SearchDocument.vault_id.in_(vids))
    # A normalized phone key (e.g. 4088592476) is a substring of the stored form
    # (+14088592476); an email matches verbatim. The Python verify below keeps it
    # precise, so a loose substring match only widens the candidate set.
    needles = [n for n in want if n and len(n) >= 4]
    if needles:
        q = q.filter(or_(*[SearchDocument.search_blob.ilike(f"%{n}%") for n in needles]))
    rows = q.order_by(SearchDocument.modified_at.desc().nullslast()).limit(scan_cap).all()
    hits: list[dict] = []
    for source_type, doc_type, object_id, title, preview, meta, modified_at, size in rows:
        parties, has_from, has_to = _message_parties(meta or {})
        if not any(v in want for (_t, v, _r, _n, _role) in parties):
            continue
        self_in_from = any(v in self_ids and role == "from" for (_t, v, _r, _n, role) in parties)
        self_in_to = any(v in self_ids and role in ("to", "cc", "bcc")
                         for (_t, v, _r, _n, role) in parties)
        hits.append({
            "source_type": source_type or "", "doc_type": doc_type or "",
            "object_id": object_id or "", "title": title or "",
            "preview": preview or "",
            "direction": (_meta_direction(meta or {})
                          or ("out" if self_in_from else "in" if self_in_to else "unknown")),
            "modified_at": modified_at.isoformat() if modified_at else None,
            "size_bytes": int(size or 0),
        })
    total = len(hits)
    return {"total": total, "items": hits[offset:offset + limit],
            "scan_capped": len(rows) >= scan_cap}


def merge(db: Session, user: User, primary_id: str, other_id: str) -> None:
    """Merge ``other`` into ``primary``: repoint its identities + union curation,
    then delete it. Identities become manual so a rebuild never re-splits them."""
    tid = user.tenant_id
    primary = db.get(UnifiedContact, primary_id)
    other = db.get(UnifiedContact, other_id)
    if not primary or not other or primary.owner_user_id != user.id \
            or other.owner_user_id != user.id:
        return
    for ci in (db.query(ContactIdentity)
               .filter(ContactIdentity.contact_id == other_id).all()):
        ci.contact_id = primary_id
        ci.link_method = "manual"  # the merge is an explicit human decision
        ci.confirmed = True
    # Union light curation.
    primary.labels = sorted(set((primary.labels or []) + (other.labels or [])))
    primary.relationship = primary.relationship or other.relationship
    primary.notes = "\n".join(x for x in (primary.notes, other.notes) if x)
    if (other.details or {}) and not (primary.details or {}):
        primary.details = other.details
    primary.starred = primary.starred or other.starred
    primary.updated_at = _now()
    db.delete(other)
    db.commit()
