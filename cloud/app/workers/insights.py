"""Daily digital-footprint insight generation.

Mines each user's search index (their own vaults) into a compact, ready-to-render
payload: a footprint-over-time timeline and a flexible set of "insight cards".
The Insights page reads the stored payload, so it never mines the index at request
time. Cards are emitted only when the data supports them, so different users see
different (and a different number of) cards.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..connectors import get_connector
from ..db import WorkerSessionLocal as SessionLocal
from ..models import SearchDocument, UserInsights, User, Vault
from .. import features

logger = logging.getLogger("cv.insights")

# Emit cards / a timeline only once the footprint is substantial enough to be
# meaningful; below this a user just sees a "keep protecting" empty state.
_MIN_OBJECTS = 8

_CREDENTIAL_TYPES = {"login", "password", "secret", "api_key", "credit_card"}
_MESSAGE_TYPES = {"email", "message", "post", "comment"}
_MEMORY_TYPES = {"image", "photo", "video"}

# Fallback colors for sources without a connector-declared brand color.
_FALLBACK_COLOR = "#7a5cff"
_ICON_MAP = {"folder": "file", "gear": "database"}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _source_meta(source_type: str) -> dict:
    conn = get_connector(source_type)
    if not conn:
        return {"label": source_type or "Unknown", "icon": "database", "color": _FALLBACK_COLOR}
    spec = conn.oauth_spec()
    return {"label": spec.display_name,
            "icon": _ICON_MAP.get(spec.icon, spec.icon),
            "color": spec.color or _FALLBACK_COLOR}


def _fmt_bytes(n: int) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class _Obj:
    __slots__ = ("source_type", "doc_type", "category", "size", "when", "title")

    def __init__(self, source_type, doc_type, category, size, when, title):
        self.source_type = source_type
        self.doc_type = doc_type
        self.category = category
        self.size = size
        self.when = when
        self.title = title


def _time_reliable(o: "_Obj") -> bool:
    """Calendar events carry arbitrary dates (far past/future), so their
    timestamps must never drive footprint timelines or "oldest item" analysis."""
    return o.doc_type != "event" and o.category != "calendar"


def _dated(objs: list["_Obj"]) -> list["_Obj"]:
    """Objects whose timestamp can be trusted for time-based analysis."""
    return [o for o in objs if o.when is not None and _time_reliable(o)]


def _collect_objects(db: Session, vault_ids: list[str], tenant_id: str) -> list[_Obj]:
    """Deduplicated logical objects (latest row per source_type+object_id)."""
    if not vault_ids:
        return []
    rows = (db.query(SearchDocument)
            .filter(SearchDocument.tenant_id == tenant_id,
                    SearchDocument.vault_id.in_(vault_ids))
            .order_by(SearchDocument.created_at.desc()).all())
    seen: set[tuple] = set()
    out: list[_Obj] = []
    for r in rows:
        key = (r.source_type, r.object_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(_Obj(r.source_type or "", (r.doc_type or "").lower(),
                        (r.category or "").lower(), int(r.size_bytes or 0),
                        r.modified_at, r.title or ""))
    return out


def _build_timeline(objs: list[_Obj]) -> dict:
    dated = _dated(objs)
    if not dated:
        return {"granularity": "year", "points": [], "series": [],
                "bytes": [], "cumulative": [], "total_objects": len(objs),
                "total_bytes": sum(o.size for o in objs)}
    dated.sort(key=lambda o: o.when)
    first, last = dated[0].when, dated[-1].when
    months = (last.year - first.year) * 12 + (last.month - first.month) + 1
    granularity = "month" if months <= 36 else "year"

    def bucket_key(dt: datetime) -> str:
        return f"{dt.year:04d}-{dt.month:02d}" if granularity == "month" else f"{dt.year:04d}"

    # Ordered, gap-filled period labels so the chart has a continuous x-axis.
    points: list[str] = []
    if granularity == "month":
        y, m = first.year, first.month
        while (y, m) <= (last.year, last.month):
            points.append(f"{y:04d}-{m:02d}")
            m += 1
            if m > 12:
                m, y = 1, y + 1
    else:
        for y in range(first.year, last.year + 1):
            points.append(f"{y:04d}")
    index = {p: i for i, p in enumerate(points)}
    n = len(points)

    # Objects added per source per period + bytes per period.
    per_source: dict[str, list[int]] = defaultdict(lambda: [0] * n)
    byte_series = [0] * n
    totals_by_source: dict[str, int] = defaultdict(int)
    for o in dated:
        i = index[bucket_key(o.when)]
        per_source[o.source_type][i] += 1
        byte_series[i] += o.size
        totals_by_source[o.source_type] += 1

    # Keep the six biggest sources as their own bands; fold the rest into "Other".
    ranked = sorted(totals_by_source.items(), key=lambda kv: -kv[1])
    top = [s for s, _ in ranked[:6]]
    series: list[dict] = []
    for s in top:
        m = _source_meta(s)
        series.append({"key": s, "label": m["label"], "icon": m["icon"],
                       "color": m["color"], "values": per_source[s]})
    rest = [s for s in totals_by_source if s not in top]
    if rest:
        merged = [0] * n
        for s in rest:
            for i, v in enumerate(per_source[s]):
                merged[i] += v
        series.append({"key": "__other__", "label": "Other sources",
                       "icon": "database", "color": "#8a93a6", "values": merged})

    cumulative: list[int] = []
    running = 0
    for i in range(n):
        running += sum(sv["values"][i] for sv in series)
        cumulative.append(running)

    return {"granularity": granularity, "points": points, "series": series,
            "bytes": byte_series, "cumulative": cumulative,
            "total_objects": len(objs), "total_bytes": sum(o.size for o in objs),
            "span_start": first.isoformat(), "span_end": last.isoformat()}


# --------------------------------------------------------------------------- #
# Insight cards — each returns a card dict when the data qualifies, else None. #
# --------------------------------------------------------------------------- #
def _card_longevity(objs: list[_Obj], stats: dict) -> dict | None:
    dated = _dated(objs)
    if len(dated) < _MIN_OBJECTS:
        return None
    dated.sort(key=lambda o: o.when)
    oldest = dated[0]
    span_days = (dated[-1].when - oldest.when).days
    years = span_days / 365.25
    if years < 1.5:
        return None
    now = _now()
    over_5y = sum(1 for o in dated if (now - o.when).days > 365.25 * 5)
    detail = [{"label": "Reaches back to", "value": oldest.when.strftime("%B %Y")},
              {"label": "Footprint span", "value": f"{years:.0f} years"}]
    if over_5y:
        detail.append({"label": "Items over 5 years old", "value": f"{over_5y:,}"})
    if oldest.title:
        detail.append({"label": "Oldest item", "value": oldest.title[:60]})
    return {
        "id": "longevity",
        "icon": "clock",
        "tone": "info",
        "title": "A digital memory spanning years",
        "headline": f"{years:.0f} years of history preserved",
        "body": ("Your protected footprint stretches back to "
                 f"{oldest.when.strftime('%B %Y')} — {years:.0f} years of email, files and "
                 "memories. The oldest records are usually the most irreplaceable and the "
                 "least likely to still exist anywhere else. Arkive keeps them recoverable "
                 "even if the original account is lost."),
        "detail": detail,
        "action": {"label": "Explore your oldest items", "to": "/search?sort=date&dir=asc"},
    }


def _card_concentration(objs: list[_Obj], stats: dict) -> dict | None:
    by_source: dict[str, int] = defaultdict(int)
    for o in objs:
        by_source[o.source_type] += 1
    total = sum(by_source.values())
    if total < _MIN_OBJECTS or len(by_source) < 2:
        return None
    top_src, top_n = max(by_source.items(), key=lambda kv: kv[1])
    share = top_n / total
    if share < 0.5:
        return None
    m = _source_meta(top_src)
    pct = round(share * 100)
    return {
        "id": "concentration",
        "icon": "shield",
        "tone": "warn",
        "title": "Most of your digital life is in one place",
        "headline": f"{pct}% of everything lives in {m['label']}",
        "body": (f"{pct}% of the objects you've protected — {top_n:,} of {total:,} — come from a "
                 f"single source, {m['label']}. Losing access to that one account (a lockout, "
                 "hack or provider shutdown) would take most of your digital life with it. "
                 "Because Arkive holds an independent, encrypted copy, you stay in control "
                 "even if that account disappears."),
        "detail": [{"label": "Largest source", "value": m["label"]},
                   {"label": "Its share", "value": f"{pct}%"},
                   {"label": "Distinct sources", "value": str(len(by_source))}],
        "action": {"label": "Review your sources", "to": "/connectors"},
    }


def _card_credentials(objs: list[_Obj], stats: dict) -> dict | None:
    creds = [o for o in objs if o.doc_type in _CREDENTIAL_TYPES or o.category == "credential"]
    if len(creds) < 5:
        return None
    return {
        "id": "credentials",
        "icon": "key",
        "tone": "ok",
        "title": "Your keys to everything are safe",
        "headline": f"{len(creds):,} credentials secured",
        "body": (f"You've protected {len(creds):,} logins, passwords and secrets — the keys that "
                 "unlock every other account you own. These are the crown jewels of your digital "
                 "identity: if your password manager were ever lost or locked, this recoverable "
                 "copy is what gets you back in. They stay end-to-end encrypted and only you can "
                 "open them."),
        "detail": [{"label": "Credentials protected", "value": f"{len(creds):,}"},
                   {"label": "Encryption", "value": "End-to-end (only you)"}],
        "action": {"label": "Review your credentials", "to": "/search?type=credential"},
    }


def _card_memories(objs: list[_Obj], stats: dict) -> dict | None:
    mem = [o for o in objs if o.doc_type in _MEMORY_TYPES or o.category in ("image", "media")]
    if len(mem) < 20:
        return None
    mem_bytes = sum(o.size for o in mem)
    return {
        "id": "memories",
        "icon": "image",
        "tone": "info",
        "title": "Your memories, preserved for good",
        "headline": f"{len(mem):,} photos & videos kept safe",
        "body": (f"Arkive is safeguarding {len(mem):,} photos and videos — {_fmt_bytes(mem_bytes)} of "
                 "memories that can never be recreated. Phones are lost and cloud accounts get "
                 "closed, but this independent copy means the moments that matter outlive any "
                 "single device or provider."),
        "detail": [{"label": "Photos & videos", "value": f"{len(mem):,}"},
                   {"label": "Total size", "value": _fmt_bytes(mem_bytes)}],
        "action": {"label": "Browse your memories", "to": "/search?type=image"},
    }


def _card_communications(objs: list[_Obj], stats: dict) -> dict | None:
    msgs = [o for o in objs if o.doc_type in _MESSAGE_TYPES or o.category == "message"]
    if len(msgs) < 50:
        return None
    return {
        "id": "communications",
        "icon": "mail",
        "tone": "info",
        "title": "A record of your conversations",
        "headline": f"{len(msgs):,} messages archived",
        "body": (f"You've preserved {len(msgs):,} emails, messages and posts — a searchable record "
                 "of the conversations, agreements and relationships that shape your life. "
                 "Providers routinely delete old mail and shut inactive accounts; your Arkive "
                 "copy keeps this history findable and yours."),
        "detail": [{"label": "Messages archived", "value": f"{len(msgs):,}"}],
        "action": {"label": "Search your messages", "to": "/search?type=message"},
    }


# Ordered candidate generators; qualifying cards are emitted (capped).
_CARD_BUILDERS = [
    _card_longevity,
    _card_concentration,
    _card_credentials,
    _card_memories,
    _card_communications,
]
_MAX_CARDS = 5


def build_payload(db: Session, user: User) -> dict:
    """Compute the full insights payload for one user (no DB writes)."""
    vault_ids = [v.id for v in db.query(Vault).filter(Vault.owner_user_id == user.id).all()]
    objs = _collect_objects(db, vault_ids, user.tenant_id)
    total_bytes = sum(o.size for o in objs)
    by_source: dict[str, int] = defaultdict(int)
    by_category: dict[str, int] = defaultdict(int)
    for o in objs:
        by_source[o.source_type] += 1
        if o.category:
            by_category[o.category] += 1
    stats = {
        "object_count": len(objs),
        "total_bytes": total_bytes,
        "source_count": len(by_source),
        "category_count": len(by_category),
    }
    if len(objs) < _MIN_OBJECTS:
        return {"status": "insufficient_data", "stats": stats,
                "timeline": _build_timeline(objs), "cards": []}
    cards = []
    for builder in _CARD_BUILDERS:
        try:
            card = builder(objs, stats)
        except Exception:  # noqa: BLE001 — one bad card never sinks the report
            logger.exception("insight card %s failed for user %s", builder.__name__, user.id)
            card = None
        if card:
            cards.append(card)
        if len(cards) >= _MAX_CARDS:
            break
    return {"status": "ready", "stats": stats,
            "timeline": _build_timeline(objs), "cards": cards}


def generate_for_user(db: Session, user: User) -> UserInsights:
    """Compute and upsert a user's insights row."""
    payload = build_payload(db, user)
    row = db.query(UserInsights).filter(UserInsights.user_id == user.id).one_or_none()
    if row is None:
        row = UserInsights(tenant_id=user.tenant_id, user_id=user.id)
        db.add(row)
    row.tenant_id = user.tenant_id
    row.status = payload["status"]
    row.timeline = payload["timeline"]
    row.cards = payload["cards"]
    row.stats = payload["stats"]
    row.generated_at = _now()
    db.commit()
    return row


def mark_pending(db: Session, user: User) -> UserInsights:
    """Flag a user's insights as awaiting (re)generation. Used on the control
    plane for node-hosted tenants: the assigned node picks this up on its next
    replication pull, generates the report locally, and pushes the result back."""
    row = db.query(UserInsights).filter(UserInsights.user_id == user.id).one_or_none()
    if row is None:
        row = UserInsights(tenant_id=user.tenant_id, user_id=user.id, stats={},
                           timeline={}, cards=[])
        db.add(row)
    row.tenant_id = user.tenant_id
    row.status = "pending"
    db.commit()
    return row


def _is_control_plane() -> bool:
    from ..config import get_settings
    return (get_settings().node_role or "control-plane") == "control-plane"


def generate_all() -> int:
    """Refresh insights for every active user who has the feature enabled.
    Returns the number of users processed."""
    count = 0
    control_plane = _is_control_plane()
    with SessionLocal() as db:
        from ..models import Tenant
        users = db.query(User).filter(User.status == "active").all()
        tenants = {t.id: t for t in db.query(Tenant).all()}
        for u in users:
            try:
                tenant = tenants.get(u.tenant_id)
                if not features.resolve(u, tenant, "insights_enabled"):
                    continue
                # In federation the assigned node owns its tenants' index and
                # computes their insights (then pushes them up); the control
                # plane must not also generate them from its replicated copy.
                if control_plane and tenant and tenant.node_id:
                    continue
                generate_for_user(db, u)
                count += 1
            except Exception:  # noqa: BLE001 — never let one user break the batch
                logger.exception("insight generation failed for user %s", u.id)
                db.rollback()
    logger.info("insights: refreshed %d user(s)", count)
    return count
