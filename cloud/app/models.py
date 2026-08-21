"""
Cloud control-plane data model (spec 14).

Multi-tenant isolation is enforced at the application, identity, storage-prefix,
policy, and encryption layers. Every tenant-scoped row carries ``tenant_id`` and
all queries in the API layer filter by the authenticated tenant.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import deferred, relationship

from .db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Tenant(Base):
    __tablename__ = "tenants"
    id = Column(String, primary_key=True, default=_uuid)
    name = Column(String, nullable=False)
    plan = Column(String, default="business")  # consumer | family | business | enterprise
    key_ownership_model = Column(String, default="customer-managed")  # spec 10.x
    storage_prefix = Column(String, nullable=False)  # tenant isolation in S3
    licensed_bytes = Column(BigInteger, default=0)  # data allowance they pay for (0 = unlimited)
    protection_options = Column(JSON, default=list)  # enabled storage tiers (feature gating)
    appliance_plan = Column(JSON, default=list)  # desired appliances [{capacity_tb, qty}]
    status = Column(String, default="active")
    created_at = Column(DateTime, default=_now)

    users = relationship("User", back_populates="tenant", cascade="all, delete-orphan")
    vaults = relationship("Vault", back_populates="tenant", cascade="all, delete-orphan")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_tenant_email"),)
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    email = Column(String, nullable=False)
    display_name = Column(String, nullable=False)
    role = Column(String, default="member")  # owner | security-admin | member | support-admin
    is_platform_admin = Column(Boolean, default=False)  # backend admin console
    email_verified = Column(Boolean, default=False)
    status = Column(String, default="active")
    created_at = Column(DateTime, default=_now)

    tenant = relationship("Tenant", back_populates="users")
    passkeys = relationship("Passkey", back_populates="user", cascade="all, delete-orphan")


class Passkey(Base):
    """WebAuthn/passkey or hardware-token credential used to unlock portal
    interfaces and authorize sensitive operations (spec: user-owned keys /
    passkeys / hardware tokens unlock data-access interfaces)."""

    __tablename__ = "passkeys"
    id = Column(String, primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    credential_id = Column(String, nullable=False, unique=True)
    public_key = Column(Text, nullable=False)
    sign_count = Column(Integer, default=0)
    transport = Column(String, default="internal")  # internal | usb | nfc | hybrid
    label = Column(String, default="Passkey")
    aaguid = Column(String, nullable=True)
    created_at = Column(DateTime, default=_now)

    user = relationship("User", back_populates="passkeys")


class Vault(Base):
    __tablename__ = "vaults"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    # The member who owns this vault. Vaults are the demarcation of a user's data:
    # a member only ever sees content in vaults they own; org admins see aggregate
    # statistics across the org but never another member's actual content.
    owner_user_id = Column(String, ForeignKey("users.id"), nullable=True, index=True)
    name = Column(String, nullable=False)
    key_ownership_model = Column(String, default="customer-managed")
    crypto_profile_id = Column(String, default="cvp-hybrid-2026a")
    # Wrapped vault key material for each recovery recipient (spec 9.4).
    wrapped_keys = Column(JSON, default=list)
    created_at = Column(DateTime, default=_now)

    tenant = relationship("Tenant", back_populates="vaults")
    collections = relationship("Collection", back_populates="vault", cascade="all, delete-orphan")


class Collection(Base):
    """A logical grouping of protected data from one source (e.g. a Gmail
    mailbox, a 1Password account) with its own key and protection policy."""

    __tablename__ = "collections"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    vault_id = Column(String, ForeignKey("vaults.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    source_type = Column(String, nullable=False)  # connector type
    connector_account_id = Column(String, ForeignKey("connector_accounts.id"), nullable=True)
    agent_id = Column(String, ForeignKey("desktop_agents.id"), nullable=True)  # agent-collected sources
    policy_id = Column(String, ForeignKey("protection_policies.id"), nullable=True)
    sensitivity = Column(String, default="standard")  # standard | sensitive | restricted
    destinations = Column(JSON, default=list)  # where this mapping stores data
    index_fields = Column(JSON, default=list)  # override of connector metadata keys to index
    config = Column(JSON, default=dict)  # source-specific settings (e.g. endpoint-files selection)
    # Auto-backup cadence: NULL = use the global default; 0 = manual only; >0 = every N minutes.
    backup_interval_minutes = Column(Integer, nullable=True)
    last_backup_run_at = Column(DateTime, nullable=True)  # last time the scheduler ran this mapping
    created_at = Column(DateTime, default=_now)

    vault = relationship("Vault", back_populates="collections")


class ConnectorAccount(Base):
    """A linked source account (OAuth/API) for a sync worker (spec: connectors
    for 1Password, Gmail, Outlook.com, OneDrive, Dropbox, iCloud)."""

    __tablename__ = "connector_accounts"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    owner_user_id = Column(String, ForeignKey("users.id"), nullable=True, index=True)
    connector_type = Column(String, nullable=False)
    account_label = Column(String, nullable=False)
    auth_status = Column(String, default="linked")  # linked | needs-reauth | revoked
    # Encrypted credential blob (never plaintext at rest).
    encrypted_credentials = Column(Text, nullable=True)
    scopes = Column(JSON, default=list)
    sync_cursor = Column(JSON, nullable=True)  # incremental sync state (e.g. Gmail historyId)
    last_sync_at = Column(DateTime, nullable=True)
    last_object_count = Column(Integer, nullable=True)  # items captured in the last sync
    last_error = Column(Text, nullable=True)            # last sync error message (NULL = healthy)
    last_error_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now)


class ProtectionPolicy(Base):
    """Destination + retention policy at the collection level (spec 8)."""

    __tablename__ = "protection_policies"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    destinations = Column(JSON, default=list)  # ["cv-cloud","appliance","customer-s3"]
    backup_frequency_minutes = Column(Integer, default=60)
    cloud_staging_hours = Column(Integer, default=24)
    cloud_retention_days = Column(Integer, default=365)
    appliance_retention_days = Column(Integer, default=3650)
    rpo_minutes = Column(Integer, default=60)
    rto_minutes = Column(Integer, default=240)
    immutability_days = Column(Integer, default=365)
    required_approvals = Column(Integer, default=1)
    verification_frequency_days = Column(Integer, default=7)
    created_at = Column(DateTime, default=_now)


class Appliance(Base):
    """Offline appliance fleet record (spec 4, 5, 14)."""

    __tablename__ = "appliances"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    serial = Column(String, nullable=False, unique=True)
    model = Column(String, default="CV Edge 8")
    name = Column(String, default="Appliance")
    location_label = Column(String, default="")
    state = Column(String, default="PROVISIONING")  # spec 4.2 state machine
    isolation_state = Column(String, default="sealed")
    software_version = Column(String, default="0.0.0")
    last_heartbeat_at = Column(DateTime, nullable=True)
    last_attestation_at = Column(DateTime, nullable=True)
    attestation_ok = Column(Boolean, default=False)
    tamper_state = Column(String, default="normal")
    # Appliance-provided public signature bundle used to verify its receipts.
    identity_bundle = Column(JSON, nullable=True)
    # Cloud's signer public bundle the appliance uses to verify commands.
    telemetry = Column(JSON, default=dict)  # capacity, drives, power, temp
    agent_token_hash = Column(String, nullable=True, index=True)  # sha256 of bearer token
    command_sequence = Column(Integer, default=0)
    created_at = Column(DateTime, default=_now)


class ApplianceStorage(Base):
    """A named storage volume that belongs to an appliance (built-in disk,
    external volume, NAS…). Mappings target a *storage* object — not the
    appliance itself — the same way they target the Arkive cloud or a customer
    S3 bucket. The destination id used in mappings/receipts is ``store:<id>``."""

    __tablename__ = "appliance_storages"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    appliance_id = Column(String, ForeignKey("appliances.id"), nullable=False, index=True)
    name = Column(String, default="Built-In Storage")
    kind = Column(String, default="builtin")  # builtin | external
    capacity_bytes = Column(BigInteger, default=0)
    used_bytes = Column(BigInteger, default=0)
    health = Column(JSON, default=dict)  # drive_health, smart, raid, temperature_c
    created_at = Column(DateTime, default=_now)


class ApplianceAssignment(Base):
    """Assigns an appliance to a member. An appliance can be shared by several
    members and a member can have several appliances. A standard member sees only
    that they have access (view-only); an org admin sees every assignment and can
    change them. ``can_manage`` optionally grants a member management rights on a
    shared appliance."""

    __tablename__ = "appliance_assignments"
    __table_args__ = (UniqueConstraint("appliance_id", "user_id", name="uq_appliance_user"),)
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    appliance_id = Column(String, ForeignKey("appliances.id"), nullable=False, index=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    can_manage = Column(Boolean, default=False)
    created_at = Column(DateTime, default=_now)


class LinkingCode(Base):
    """Short-lived turnkey linking code entered during appliance activation."""

    __tablename__ = "linking_codes"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    code = Column(String, nullable=False, unique=True, index=True)
    kind = Column(String, default="appliance")  # appliance | agent
    appliance_id = Column(String, ForeignKey("appliances.id"), nullable=True)
    model = Column(String, default="CV Edge 8")
    name = Column(String, default="Appliance")
    consumed = Column(Boolean, default=False)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=_now)


class DesktopAgent(Base):
    """A managed endpoint agent (e.g. macOS) that collects locally via native
    tools (1Password CLI, etc.) and pushes encrypted data to the platform."""

    __tablename__ = "desktop_agents"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    name = Column(String, default="Desktop Agent")
    platform = Column(String, default="macos")
    hostname = Column(String, default="")
    version = Column(String, default="1.0.0")
    state = Column(String, default="active")  # active | offline | quarantined
    collectors = Column(JSON, default=list)  # e.g. ["onepassword"]
    config = Column(JSON, default=dict)  # destinations, schedule, collector opts
    pending_command = Column(JSON, nullable=True)  # legacy single slot (drained first)
    pending_commands = Column(JSON, default=list)  # FIFO queue of {type, params}
    telemetry = Column(JSON, default=dict)
    # Large blobs (the whole folder tree + cached expansions) — DEFERRED so routine
    # queries (agents list, activity, collections) don't pull megabytes per agent
    # into memory; only the folder-picker endpoints that read them pay the cost.
    last_scan = deferred(Column(JSON, nullable=True))  # latest endpoint filesystem scan result
    fs_expansions = deferred(Column(JSON, default=dict))  # lazy per-folder expansions {path: {...}}
    identity_bundle = Column(JSON, nullable=True)
    agent_token_hash = Column(String, nullable=True, index=True)  # sha256 of bearer token
    last_heartbeat_at = Column(DateTime, nullable=True)
    last_collection_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now)

    def enqueue_command(self, cmd: dict) -> None:
        """Append a command, collapsing any pending duplicate of the same kind so
        a slow/offline agent never accumulates a backlog of identical work (and so
        different sources — e.g. 1Password vs endpoint files — don't overwrite each
        other the way a single slot did)."""
        key = _command_key(cmd)
        q = [c for c in (self.pending_commands or []) if _command_key(c) != key]
        q.append(cmd)
        self.pending_commands = q

    def dequeue_command(self) -> dict | None:
        """Pop the next command (draining the legacy single slot first)."""
        if self.pending_command:
            nxt, self.pending_command = self.pending_command, None
            return nxt
        q = list(self.pending_commands or [])
        if not q:
            return None
        nxt = q.pop(0)
        self.pending_commands = q
        return nxt

    def clear_commands(self) -> None:
        self.pending_command = None
        self.pending_commands = []

    @property
    def has_pending_command(self) -> bool:
        return bool(self.pending_command) or bool(self.pending_commands)


def _command_key(cmd: dict) -> tuple:
    p = (cmd or {}).get("params") or {}
    return ((cmd or {}).get("type"), p.get("source_type") or p.get("path") or "")


class ApplianceCommand(Base):
    __tablename__ = "appliance_commands"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    appliance_id = Column(String, ForeignKey("appliances.id"), nullable=False, index=True)
    command_type = Column(String, nullable=False)
    sequence = Column(Integer, nullable=False)
    envelope = Column(JSON, nullable=False)  # signed command {payload, signature}
    status = Column(String, default="pending")  # pending | delivered | acked | rejected | expired
    requested_by = Column(String, nullable=False)
    result = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=_now)


class SnapshotReceipt(Base):
    """Signed seal receipt returned by an appliance (spec 6.1 step 11)."""

    __tablename__ = "snapshot_receipts"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    appliance_id = Column(String, ForeignKey("appliances.id"), nullable=True, index=True)
    vault_id = Column(String, ForeignKey("vaults.id"), nullable=False, index=True)
    collection_id = Column(String, ForeignKey("collections.id"), nullable=False, index=True)
    snapshot_id = Column(String, nullable=False, index=True)
    destination = Column(String, nullable=False)  # cv-cloud | appliance | customer-s3
    object_count = Column(Integer, default=0)
    total_bytes = Column(BigInteger, default=0)
    manifest_hash = Column(String, nullable=False)
    recoverable = Column(Boolean, default=False)  # spec 6.1 step 12 / build-instr 18
    receipt = Column(JSON, nullable=True)  # signed seal receipt
    created_at = Column(DateTime, default=_now)


class SearchDocument(Base):
    """Denormalised, tenant-scoped index entry powering unified search across
    all data types, accounts, and objects within a user account."""

    __tablename__ = "search_documents"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    vault_id = Column(String, index=True)
    collection_id = Column(String, index=True)
    snapshot_id = Column(String, index=True)
    object_id = Column(String, index=True)
    source_type = Column(String, index=True)  # gmail | onepassword | dropbox | ...
    doc_type = Column(String, index=True)  # canonical kind (email | pdf | login | ...)
    category = Column(String, index=True)  # canonical category (message | document | ...)
    title = Column(String, nullable=False)
    # Searchable metadata only. Content stays encrypted; this is derived,
    # policy-permitted preview text (empty for zero-knowledge vaults).
    preview = Column(Text, default="")
    meta = Column(JSON, default=dict)
    labels = Column(JSON, default=list)  # tags / folders for faceting
    # Denormalised searchable text (title + preview + connector-declared
    # searchable metadata fields). Empty for zero-knowledge vaults.
    search_blob = Column(Text, default="")
    size_bytes = Column(BigInteger, default=0)
    modified_at = Column(DateTime, nullable=True)
    # Content-addressed versioning: sha256 of the object's plaintext content and
    # which version this index row represents.
    content_hash = Column(String, index=True)
    version = Column(Integer, default=1)
    created_at = Column(DateTime, default=_now)


class ObjectVersion(Base):
    """One immutable version of a logical object (identified by source_type +
    object_id) across all snapshots. A new version is recorded only when the
    content hash changes; identical re-collections are de-duplicated. This gives
    every source a tamper-evident history of changes, deletions, and reversions."""

    __tablename__ = "object_versions"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    source_type = Column(String, index=True)
    object_id = Column(String, index=True)
    collection_id = Column(String, index=True)
    version = Column(Integer, default=1)
    content_hash = Column(String, index=True)  # sha256 of plaintext content
    snapshot_id = Column(String, index=True)   # snapshot holding this version's bytes
    size_bytes = Column(BigInteger, default=0)
    is_current = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=_now)


class RestoreRequest(Base):
    __tablename__ = "restore_requests"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    requested_by = Column(String, ForeignKey("users.id"), nullable=False)
    snapshot_id = Column(String, nullable=False)
    object_ids = Column(JSON, default=list)
    destination = Column(String, nullable=False)
    purpose = Column(String, default="")
    status = Column(String, default="pending-approval")
    approvals = Column(JSON, default=list)
    required_approvals = Column(Integer, default=1)
    plan = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=_now)


class SoftwareRelease(Base):
    """Signed software release for cloud-triggered updates (spec 11)."""

    __tablename__ = "software_releases"
    id = Column(String, primary_key=True, default=_uuid)
    component = Column(String, nullable=False)  # cloud | appliance
    version = Column(String, nullable=False)
    channel = Column(String, default="stable")
    package_url = Column(String, nullable=False)
    package_hash = Column(String, nullable=False)
    security_floor = Column(String, default="0.0.0")
    manifest = Column(JSON, nullable=False)  # signed update manifest
    created_at = Column(DateTime, default=_now)


class UpdateJob(Base):
    __tablename__ = "update_jobs"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, nullable=True, index=True)
    target_type = Column(String, nullable=False)  # cloud | appliance
    target_id = Column(String, nullable=True)  # appliance id
    release_id = Column(String, ForeignKey("software_releases.id"), nullable=False)
    status = Column(String, default="scheduled")  # scheduled | staged | applying | applied | rolled-back | failed
    approval_mode = Column(String, default="maintenance-window")
    created_at = Column(DateTime, default=_now)


class SyncJob(Base):
    """A tracked, long-running backup/sync run so the UI can show live progress."""

    __tablename__ = "sync_jobs"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    collection_id = Column(String, index=True)
    kind = Column(String, default="backup")  # backup | sync
    status = Column(String, default="queued")  # queued | running | done | failed
    processed = Column(Integer, default=0)
    total = Column(Integer, default=0)
    message = Column(String, default="")
    error = Column(String, default="")
    snapshot_id = Column(String, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now)


class RecoveredItem(Base):
    """A decrypted item brought out of storage for a time-limited viewing window.

    The plaintext lives in a temporary store and is automatically destroyed at
    ``expires_at`` (or on manual destroy) — a recovery window, not a copy."""

    __tablename__ = "recovered_items"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    object_id = Column(String, index=True)
    snapshot_id = Column(String)
    title = Column(String, default="")
    doc_type = Column(String, default="")
    source_type = Column(String, default="")
    mime = Column(String, default="application/octet-stream")
    size_bytes = Column(BigInteger, default=0)
    location = Column(String, default="")
    version = Column(Integer, nullable=True)  # which stored version was recovered
    version_created_at = Column(DateTime, nullable=True)  # when that version was captured
    requested_by = Column(String, default="")
    created_at = Column(DateTime, default=_now)
    expires_at = Column(DateTime, nullable=False)
    viewed_at = Column(DateTime, nullable=True)
    destroyed = Column(Boolean, default=False)


class AuditEvent(Base):
    """Tamper-evident audit ledger (append-only, hash-chained)."""

    __tablename__ = "audit_events"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, index=True, nullable=True)
    actor = Column(String, nullable=False)
    action = Column(String, nullable=False)
    resource = Column(String, default="")
    detail = Column(JSON, default=dict)
    severity = Column(String, default="info")     # info | notice | warning | critical
    category = Column(String, default="activity")  # activity | security | credential | admin | system
    prev_hash = Column(String, default="")
    entry_hash = Column(String, default="")
    created_at = Column(DateTime, default=_now)


class PricingConfig(Base):
    """Platform-wide pricing + data-value model, editable by platform admins and
    read by the customer Protection Setup / billing page. Single row (id="default")."""

    __tablename__ = "pricing_config"
    id = Column(String, primary_key=True, default="default")
    currency = Column(String, default="USD")
    # Recurring, per TB / month.
    protection_price_per_tb_month = Column(Float, default=6.0)   # legacy flat rate / fallback
    cloud_price_per_tb_month = Column(Float, default=10.0)       # Arkive Cloud storage
    s3_price_per_tb_month = Column(Float, default=23.0)          # AWS S3 Standard estimate
    azure_price_per_tb_month = Column(Float, default=18.0)       # Azure Blob Hot estimate
    # License tiers for recurring data-protection pricing. Each tenant is on one
    # tier (Tenant.plan == tier id). [{id, name, price_per_tb_month, min_tb}].
    license_plans = Column(JSON, default=list)
    # One-time appliance device pricing: [{capacity_tb, price, model}].
    appliance_tiers = Column(JSON, default=list)
    # Estimated value per protected object, keyed by object bucket.
    data_value_per_type = Column(JSON, default=dict)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class EmailConfig(Base):
    """Platform-wide outbound email settings (AWS SES), editable by platform
    admins. Credentials come from the platform AWS identity (env/IAM) — only the
    non-secret routing config lives here. Single row (id="default")."""

    __tablename__ = "email_config"
    id = Column(String, primary_key=True, default="default")
    provider = Column(String, default="ses")   # ses | smtp | log
    enabled = Column(Boolean, default=False)
    from_email = Column(String, default="notifications@arkive.life")
    from_name = Column(String, default="Arkive")
    reply_to = Column(String, default="support@arkive.life")
    region = Column(String, default="us-east-1")
    aws_access_key_id = Column(String, default="")        # SES access key id
    aws_secret_encrypted = Column(String, default="")     # SES secret, encrypted at rest
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class Node(Base):
    """A platform node in the (multi-node) control/storage fleet. The running
    instance registers itself as ``is_self`` and reports live health; additional
    nodes are registered for centralized visibility and management as the
    platform scales horizontally."""

    __tablename__ = "nodes"
    id = Column(String, primary_key=True, default=_uuid)
    name = Column(String, default="node")
    region = Column(String, default="")
    role = Column(String, default="control-plane")  # control-plane | customer-tenant | public-web | storage | worker | edge
    endpoint = Column(String, default="")            # base URL / address
    status = Column(String, default="active")        # active | draining | offline | maintenance
    is_self = Column(Boolean, default=False)         # the running instance
    version = Column(String, default="")
    telemetry = Column(JSON, default=dict)           # cpu/mem/disk/storage snapshot
    cloud = Column(JSON, default=dict)               # detected provider/region/instance (IMDS)
    # Which platform service objects this node uses (one of each). The running
    # node's selections drive where cloud objects are stored and how mail sends.
    storage_service_id = Column(String, nullable=True)  # ServiceObject (storage-*)
    email_service_id = Column(String, nullable=True)    # ServiceObject (email-*)
    last_heartbeat_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now)


class NodeBlueprint(Base):
    """Per-role configuration + update target managed by platform admins. Nodes
    heartbeat to the control plane and receive their role's blueprint so config,
    settings, and the target software version are centrally controlled."""

    __tablename__ = "node_blueprints"
    role = Column(String, primary_key=True)  # control-plane | customer-tenant | public-web | ...
    target_version = Column(String, default="")
    config = Column(JSON, default=dict)      # role-specific config the node applies
    settings = Column(JSON, default=dict)    # feature flags / general settings
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class SiteContent(Base):
    """Editable content for the public marketing website (Public Web Node),
    managed via the Control Plane admin CMS. Single row (id="default"); the
    public site fetches it and falls back to bundled defaults."""

    __tablename__ = "site_content"
    id = Column(String, primary_key=True, default="default")
    content = Column(JSON, default=dict)  # structured page/section content
    published = Column(Boolean, default=True)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class ConfigObject(Base):
    """A named, encrypted key-value credential/config bundle managed by platform
    admins (OAuth client id/secret, API keys, SES creds…). Linked to platform
    sources via SourceConfig. Values are stored encrypted (credstore, 'platform')."""

    __tablename__ = "config_objects"
    id = Column(String, primary_key=True, default=_uuid)
    name = Column(String, nullable=False)
    kind = Column(String, default="generic")  # oauth | api-key | ses | generic
    encrypted_values = Column(Text, default="")
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class SourceConfig(Base):
    """Per-integration platform setting: whether a source is enabled and which
    ConfigObject supplies its credentials (one row per connector type / 'ses')."""

    __tablename__ = "source_configs"
    connector_type = Column(String, primary_key=True)
    enabled = Column(Boolean, default=True)
    config_object_id = Column(String, nullable=True)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class PendingAction(Base):
    """A task surfaced to the customer that needs a manual step — e.g. pick new
    Google Photos to back up (the Picker API requires a human selection). Created
    on a cadence by the scheduler; auto-resolved or dismissed when handled."""

    __tablename__ = "pending_actions"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    kind = Column(String, default="photos_pick")
    collection_id = Column(String, nullable=True, index=True)
    source_type = Column(String, default="")
    title = Column(String, default="")
    message = Column(String, default="")
    status = Column(String, default="open")  # open | done | dismissed
    due_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class ServiceObject(Base):
    """A named platform service instance configured on the Service Objects admin
    page: a storage backend (Amazon S3 / Azure Blob) or an email sender (SES).

    Credentials come from a linked ``ConfigObject``; non-secret routing (bucket,
    region, container, from address, storage tier…) lives in ``settings``. Each
    Node selects one storage service and one email service; the running node's
    selections determine where cloud objects are stored and how mail is sent."""

    __tablename__ = "service_objects"
    id = Column(String, primary_key=True, default=_uuid)
    name = Column(String, nullable=False)
    kind = Column(String, nullable=False)  # email-ses | storage-s3 | storage-azure
    enabled = Column(Boolean, default=True)
    config_object_id = Column(String, nullable=True)  # linked credentials
    settings = Column(JSON, default=dict)             # non-secret routing
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

