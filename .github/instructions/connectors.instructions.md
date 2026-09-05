---
applyTo: "cloud/app/connectors/**/*.py"
description: "Adding or changing data-source connectors."
---

# Source connectors

Mirror an existing connector. Two files:

- **`registry.py`** — `@register_connector class XConnector(Connector)`: set `connector_type`,
  `display_name`, `capabilities()` (`streaming=True` for content-heavy sources to avoid OOM; `delta=True`
  for auto-scheduled sources; `searchable_fields`/`facet_fields`; `filter_categories=[{id,label}]`),
  `oauth_spec()` (`auth_type` oauth2|api-token|app-password|agent), and `fetch_objects()`/`fetch()`.
- **`live.py`** — `fetch_X(token, ...)`: OPTIONAL SDK import in try/except (return if missing); yield
  `SourceObject`. Cap content with `_capped`. Respect `options["includeCategories"]`.

Rules:
- **Streaming + OOM:** base `Connector.fetch` does `list(fetch_objects)` → a whole library in RAM → OOM kills
  uvicorn. Content/media sources MUST set `capabilities().streaming=True` and yield lazily; `sync_worker`
  ingests in bounded batches.
- **Stable dedup:** ingest versions by `content_hash`. If the content JSON contains volatile fields (rotating
  CDN URLs, counts), set an explicit `SourceObject.content_hash` over durable fields, else it re-ingests every run.
- **Object date:** set `SourceObject.modified_at` from the item's real date (email internalDate, file
  lastModified, etc.); unset defaults to ingest time and looks wrong in search.
- **Connectors must RAISE real errors** (esp. 401/403). Catching and returning empty records a false 0-object
  success and never flags needs-reauth.
- **Attachments** (email/message sources) are emitted as their OWN file objects (kind image/pdf/file…),
  linked to the parent via `meta.message_object_id`, inheriting from/to/subject. They categorize as files but
  stay tied to the message.
- OAuth2: add a `ProviderSpec` in `oauth.py` + `client_id/secret` in `config.py`. Token sources: add to
  `oauth.TOKEN_TYPES`. Register in `api/connectors.py` `_SOURCE_FAMILY`/`_SOURCE_TYPE`.
- Brand icon: add the type to `scripts/sync_source_icons.py` SOURCE_ICONS + the registries in
  `web/src/components/sourceIcons.ts` AND `cloud/app/source_icons.py`, then run the sync script.
