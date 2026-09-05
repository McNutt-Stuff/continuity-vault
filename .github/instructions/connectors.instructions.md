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

## Logging & error detail — STANDARD (so a source can be triaged from the admin Platform Logs)

Every connector/source failure MUST be reproducible from the control plane without shell access. Follow this
standard — it is not optional; a bare `except: return []` or a terse `logger.warning("failed")` is a bug.

- **Log via a `cv.*` logger** (`logging.getLogger("cv.connectors.<type>")` / the shared `cv.sync`) so the
  in-process sink captures it into the unified `log_entries` store. Never `print`. Never swallow.
- **RAISE, don't swallow.** Let real failures propagate so `sync_worker._record_sync_error` records them on the
  account (`last_error`/`last_error_at`/`fail_count`, needs-reauth on auth errors) AND `audit.record`
  dual-writes a tenant-attributed LogEntry that appears in Platform Logs + feeds `source_problem` notifications.
- **Granular detail is REQUIRED.** The recorded error must answer *why*. Include, when available:
  - the **HTTP status code** and the provider's **error code/reason** (e.g. `invalid_grant`, `quotaExceeded`,
    `rate_limit_exceeded`, `insufficientPermissions`);
  - a **bounded snippet of the response body** (cap ~200 chars) — error responses carry codes/descriptions;
  - the **request context** that identifies the call: endpoint/operation, source type, account label, and the
    relevant identifier (folder, cursor/page token, object id) — NOT the full payload.
  - `sync_worker._normalize_sync_error` already appends `HTTP <code> (<body snippet>)` from `exc.response`; if
    you raise `httpx.HTTPStatusError` (or any exc with `.response`) that detail is captured automatically.
    Preserve `.response` — use `raise ... from exc`; don't restring the error into a bare message.
- **Structured detail on `audit.record`.** When you record a source event yourself, pass a `detail={...}` with
  `type`, `account`, `error`, `reason`/`code`, and any operation context. `audit.record` folds
  `account/type/error/message/reason` into the Platform-Logs message and carries the full `detail` in `meta`
  (drill-down), so keep those keys meaningful.
- **Levels:** `warning` for a recoverable/needs-reauth source problem, `error` for an unexpected failure,
  `info` for lifecycle (linked/reauthorized/first-backup), `debug` for per-item/pagination progress
  (`logger.debug("fetched %d of ~%d (cursor=%s)", ...)`). Capture level is configurable via
  `CV_LOG_CAPTURE_LEVEL` — keep debug lines cheap and useful.
- **NEVER log secrets** — no tokens, refresh tokens, passwords, cookies, `Authorization` headers, or full
  credential blobs. Log the account label / username and the error, never the secret. A response snippet that
  might echo a token must be redacted.
- **Timeouts/network** get their own clear reason (`_is_timeout_error` → "Connection timeout: …"); don't
  collapse a network blip into a generic failure — it changes the operator's remediation.

