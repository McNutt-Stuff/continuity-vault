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
}

# Human labels for the admin UI.
LABELS = {
    "purge_enabled": "Allow data purge",
    "insights_enabled": "Digital-footprint Insights",
    "cloud_storage_enabled": "Cloud Storage (bring-your-own)",
    "integrations_enabled": "Integrations (network intelligence)",
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
