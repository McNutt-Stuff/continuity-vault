"""Google Workspace Managed Integration — data model.

Self-contained package schema mirroring the Microsoft 365 managed integration.
All NEW tables (``gw_*``), so SQLAlchemy ``create_all`` provisions them at startup
via integration auto-discovery — no migration needed. Every row is organization-
scoped (``tenant_id``) and bound to its integration instance; the assigned node id
scopes federation. Google payloads, plaintext indexes, reusable credentials and
private keys never live in these control-plane tables — only consent/metadata and
derived, non-secret discovery state.

Auth model: a Google **service account with domain-wide delegation**. A Workspace
super-admin authorizes read-only scopes once; the assigned node holds the service
account key and impersonates each in-scope user to collect their data. The control
plane records only the authorization reference (subject admin, delegated scopes,
consent state) — never the key.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)

from ...db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class GwManagedCredential(Base):
    """Organization-authorized Google credential REFERENCE. Records consent +
    service-account metadata only — the reusable key lives on the assigned customer
    node, never here. Proves a tenant is connected and routes node token use."""

    __tablename__ = "gw_managed_credentials"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    integration_instance_id = Column(String, index=True, nullable=False)
    provider = Column(String, default="google")
    auth_model = Column(String, default="domain_wide_delegation")  # domain_wide_delegation | oauth
    customer_id = Column(String, default="", index=True)     # Google Workspace customer id (durable)
    primary_domain = Column(String, default="")
    service_account_email = Column(String, default="")       # non-secret; the key is on the node
    subject_admin = Column(String, default="")               # admin the SA impersonates for directory calls
    consent_state = Column(String, default="pending")        # pending | granted | revoked | error
    scopes_granted = Column(JSON, default=list)              # delegated read-only scopes authorized
    assigned_node_id = Column(String, nullable=True, index=True)
    meta = Column(JSON, default=dict)                        # non-secret; NEVER a reusable secret
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class GwExternalIdentity(Base):
    """A discovered Google Workspace directory user. Durable key is
    ``(tenant_id, customer_id, google_user_id)`` — scoped to the Arkive tenant so
    two Arkive tenants can protect the SAME Workspace org. Email/name are mutable
    snapshots."""

    __tablename__ = "gw_external_identities"
    __table_args__ = (UniqueConstraint("tenant_id", "customer_id", "google_user_id",
                                       name="uq_gw_ext_identity_tenant"),)
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    integration_instance_id = Column(String, index=True, nullable=False)
    customer_id = Column(String, default="", index=True)
    google_user_id = Column(String, default="", index=True)  # durable identity id
    primary_email = Column(String, default="")               # mutable snapshot
    display_name = Column(String, default="")
    suspended = Column(Boolean, default=False)
    is_admin = Column(Boolean, default=False)
    org_unit_path = Column(String, default="")
    user_type = Column(String, default="member")             # member | guest (external)
    in_scope = Column(Boolean, default=False)
    scope_reason = Column(String, default="")
    state = Column(String, default="discovered")             # discovered|mapped|suggested_match|new_user_candidate|protected_only|excluded|stale
    meta = Column(JSON, default=dict)
    first_seen = Column(DateTime, default=_now)
    last_seen = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class GwIdentityBinding(Base):
    """Binds a GwExternalIdentity to an Arkive user. Mapping does NOT itself grant
    portal access (a separate account/invite decision)."""

    __tablename__ = "gw_identity_bindings"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    integration_instance_id = Column(String, index=True, nullable=False)
    external_identity_id = Column(String, index=True, nullable=False)
    user_id = Column(String, nullable=True, index=True)      # null for protected-only
    protected_only = Column(Boolean, default=False)
    mapping_method = Column(String, default="")              # google_user_id|verified_email|alt_email|manual
    status = Column(String, default="suggested")             # mapped|suggested|protected_only|conflict|excluded|stale
    approved_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class GwScopePolicy(Base):
    """Admin-defined scope (which directory users are in scope) with deterministic
    include/exclude precedence (mirrors the M365 scope policy)."""

    __tablename__ = "gw_scope_policies"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    integration_instance_id = Column(String, index=True, nullable=False)
    rules = Column(JSON, default=dict)   # {org_units, groups, domains, includes, excludes, include_guests, ...}
    version = Column(Integer, default=1)
    updated_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class GwManagedSource(Base):
    """A managed Google source (per-user workload) or organization source
    (Shared Drive). Read-only to its assigned user; governed by the main rules
    engine like any other Collection."""

    __tablename__ = "gw_managed_sources"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    integration_instance_id = Column(String, index=True, nullable=False)
    workload = Column(String, default="")        # gmail|google_drive|google_calendar|google_contacts|google_photos|shared_drive
    ownership_type = Column(String, default="managed_user")  # managed_user | organization
    owner_user_id = Column(String, nullable=True, index=True)  # set for managed_user
    managed_credential_id = Column(String, nullable=True)
    source_key = Column(String, default="", index=True)  # Google resource id (user email / shared-drive id)
    name = Column(String, default="")
    visibility = Column(JSON, default=dict)      # {source, metadata, content, recovery}
    state = Column(String, default="planned")    # planned|provisioning|baseline_pending|active|empty|paused_by_admin|permission_required|credential_error|source_unavailable|disconnected|decommissioned
    assigned_node_id = Column(String, nullable=True, index=True)
    config = Column(JSON, default=dict)
    last_collected_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)
