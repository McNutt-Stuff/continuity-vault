"""Microsoft 365 Managed Integration — data model (spec §19).

Self-contained package schema. These are all NEW tables, so SQLAlchemy's
``create_all`` provisions them automatically (no migration needed) once this
module is imported — which happens at startup via integration auto-discovery,
before ``init_db``. Every row is organization-scoped (``tenant_id``) and bound to
its integration instance; the assigned node id scopes federation. Microsoft
payloads, plaintext indexes, reusable credentials and private keys never live in
these control-plane tables.

Table names are namespaced ``m365_*`` so the package owns its schema and cannot
collide with other packages.
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
    Text,
    UniqueConstraint,
)

from ...db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ManagedCredentialRef(Base):
    """Organization-authorized Microsoft credential REFERENCE. It records consent
    and application metadata only — the reusable secret/token lives on the assigned
    customer node, never here. Used to prove a tenant is connected and to route
    node token acquisition."""

    __tablename__ = "m365_managed_credentials"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    integration_instance_id = Column(String, index=True, nullable=False)
    provider = Column(String, default="microsoft")
    auth_model = Column(String, default="oauth_admin_consent")  # oauth_admin_consent | customer_app
    microsoft_tenant_id = Column(String, default="", index=True)
    application_id = Column(String, default="")
    consent_state = Column(String, default="pending")  # pending | granted | revoked | error
    cert_thumbprint = Column(String, default="")
    cert_expiry = Column(DateTime, nullable=True)
    assigned_node_id = Column(String, nullable=True, index=True)
    scopes_granted = Column(JSON, default=list)   # capability bundles consented
    meta = Column(JSON, default=dict)             # non-secret; NEVER a reusable secret
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class ExternalIdentity(Base):
    """A discovered Microsoft Entra identity. The durable key is
    ``(tenant_id, microsoft_tenant_id, entra_object_id)`` — scoped to the Arkive
    tenant because two Arkive tenants can back up the SAME Microsoft 365 org (so the
    same Entra identity legitimately appears once per tenant). UPN/email are mutable
    snapshots."""

    __tablename__ = "m365_external_identities"
    __table_args__ = (UniqueConstraint("tenant_id", "microsoft_tenant_id",
                                       "entra_object_id",
                                       name="uq_m365_ext_identity_tenant"),)
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    integration_instance_id = Column(String, index=True, nullable=False)
    microsoft_tenant_id = Column(String, default="", index=True)
    entra_object_id = Column(String, default="", index=True)  # durable identity
    upn = Column(String, default="")             # mutable snapshot
    email = Column(String, default="")           # mutable snapshot
    display_name = Column(String, default="")
    account_enabled = Column(Boolean, default=True)
    user_type = Column(String, default="member")  # member | guest
    in_scope = Column(Boolean, default=False)
    scope_reason = Column(String, default="")
    state = Column(String, default="discovered")  # discovered|mapped|suggested_match|new_user_candidate|protected_only|conflict|excluded|stale
    meta = Column(JSON, default=dict)
    first_seen = Column(DateTime, default=_now)
    last_seen = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class ExternalIdentityBinding(Base):
    """Binds an ExternalIdentity to an Arkive user. Mapping a Microsoft user does
    NOT itself grant portal access (that's a separate account/invite decision)."""

    __tablename__ = "m365_identity_bindings"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    integration_instance_id = Column(String, index=True, nullable=False)
    external_identity_id = Column(String, index=True, nullable=False)
    user_id = Column(String, nullable=True, index=True)   # null for protected-only
    protected_only = Column(Boolean, default=False)
    mapping_method = Column(String, default="")  # object_id|oidc|verified_email|alt_email|manual
    status = Column(String, default="suggested")  # mapped|suggested|protected_only|conflict|excluded|stale
    approved_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class IdentityScopePolicy(Base):
    """The admin-defined scope (which Entra users/resources are in scope) with
    deterministic include/exclude precedence."""

    __tablename__ = "m365_scope_policies"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    integration_instance_id = Column(String, index=True, nullable=False)
    rules = Column(JSON, default=dict)   # {administrative_units, groups, domains, includes, excludes, ...}
    version = Column(Integer, default=1)
    updated_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class ManagedSource(Base):
    """A managed Microsoft source (user-associated) or organization source. Read-only
    to its assigned user; governed by managed mappings/rules. Content authorization
    is separate from connector-credential authorization."""

    __tablename__ = "m365_managed_sources"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    integration_instance_id = Column(String, index=True, nullable=False)
    workload = Column(String, default="")        # exchange|onedrive|sharepoint|teams|entra
    ownership_type = Column(String, default="managed_user")  # managed_user | organization
    owner_user_id = Column(String, nullable=True, index=True)  # set for managed_user
    managed_credential_id = Column(String, nullable=True)
    source_key = Column(String, default="", index=True)  # Microsoft resource id
    name = Column(String, default="")
    visibility = Column(JSON, default=dict)      # {source, metadata, content, recovery}
    state = Column(String, default="planned")    # planned|provisioning|baseline_pending|active|empty|delayed|partial|paused_by_admin|permission_required|credential_error|source_unavailable|retention_hold|disconnected|decommissioned
    assigned_node_id = Column(String, nullable=True, index=True)
    config = Column(JSON, default=dict)
    last_collected_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class SourceAssignment(Base):
    """Assigns a managed/organization source to a user, group, or the organization."""

    __tablename__ = "m365_source_assignments"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    managed_source_id = Column(String, index=True, nullable=False)
    assignee_type = Column(String, default="user")  # user | group | organization
    assignee_id = Column(String, default="")
    role = Column(String, default="viewer")          # owner | custodian | viewer
    created_at = Column(DateTime, default=_now)


class ManagedMapping(Base):
    """Administrator-defined mapping of source data → Arkive collections/objects/
    index. Active versions are immutable; ``active_version`` points at the live one."""

    __tablename__ = "m365_managed_mappings"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    integration_instance_id = Column(String, index=True, nullable=False)
    name = Column(String, default="")
    mapping_type = Column(String, default="source_to_arkive")
    status = Column(String, default="draft")     # draft | active | retired
    active_version = Column(Integer, default=0)
    visibility = Column(JSON, default=dict)      # user_can_see_mapping/metadata/content
    created_by = Column(String, nullable=True)
    approved_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class ManagedMappingVersion(Base):
    """An immutable version of a managed mapping."""

    __tablename__ = "m365_managed_mapping_versions"
    __table_args__ = (UniqueConstraint("mapping_id", "version", name="uq_m365_mapping_version"),)
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    mapping_id = Column(String, index=True, nullable=False)
    version = Column(Integer, default=1)
    spec = Column(JSON, default=dict)            # selectors, transforms, relationships, indexed_fields
    source_schema_version = Column(String, default="")
    target_schema_version = Column(String, default="")
    created_at = Column(DateTime, default=_now)


class ManagedMappingAssignment(Base):
    __tablename__ = "m365_managed_mapping_assignments"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    mapping_id = Column(String, index=True, nullable=False)
    assignee_type = Column(String, default="organization")  # organization|group|user|source
    assignee_id = Column(String, default="")
    created_at = Column(DateTime, default=_now)


class ManagedRule(Base):
    """Centrally assigned policy (collection/classification/retention/legal hold/
    visibility/recovery/notifications). Users cannot alter managed rules."""

    __tablename__ = "m365_managed_rules"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    integration_instance_id = Column(String, index=True, nullable=False)
    name = Column(String, default="")
    rule_family = Column(String, default="collection")  # collection|schedule|retention|legal_hold|classification|indexing|visibility|recovery|notifications|residency
    status = Column(String, default="draft")     # draft | active | retired
    active_version = Column(Integer, default=0)
    created_by = Column(String, nullable=True)
    approved_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class ManagedRuleVersion(Base):
    __tablename__ = "m365_managed_rule_versions"
    __table_args__ = (UniqueConstraint("rule_id", "version", name="uq_m365_rule_version"),)
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    rule_id = Column(String, index=True, nullable=False)
    version = Column(Integer, default=1)
    spec = Column(JSON, default=dict)
    created_at = Column(DateTime, default=_now)


class ManagedRuleAssignment(Base):
    __tablename__ = "m365_managed_rule_assignments"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    rule_id = Column(String, index=True, nullable=False)
    assignee_type = Column(String, default="organization")
    assignee_id = Column(String, default="")
    created_at = Column(DateTime, default=_now)


class EffectivePolicySnapshot(Base):
    """The deterministic compile of package defaults + org/group/source rules +
    exceptions for a scope, with the exact mapping/rule versions used."""

    __tablename__ = "m365_effective_policy_snapshots"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    integration_instance_id = Column(String, index=True, nullable=False)
    scope_ref = Column(String, default="")       # source/user/group/org this applies to
    compiled = Column(JSON, default=dict)
    mapping_versions = Column(JSON, default=dict)
    rule_versions = Column(JSON, default=dict)
    created_at = Column(DateTime, default=_now)


class IntegrationDesiredState(Base):
    """Versioned desired configuration the control plane sends to the assigned node;
    the node acknowledges the applied version (optimistic concurrency)."""

    __tablename__ = "m365_desired_states"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    integration_instance_id = Column(String, index=True, nullable=False)
    node_id = Column(String, nullable=True, index=True)
    version = Column(Integer, default=1)
    desired = Column(JSON, default=dict)
    applied_version = Column(Integer, default=0)
    applied_at = Column(DateTime, nullable=True)
    status = Column(String, default="pending_node")  # pending_node|applying|applied|drifted|failed
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class CompliancePack(Base):
    """A versioned framework pack (NIST CSF, CIS, ISO 27001, SOC 2, HIPAA, …)."""

    __tablename__ = "m365_compliance_packs"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    integration_instance_id = Column(String, index=True, nullable=True)
    framework = Column(String, default="")       # nist_csf|cis|iso27001|soc2|hipaa|pci|gdpr|cmmc|bms
    version = Column(String, default="1.0")
    enabled = Column(Boolean, default=False)
    config = Column(JSON, default=dict)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class ComplianceControl(Base):
    __tablename__ = "m365_compliance_controls"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    pack_id = Column(String, index=True, nullable=False)
    control_id = Column(String, default="")      # framework control identifier
    title = Column(String, default="")
    state = Column(String, default="not_assessed")  # not_assessed|not_applicable|planned|partially_implemented|implemented|operating|exception|failed|evidence_stale
    owner = Column(String, nullable=True)
    evidence_refs = Column(JSON, default=list)
    due_date = Column(DateTime, nullable=True)
    last_evaluated_at = Column(DateTime, nullable=True)
    meta = Column(JSON, default=dict)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class ComplianceException(Base):
    __tablename__ = "m365_compliance_exceptions"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    control_id = Column(String, index=True, nullable=False)
    reason = Column(Text, default="")
    approved_by = Column(String, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now)


class RecoveryJob(Base):
    """An export-first (v1) or restore (gated) recovery request against a source."""

    __tablename__ = "m365_recovery_jobs"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    integration_instance_id = Column(String, index=True, nullable=False)
    managed_source_id = Column(String, index=True, nullable=True)
    kind = Column(String, default="export")      # export | restore
    destination = Column(String, default="")     # original | alternate
    state = Column(String, default="requested")  # requested|approved|staging|running|complete|failed|denied
    selection = Column(JSON, default=dict)        # time/version/object selection
    requested_by = Column(String, nullable=True)
    node_id = Column(String, nullable=True)
    result = Column(JSON, default=dict)           # manifest ref, counts, warnings (no payload)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class RecoveryApproval(Base):
    __tablename__ = "m365_recovery_approvals"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    recovery_job_id = Column(String, index=True, nullable=False)
    approver_id = Column(String, nullable=True)
    decision = Column(String, default="pending")  # pending | approved | denied
    created_at = Column(DateTime, default=_now)
