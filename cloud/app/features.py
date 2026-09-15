"""Admin-controlled capability flags, resolved per user / per tenant.

A flag defaults ON; an admin turns it OFF to remove a capability. The key flag is
``purge_enabled`` — cleared to place a user or tenant under a legal hold so they
can no longer delete (purge) their protected data.

Resolution:
- Personal accounts (shared tenant) carry ONLY user-level flags.
- Org tenants (dedicated/restricted/internal) may set a flag tenant-wide; a
  tenant-level *disable* is authoritative (a legal hold can't be user-overridden),
  otherwise a user-level value applies over the tenant/default.
"""

from __future__ import annotations

# flag name -> default when neither user nor tenant sets it
FLAGS: dict[str, bool] = {
    "purge_enabled": True,   # may the account delete (purge) its protected data?
    "insights_enabled": True,  # show the Insights page (digital-footprint findings)?
    "cloud_storage_enabled": True,  # show/allow Cloud Storage (bring-your-own buckets)?
    "integrations_enabled": True,   # show/allow Integrations (network intelligence)?
    # Rules engine: declarative ingestion rules that label, restrict, obfuscate,
    # don't-index or discard data. OFF by default. NOTE: the rules engine can HELP
    # DRIVE compliance (it feeds the data_classification/data_minimization
    # capabilities) — it is NOT the compliance feature itself. Compliance posture
    # lives behind `compliance_enabled`.
    "rules_enabled": False,
    # Compliance engine: framework posture (NIST CSF, CIS, HIPAA, …) scored from
    # live evidence provided by Arkive + its integrations. OFF by default and
    # Business/Enterprise only (gated in the API alongside this flag).
    "compliance_enabled": False,
    # Advanced Ubiquiti/UniFi analytics: per-user + org-level network drilldowns,
    # device→user mapping analytics, and collection-gap detection. OFF by default.
    "advanced_ubiquiti_analytics": False,
    # Microsoft 365 Managed Integration (Business/Enterprise, admin-governed). Gated
    # by plan AND this flag; OFF by default until the collection backend ships.
    "m365_managed_integration": False,
    # Org admins may search + recover ANOTHER member's protected data (org-wide /
    # per-user scope). ON by default for org tenants; clear it for a privacy/legal
    # hold so admins are confined to their own vaults. Every cross-member access is
    # audited regardless.
    "admin_cross_member_access": True,
    # Require a second org admin to approve a cross-member RECOVERY (dual control /
    # "break glass") before the data is released. OFF by default.
    "cross_member_recovery_approval": False,
}

# Human labels for the admin UI.
LABELS = {
    "purge_enabled": "Allow data purge",
    "insights_enabled": "Digital-footprint Insights",
    "cloud_storage_enabled": "Cloud Storage (bring-your-own)",
    "integrations_enabled": "Integrations (network intelligence)",
    "rules_enabled": "Rules engine",
    "compliance_enabled": "Compliance engine (framework posture — Business)",
    "advanced_ubiquiti_analytics": "Advanced Ubiquiti Analytics (per-user & org)",
    "m365_managed_integration": "Microsoft 365 Managed Integration (Business/Enterprise)",
    "admin_cross_member_access": "Admin cross-member data access (search & recover)",
    "cross_member_recovery_approval": "Require approval for cross-member recovery",
}


def defaults() -> dict:
    return dict(FLAGS)


def _shared(tenant) -> bool:
    return ((tenant.tenant_type if tenant else "dedicated") or "dedicated") == "shared"


def resolve(user, tenant, name: str) -> bool:
    default = FLAGS.get(name, False)
    uf = (user.feature_flags or {}) if user else {}
    tf = (tenant.feature_flags or {}) if tenant else {}
    # Tenant-level disable is authoritative (legal hold) for org tenants.
    if not _shared(tenant) and tf.get(name) is False:
        return False
    if name in uf:
        return bool(uf[name])
    if not _shared(tenant) and name in tf:
        return bool(tf[name])
    return bool(default)


def effective(user, tenant) -> dict:
    return {name: resolve(user, tenant, name) for name in FLAGS}
