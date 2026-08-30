"""
Configuration catalog — the registry of known node settings a configuration
profile can carry. Drives the admin editor (contextual key autocomplete, example
values, grouping, inline help) and server-side validation/coercion on save.

Extensible: add an entry here and it immediately appears in the editor and is
validated. Unknown keys are still allowed (stored as-is) so profiles aren't a
bottleneck for new settings — but cataloged keys get typing + examples + help.
"""

from __future__ import annotations

# type: int | float | bool | string | csv | json
CONFIG_CATALOG: list[dict] = [
    # --- Scheduling -------------------------------------------------------- #
    {"key": "CV_SYNC_INTERVAL_MINUTES", "label": "Default backup interval",
     "type": "int", "group": "Scheduling", "example": "60", "unit": "minutes",
     "description": "Minutes between scheduled backups for mappings using the default cadence."},
    {"key": "CV_SCHEDULER_TICK_SECONDS", "label": "Scheduler tick",
     "type": "int", "group": "Scheduling", "example": "30", "unit": "seconds",
     "description": "How often the scheduler wakes to check for due work (minimum 15)."},

    # --- Locale ------------------------------------------------------------ #
    {"key": "CV_TIMEZONE", "label": "Node timezone",
     "type": "string", "group": "Locale", "choices": "timezone", "example": "America/New_York",
     "description": "IANA timezone for this node. Governs the daily-summary send time, "
                    "server log timestamps and other server-side time formatting."},

    # --- Notifications ----------------------------------------------------- #
    {"key": "notif.source_repeat_hours", "label": "Source-problem repeat window",
     "type": "int", "group": "Notifications", "example": "24", "unit": "hours",
     "description": "Hours between repeated 'source needs attention' emails."},
    {"key": "notif.enabled_insights", "label": "Daily-summary insights",
     "type": "csv", "group": "Notifications", "example": "footprint",
     "description": "Comma-separated insight cards to include in the daily summary email."},
    {"key": "notif.daily_hour", "label": "Daily summary send time",
     "type": "int", "group": "Notifications", "example": "8", "unit": "hour (0–23)",
     "description": "Hour of day to send the daily summary, in the node's timezone (CV_TIMEZONE)."},

    # --- Telemetry --------------------------------------------------------- #
    {"key": "CV_HEARTBEAT_INTERVAL_SECONDS", "label": "Heartbeat interval",
     "type": "int", "group": "Telemetry", "example": "60", "unit": "seconds",
     "description": "How often this node reports health to the control plane."},
    {"key": "CV_METRICS_RETENTION_DAYS", "label": "Metrics retention",
     "type": "int", "group": "Telemetry", "example": "90", "unit": "days",
     "description": "How long per-node health time-series is kept."},

    # --- Storage ----------------------------------------------------------- #
    {"key": "CV_CONTENT_CHUNK_BYTES", "label": "Content chunk size",
     "type": "int", "group": "Storage", "example": "8388608", "unit": "bytes",
     "description": "Chunk size used to split large content into encrypted units at rest."},

    # --- Assigned services (per-node backend selection) -------------------- #
    {"key": "service.storage", "label": "Arkive Cloud storage service",
     "type": "string", "group": "Services", "choices": "storage-service",
     "description": "Which storage ServiceObject this node uses for Arkive Cloud object storage."},
    {"key": "service.email", "label": "Email service",
     "type": "string", "group": "Services", "choices": "email-service",
     "description": "Which email ServiceObject this node uses to send mail (SES / SendGrid / SMTP)."},
    {"key": "service.payment", "label": "Payment processor",
     "type": "string", "group": "Services", "choices": "payment-service",
     "description": "Which payment ServiceObject this node uses to process billing (Stripe / PayPal)."},
    {"key": "tenant.default_shared", "label": "Default shared account tenant",
     "type": "string", "group": "Services", "choices": "shared-tenant",
     "description": "The shared tenant that personal (downgraded) accounts on this node belong to."},
]

_INDEX = {c["key"]: c for c in CONFIG_CATALOG}
_TYPES = {"int", "float", "bool", "string", "csv", "json"}


def catalog() -> list[dict]:
    return CONFIG_CATALOG


def catalog_index() -> dict:
    return _INDEX


def _coerce(t: str, v):
    if t == "int":
        return int(v)
    if t == "float":
        return float(v)
    if t == "bool":
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1", "true", "yes", "on")
    if t == "csv":
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        return [x.strip() for x in str(v).split(",") if x.strip()]
    return str(v)


def validate_data(data: dict) -> tuple[dict, list[str]]:
    """Coerce cataloged keys to their declared type; collect any errors. Unknown
    keys pass through unchanged so the profile can still hold ad-hoc settings."""
    coerced: dict = {}
    errors: list[str] = []
    for k, v in (data or {}).items():
        spec = _INDEX.get(k)
        if spec is None:
            coerced[k] = v
            continue
        try:
            coerced[k] = _coerce(spec["type"], v)
        except (ValueError, TypeError):
            errors.append(f"{k}: expected {spec['type']}")
            coerced[k] = v
        # Timezone values must be a valid IANA name so the scheduler never gets a
        # bad zone (which would silently fall back to UTC).
        if k == "CV_TIMEZONE" and coerced.get(k):
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(str(coerced[k]))
            except Exception:  # noqa: BLE001
                errors.append(f"{k}: unknown timezone {coerced[k]!r}")
    return coerced, errors
