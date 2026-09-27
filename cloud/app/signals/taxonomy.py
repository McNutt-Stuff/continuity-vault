"""Arkive Signal Platform — taxonomy.

Categories, quality enums, and the signal-type DEFINITION registry (kind + default
severity + freshness expectation). Kept in code (not a table) so providers and the
Phase-2 AI engine share one authoritative vocabulary without a schema change. A
signal type not explicitly registered resolves via its ``domain.`` prefix, so
providers can emit new types without editing this file — while known types get a
tuned freshness/severity.
"""

from __future__ import annotations

from dataclasses import dataclass

# Extensible category taxonomy (spec §5). AI/MCP present from Phase 1 so Phase 2
# needs no schema change.
CATEGORIES: tuple[str, ...] = (
    "IDENTITY", "ENDPOINT", "NETWORK", "APPLICATION", "SAAS", "CLOUD", "DATA",
    "SECURITY", "DEVICE", "ACCESS", "AUTHENTICATION", "AUTHORIZATION",
    "CONFIGURATION", "VULNERABILITY", "PATCH", "MALWARE", "ENCRYPTION", "BACKUP",
    "RECOVERY", "RESILIENCE", "ARKIVE", "AI", "MCP",
)

KINDS = ("state", "event", "measurement")
SEVERITIES = ("info", "low", "medium", "high", "critical")
CONFIDENCE = ("low", "moderate", "high", "very_high")
FRESHNESS = ("fresh", "stale", "expired", "unknown")

_HOUR = 3600
_DAY = 24 * _HOUR

# domain prefix -> (category, default freshness seconds). A signal_type's first
# dotted segment selects this when no explicit definition exists.
_PREFIX_CATEGORY: dict[str, tuple[str, int]] = {
    "endpoint": ("ENDPOINT", 1 * _DAY),
    "identity": ("IDENTITY", 1 * _DAY),
    "network": ("NETWORK", 6 * _HOUR),
    "application": ("APPLICATION", 1 * _DAY),
    "saas": ("SAAS", 1 * _DAY),
    "cloud": ("CLOUD", 1 * _DAY),
    "data": ("DATA", 1 * _DAY),
    "security": ("SECURITY", 12 * _HOUR),
    "device": ("DEVICE", 1 * _DAY),
    "node": ("ARKIVE", 15 * 60),
    "arkive": ("ARKIVE", 1 * _DAY),
    "ai": ("AI", 1 * _DAY),
    "mcp": ("MCP", 1 * _DAY),
}


@dataclass(frozen=True)
class Definition:
    signal_type: str
    category: str
    kind: str = "state"
    default_severity: str = "info"
    freshness_seconds: int = _DAY   # 0 = never expires
    label: str = ""


# Explicit overrides for well-known types (freshness/severity/kind tuned).
_DEFS: dict[str, Definition] = {}


def _reg(signal_type: str, category: str, kind: str = "state",
         severity: str = "info", freshness: int = _DAY, label: str = "") -> None:
    _DEFS[signal_type] = Definition(signal_type, category, kind, severity, freshness, label)


# --- Endpoint posture (24h freshness) ------------------------------------------
_reg("endpoint.disk_encryption.enabled", "ENCRYPTION", severity="high", label="Disk encryption")
_reg("endpoint.firewall.enabled", "SECURITY", severity="medium", label="Firewall")
_reg("endpoint.secure_boot.enabled", "SECURITY", severity="medium", label="Secure boot")
_reg("endpoint.screen_lock.enabled", "SECURITY", severity="low", label="Screen lock")
_reg("endpoint.antimalware.running", "MALWARE", severity="high", label="Anti-malware running")
_reg("endpoint.auto_update.enabled", "PATCH", severity="low", label="Automatic updates")
_reg("endpoint.reboot_required", "PATCH", severity="low", label="Reboot required")
_reg("endpoint.patch_age", "PATCH", kind="measurement", severity="medium", label="Patch age")
_reg("endpoint.application.installed", "APPLICATION", label="Installed application")

# --- Arkive-native protection / recovery (spec §16) ----------------------------
_reg("arkive.agent.running", "ARKIVE", severity="medium", freshness=1 * _HOUR, label="Agent running")
_reg("arkive.protection.enabled", "ARKIVE", severity="high", label="Protection enabled")
_reg("arkive.source.protection_status", "ARKIVE", severity="high", label="Source protection")
_reg("arkive.source.last_success", "BACKUP", kind="measurement", label="Last successful backup")
_reg("arkive.source.sync_health", "BACKUP", severity="medium", label="Source sync health")
_reg("arkive.backup.current", "BACKUP", severity="high", label="Backup current")
_reg("arkive.backup.coverage", "BACKUP", kind="measurement", label="Backup coverage")
_reg("arkive.immutable_copy.present", "RESILIENCE", severity="high", label="Immutable copy")
_reg("arkive.recovery_readiness", "RECOVERY", severity="high", label="Recovery readiness")
_reg("arkive.recovery_test.status", "RECOVERY", severity="medium", label="Recovery test")
_reg("arkive.encryption.status", "ENCRYPTION", severity="high", label="Encryption")

# --- Node / appliance operational (short freshness) ----------------------------
_reg("node.online", "ARKIVE", severity="high", freshness=15 * 60, label="Node online")
_reg("node.storage.health", "ARKIVE", severity="high", freshness=15 * 60, label="Node storage health")
_reg("node.storage.free", "ARKIVE", kind="measurement", freshness=15 * 60, label="Node free storage")
_reg("node.updates.current", "PATCH", severity="low", freshness=1 * _HOUR, label="Node updates current")
_reg("appliance.online", "DEVICE", severity="high", freshness=20 * 60, label="Appliance online")
_reg("appliance.storage.health", "DEVICE", severity="high", freshness=20 * 60, label="Appliance storage health")
_reg("appliance.mirror.in_sync", "RESILIENCE", severity="medium", freshness=30 * 60, label="Mirror in sync")

# --- Identity (M365) -----------------------------------------------------------
_reg("identity.user.active", "IDENTITY", label="User active")
_reg("identity.user.disabled", "IDENTITY", severity="low", label="User disabled")
_reg("identity.user.external", "IDENTITY", severity="low", label="External user")
_reg("identity.user.stale", "IDENTITY", severity="medium", label="Stale user")
_reg("identity.mfa.registered", "AUTHENTICATION", severity="high", label="MFA registered")
_reg("identity.mfa.enabled", "AUTHENTICATION", severity="info", label="MFA enabled")
_reg("identity.mfa.missing", "AUTHENTICATION", severity="high", label="MFA not registered")
_reg("identity.privileged.account", "AUTHORIZATION", severity="info", label="Privileged account")
_reg("identity.admin_role.assigned", "AUTHORIZATION", severity="medium", label="Admin role")
_reg("identity.device.compliant", "DEVICE", severity="medium", label="Device compliant")
_reg("identity.device.managed", "DEVICE", severity="low", label="Device managed")

# --- Applications / OAuth (M365) — AI-ready metadata ---------------------------
_reg("application.enterprise.registered", "APPLICATION", label="Enterprise application")
_reg("application.oauth.grant", "AUTHORIZATION", severity="medium", label="OAuth grant")
_reg("application.oauth.high_privilege", "AUTHORIZATION", severity="high", label="High-privilege OAuth")

# --- Network (Ubiquiti) --------------------------------------------------------
_reg("network.gateway.online", "NETWORK", severity="high", freshness=1 * _HOUR, label="Gateway online")
_reg("network.device.online", "NETWORK", severity="medium", freshness=1 * _HOUR, label="Network device online")
_reg("network.device.update_available", "PATCH", severity="low", label="Firmware update available")
_reg("network.client.detected", "NETWORK", freshness=6 * _HOUR, label="Network client")
_reg("network.application.detected", "APPLICATION", kind="event", label="Network application seen")
_reg("network.destination.detected", "NETWORK", kind="event", label="Network destination seen")
_reg("network.ids.enabled", "SECURITY", severity="medium", label="IDS enabled")
_reg("network.guest_isolation.enabled", "CONFIGURATION", severity="low", label="Guest isolation")


def definition(signal_type: str) -> Definition:
    """Resolve a signal type to its definition; fall back to the domain prefix so
    unregistered types are still categorized + given a sane freshness."""
    d = _DEFS.get(signal_type)
    if d is not None:
        return d
    prefix = (signal_type.split(".", 1)[0] if signal_type else "").lower()
    cat, fresh = _PREFIX_CATEGORY.get(prefix, ("CONFIGURATION", _DAY))
    return Definition(signal_type, cat, "state", "info", fresh, "")


def all_definitions() -> list[Definition]:
    return list(_DEFS.values())
