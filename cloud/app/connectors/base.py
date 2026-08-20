"""
Sync-worker connector (puller) framework.

A *connector* pulls data from a source service and normalizes it into
``SourceObject`` records that the sync worker encrypts, snapshots, and indexes
for unified search. The framework is designed to be extensible and customizable:

- Add a new service by subclassing ``Connector`` and decorating it with
  ``@register_connector`` — no changes to callers or the sync worker.
- Each connector declares ``ConnectorCapabilities`` including which metadata
  fields are promoted into the searchable index (``searchable_fields``) and which
  are exposed as filter facets (``facet_fields``).
- Incremental pulls are supported via an opaque ``cursor`` (``FetchResult``);
  connectors that only do full syncs simply ignore it.

For the prototype, connectors emit realistic simulated datasets so the full
ingest -> encrypt -> snapshot -> index -> search -> restore pipeline runs end to
end. The OAuth metadata and interface are production-shaped: replacing a
connector's ``fetch`` body with real API calls (Gmail API, Microsoft Graph,
Dropbox, 1Password Connect, etc.) requires no other changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Type

from ..taxonomy import category_for_kind


@dataclass
class SourceObject:
    """One normalized item pulled from a source service."""

    object_id: str
    doc_type: str  # canonical kind (email, pdf, login, person, event, ...)
    title: str
    content: bytes
    preview: str  # policy-permitted derived preview text
    meta: Dict[str, object] = field(default_factory=dict)
    size_bytes: int = 0
    modified_at: Optional[datetime] = None
    labels: List[str] = field(default_factory=list)  # tags / folders for faceting
    category: str = ""  # canonical top-level category; derived from kind if unset
    # Stable content hash of the *plaintext* — set by client-encrypting agents so
    # versioning/dedup works on the original bytes, not the (nonce-randomised)
    # ciphertext. Falls back to hashing ``content`` when unset.
    content_hash: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.size_bytes:
            self.size_bytes = len(self.content)
        if self.modified_at is None:
            self.modified_at = datetime.now(timezone.utc)
        if not self.category:
            self.category = category_for_kind(self.doc_type)

    @property
    def kind(self) -> str:
        return self.doc_type

    def searchable_text(self, fields: List[str]) -> str:
        """Flatten title, preview, labels, and the connector's declared
        searchable metadata fields into one indexable text blob. ``["*"]``
        indexes every metadata value."""
        parts: List[str] = [self.title, self.preview, *self.labels]
        keys = list(self.meta.keys()) if "*" in fields else fields
        for key in keys:
            value = self.meta.get(key)
            if value is None:
                continue
            if isinstance(value, (list, tuple, set)):
                parts.extend(str(v) for v in value)
            elif isinstance(value, dict):
                parts.extend(f"{k} {v}" for k, v in value.items())
            else:
                parts.append(str(value))
        return " ".join(p for p in parts if p).strip()


@dataclass
class ConnectorCapabilities:
    """Declarative, per-connector capabilities and search configuration."""

    incremental: bool = False
    supports_pagination: bool = False
    # True when the connector returns a delta cursor (only changed items on later
    # syncs) — the background scheduler only auto-runs delta-capable sources.
    delta: bool = False
    rate_limit_per_min: int = 600
    # Source is collected by a local desktop agent (native CLI), not a cloud pull.
    requires_agent: bool = False
    # Source has a large history worth crawling in resumable chunks and supports a
    # "back up from this date" window (config["sinceDate"]). Big-history pulls run
    # as a looping background job (chunk → persist cursor → continue).
    historical: bool = False
    # Metadata keys promoted into the searchable index blob.
    searchable_fields: List[str] = field(default_factory=list)
    # Metadata keys exposed as filter facets in the UI.
    facet_fields: List[str] = field(default_factory=list)
    # Content categories the operator can include/exclude in the Data Map
    # (e.g. iCloud: photos/files/contacts). Each = {"id","label"}. The chosen ids
    # arrive as config["includeCategories"]; empty selection = include everything.
    filter_categories: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class OAuthSpec:
    connector_type: str
    display_name: str
    auth_type: str  # oauth2 | api-token | app-password | custom
    authorize_url: str
    token_url: str
    scopes: List[str]
    icon: str
    color: str
    doc_types: List[str]


@dataclass
class FetchResult:
    """Result of one pull. ``cursor`` enables incremental sync on the next run."""

    objects: List[SourceObject]
    cursor: Optional[str] = None
    has_more: bool = False


class Connector:
    connector_type: str = "base"
    display_name: str = "Base"

    def oauth_spec(self) -> OAuthSpec:  # pragma: no cover - overridden
        raise NotImplementedError

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities()

    def validate(self, credentials: Optional[dict] = None) -> bool:
        """Verify credentials before enabling scheduled pulls."""
        return True

    def fetch(self, account_label: str, cursor: Optional[str] = None,
              config: Optional[dict] = None) -> FetchResult:
        """Pull objects. Override for real services. The default adapts a simple
        ``fetch_objects`` generator into a full-sync ``FetchResult``."""
        objects = list(self.fetch_objects(account_label, config=config))
        return FetchResult(objects=objects, cursor=None, has_more=False)

    def fetch_objects(self, account_label: str, since: Optional[datetime] = None,
                      config: Optional[dict] = None) -> Iterable[SourceObject]:
        raise NotImplementedError


# --- Extensible registry ----------------------------------------------------

CONNECTOR_REGISTRY: Dict[str, Connector] = {}


def register_connector(cls: Type[Connector]) -> Type[Connector]:
    """Class decorator that self-registers a connector by its ``connector_type``."""
    instance = cls()
    CONNECTOR_REGISTRY[instance.connector_type] = instance
    return cls


def get_connector(connector_type: str) -> Optional[Connector]:
    return CONNECTOR_REGISTRY.get(connector_type)


def all_connectors() -> List[Connector]:
    return list(CONNECTOR_REGISTRY.values())
