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
    # Tenant model determines UX + isolation posture:
    #   shared      — personal accounts pooled in one tenant; each user is 1:1,
    #                  no organization view/settings.
    #   dedicated   — family/business tenant with full organization management.
    #   restricted  — high-value/enterprise tenant with elevated security (placeholder).
    #   internal    — Arkive Operations; where employee + platform-admin accounts live.
    tenant_type = Column(String, default="dedicated")
    # The customer-node that processes this tenant's workers, indexing, sync,
    # appliance/agent channels and storage. NULL = processed on the control plane
    # itself (single-box default). Assigning a node offloads tenant processing.
    node_id = Column(String, ForeignKey("nodes.id"), nullable=True, index=True)
    status = Column(String, default="active")
    # Admin-controlled capability flags (e.g. purge_enabled=False for a legal hold
    # / subpoena). Org tenants set them tenant-wide; personal accounts use user-level.
    feature_flags = Column(JSON, default=dict)
    # When set, the tenant unsubscribed from Arkive Cloud; its cloud-stored data is
    # scheduled for permanent deletion at this time (30-day grace). Cleared on re-subscribe.
    cloud_delete_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now)

    users = relationship("User", back_populates="tenant", cascade="all, delete-orphan")
    vaults = relationship("Vault", back_populates="tenant", cascade="all, delete-orphan")


class User(Base):
    __tablename__ = "users"
    # Email is globally unique across the whole platform — one account per address.
    __table_args__ = (UniqueConstraint("email", name="uq_user_email"),)
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    email = Column(String, nullable=False)
    display_name = Column(String, nullable=False)
    first_name = Column(String, default="")
    last_name = Column(String, default="")
    phone = Column(String, default="")
    role = Column(String, default="member")  # owner | security-admin | member | support-admin
    is_platform_admin = Column(Boolean, default=False)  # backend admin console
    email_verified = Column(Boolean, default=False)
    status = Column(String, default="active")
    feature_flags = Column(JSON, default=dict)  # per-user capability flags (admin-set)
    # Shared-tenant personal accounts own their protection destinations here (the
    # tenant is a pool of unrelated accounts); org tenants use Tenant.protection_options.
    protection_options = Column(JSON, default=list)
    # Shared/personal account's pending Arkive Cloud deletion (30-day grace, see Tenant).
    cloud_delete_at = Column(DateTime, nullable=True)
    last_login_at = Column(DateTime, nullable=True)
    # NULL until the account finishes (or an admin re-triggers) the setup wizard.
    setup_completed_at = Column(DateTime, nullable=True)
    # Per-type email notification preferences {type_key: bool}. Missing key = the
    # type's default. Managed by the user (account settings) and admins.
    notification_prefs = Column(JSON, default=dict)
    # Additional addresses that also receive this account's email notifications
    # (never used for login; may duplicate another account's login email).
    notification_emails = Column(JSON, default=list)
    # Opt-in: build a contact directory so messages that only carry a phone/email
    # can show the linked contact name (see ContactLink).
    contact_linking_enabled = Column(Boolean, default=False)
    created_at = Column(DateTime, default=_now)

    tenant = relationship("Tenant", back_populates="users")
    passkeys = relationship("Passkey", back_populates="user", cascade="all, delete-orphan")

    @property
    def full_name(self) -> str:
        n = " ".join(p for p in [self.first_name, self.last_name] if p).strip()
        return n or self.display_name or self.email


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
    # The provider identity used when linking (e.g. rob@outlook.com) — immutable and
    # always shown alongside the (editable) account_label so a source stays identifiable.
    account_username = Column(String, nullable=True)
    auth_status = Column(String, default="linked")  # linked | needs-reauth | revoked
    # Once data is ingested a source is never deleted (it identifies that data);
    # "removing" deactivates it (active=False) — sync stops, data kept, can be
    # re-linked or purged. Purge is the only true removal.
    active = Column(Boolean, default=True)
    # Encrypted credential blob (never plaintext at rest).
    encrypted_credentials = Column(Text, nullable=True)
    scopes = Column(JSON, default=list)
    sync_cursor = Column(JSON, nullable=True)  # incremental sync state (e.g. Gmail historyId)
    last_sync_at = Column(DateTime, nullable=True)
    last_object_count = Column(Integer, nullable=True)  # items captured in the last sync
    last_error = Column(Text, nullable=True)            # last sync error message (NULL = healthy)
    last_error_at = Column(DateTime, nullable=True)
    # Consecutive failed syncs (reset to 0 on success). Drives source-problem
    # escalation (>5 failures within the schedule) and notifications.
    fail_count = Column(Integer, default=0)
    # Dual-track deep crawl: independent of the forward/delta sync_cursor, a
    # long-running BACKWARD crawl captures full history in resumable chunks while
    # the scheduled "recent" delta track keeps up with new items concurrently.
    backfill_cursor = Column(JSON, nullable=True)
    backfill_done = Column(Boolean, default=False)
    backfill_started_at = Column(DateTime, nullable=True)
    backfill_completed_at = Column(DateTime, nullable=True)  # last full-history pass finished
    backfill_count = Column(Integer, default=0)  # items captured by the deep crawl
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
    version_updated_at = Column(DateTime, nullable=True)  # when software_version last changed
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
    # Storage service objects this appliance replicates its sealed data to for
    # off-site resiliency (multiple, distinct destinations).
    backup_service_ids = Column(JSON, default=list)
    # The single configuration profile (kind='appliance') assigned to this appliance.
    config_profile_id = Column(String, nullable=True)
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


class PendingAppliance(Base):
    """A zero-touch appliance that registered with the control plane WITHOUT a
    linking code. It displays a pairing code on its local web UI until a customer
    claims it; on pairing a real Appliance is created and the agent adopts the
    returned activation payload."""

    __tablename__ = "pending_appliances"
    id = Column(String, primary_key=True, default=_uuid)
    serial = Column(String, nullable=False, unique=True, index=True)
    model = Column(String, default="CV Edge 8")
    pairing_code = Column(String, unique=True, index=True)
    token_hash = Column(String, index=True)          # sha256 of the registration token
    identity_bundle = Column(JSON, nullable=True)
    attestation = Column(JSON, nullable=True)
    telemetry = Column(JSON, default=dict)
    paired_appliance_id = Column(String, nullable=True)  # set when claimed
    paired_tenant_id = Column(String, nullable=True)
    last_seen_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now, index=True)


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
    version_updated_at = Column(DateTime, nullable=True)  # when version last changed
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
    # Exactly one current row per (tenant, source_type, object_id): set False on
    # prior rows when a new version is indexed, so reads filter to the current row
    # instead of de-duplicating the whole index. Backfilled for legacy rows.
    is_current = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=_now)


class ContactLink(Base):
    """Derived contact directory: links a NORMALIZED identifier (phone number or
    email address) to a contact display name gathered from the user's contact
    sources, so messages that only carry a raw number/address can display the
    person's name. Rebuilt periodically by a node scheduler; opt-in per user."""

    __tablename__ = "contact_links"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, index=True, nullable=False)
    owner_user_id = Column(String, index=True, nullable=True)
    identifier_type = Column(String, index=True)   # phone | email
    identifier = Column(String, index=True)        # normalized match key
    display_name = Column(String, default="")
    source_type = Column(String, default="")       # google_contacts | icloud | ...
    source_object_id = Column(String, default="")
    updated_at = Column(DateTime, default=_now, index=True)
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
    trigger = Column(String, default="manual")  # manual | schedule
    node_id = Column(String, index=True, nullable=True)  # node that should execute it (per-node scoping)
    status = Column(String, default="queued")  # queued | running | done | failed
    processed = Column(Integer, default=0)
    total = Column(Integer, default=0)
    message = Column(String, default="")
    error = Column(String, default="")
    # Verbose per-run process log (list of {ts, level, msg}) for success + failure,
    # shipped node -> control plane with the rest of the job telemetry.
    log = Column(JSON, default=list)
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
    object_modified_at = Column(DateTime, nullable=True)  # the object's own native timestamp
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
    created_at = Column(DateTime, default=_now, index=True)


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
    version_updated_at = Column(DateTime, nullable=True)  # when version last changed
    telemetry = Column(JSON, default=dict)           # cpu/mem/disk/storage snapshot
    cloud = Column(JSON, default=dict)               # detected provider/region/instance (IMDS)
    # Which platform service objects this node uses (one of each). The running
    # node's selections drive where cloud objects are stored and how mail sends.
    storage_service_id = Column(String, nullable=True)  # ServiceObject (storage-*)
    email_service_id = Column(String, nullable=True)    # ServiceObject (email-*)
    # Per-node setting overrides — HIGHEST precedence (override > config profile >
    # local/env default). Delivered to remote nodes on heartbeat.
    config_overrides = Column(JSON, default=dict)
    # The single configuration profile (kind='node') assigned to this node.
    config_profile_id = Column(String, nullable=True)
    # Storage service objects this node backs its own core state up to. A list so
    # a node can replicate its infrastructure backup to MULTIPLE destinations for
    # resiliency (use different services, not the same one twice).
    backup_service_ids = Column(JSON, default=list)
    last_heartbeat_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now)


class BackupRun(Base):
    """One infrastructure-backup attempt of a node's (or the control plane's) core
    state — database, search index, config and key material — to its assigned
    backup storage service objects. The backup worker records one per run; nodes
    push theirs to the control plane so the admin Backups page shows fleet-wide
    success/failure, size and coverage."""

    __tablename__ = "backup_runs"
    id = Column(String, primary_key=True, default=_uuid)
    node_id = Column(String, index=True, nullable=True)
    node_name = Column(String, default="")
    role = Column(String, default="")
    kind = Column(String, default="node")     # node | appliance
    status = Column(String, default="running")  # running | success | partial | failed | skipped
    components = Column(JSON, default=list)   # ["database","keystore","config"]
    destinations = Column(JSON, default=list)  # [{service_id,name,kind,status,bytes,key,error}]
    total_bytes = Column(BigInteger, default=0)
    message = Column(String, default="")
    error = Column(Text, default="")
    log = Column(JSON, default=list)  # verbose per-run process log [{ts,level,msg}]
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now, index=True)


class SystemSetting(Base):
    """Tiny key/value store for platform-wide runtime settings (e.g. the debug-API
    key). Auto-created by create_all; no migration needed."""

    __tablename__ = "system_settings"
    key = Column(String, primary_key=True)
    value = Column(Text, default="")
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class NotificationLog(Base):
    """One row per notification email sent, used to (a) rate-limit recurring
    notifications (daily summary once/day, source-problem repeat once/day) via a
    dedupe key, and (b) give the admin an audit of what went out."""

    __tablename__ = "notification_log"
    id = Column(String, primary_key=True, default=_uuid)
    user_id = Column(String, index=True, nullable=True)
    tenant_id = Column(String, index=True, nullable=True)
    type = Column(String, index=True, nullable=False)   # notification type key
    dedupe_key = Column(String, index=True, default="")  # e.g. "daily:2026-08-28"
    channel = Column(String, default="email")
    subject = Column(String, default="")
    to_email = Column(String, default="")
    ok = Column(Boolean, default=True)
    created_at = Column(DateTime, default=_now, index=True)



class Communication(Base):
    """Every outbound email to a user/address, captured globally at the email
    service (emailer.send) so the admin sees a full per-account communications
    history: subject, rendered body, delivery status/provider, which node sent it,
    and whether the recipient opened it (1x1 tracking pixel). On a federated node
    the row is written locally and pushed to the control plane by replication; the
    open pixel always points at the control plane, which owns the open fields."""

    __tablename__ = "communications"
    id = Column(String, primary_key=True, default=_uuid)  # also the open-tracking token
    user_id = Column(String, index=True, nullable=True)
    tenant_id = Column(String, index=True, nullable=True)
    to_email = Column(String, index=True, default="")
    # signin | notification:<type> | broadcast | welcome | access | email
    category = Column(String, index=True, default="email")
    subject = Column(String, default="")
    body_html = Column(Text, default="")
    body_text = Column(Text, default="")
    channel = Column(String, default="")      # ses | smtp | log | error
    status = Column(String, index=True, default="sent")  # sent | failed | logged
    provider = Column(String, default="")
    error = Column(Text, default="")
    node_name = Column(String, default="")    # which node sent it
    opened_at = Column(DateTime, nullable=True)
    open_count = Column(Integer, default=0)
    last_opened_ip = Column(String, default="")
    created_at = Column(DateTime, default=_now, index=True)



class NodeMetric(Base):
    """Time-series health sample for a node (~1/min), retained 90 days on the
    control plane so the admin can render CPU/memory/disk/network trend lines."""

    __tablename__ = "node_metrics"
    id = Column(String, primary_key=True, default=_uuid)
    node_id = Column(String, index=True, nullable=False)
    ts = Column(DateTime, default=_now, index=True)
    cpu_pct = Column(Float, default=0)
    mem_pct = Column(Float, default=0)
    disk_pct = Column(Float, default=0)
    mem_used = Column(BigInteger, default=0)
    mem_total = Column(BigInteger, default=0)
    disk_used = Column(BigInteger, default=0)
    disk_total = Column(BigInteger, default=0)
    net_sent_rate = Column(BigInteger, default=0)
    net_recv_rate = Column(BigInteger, default=0)
    load1 = Column(Float, default=0)


class ConfigProfile(Base):
    """A named, reusable set of settings (key→value) that platform admins bind to
    specific nodes. The profiles bound to a node are merged (in name order) to form
    the node's effective settings, which reconfigure its running behavior — resolved
    live on the control plane and delivered to remote nodes on heartbeat. The
    settings catalog (config_catalog.py) drives the editor's autocomplete/examples
    and validation."""

    __tablename__ = "config_profiles"
    id = Column(String, primary_key=True, default=_uuid)
    name = Column(String, nullable=False, unique=True)
    description = Column(Text, default="")
    kind = Column(String, default="node-settings")  # extensible for future setting groups
    data = Column(JSON, default=dict)               # key -> typed value
    node_ids = Column(JSON, default=list)           # nodes this profile applies to
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=_now)
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


class SupportDoc(Base):
    """A single wiki/documentation page for the public support site. Managed via
    the Control Plane admin CMS and mirrored to the Public Web Node (like
    SiteContent). Pages are organized into sections and ordered for the nav."""

    __tablename__ = "support_docs"
    id = Column(String, primary_key=True, default=_uuid)
    slug = Column(String, nullable=False, unique=True, index=True)  # url key, e.g. "getting-started"
    title = Column(String, nullable=False)
    section = Column(String, default="General")   # nav group heading
    section_order = Column(Integer, default=100)  # order of the section in the nav
    nav_order = Column(Integer, default=100)      # order of this page within its section
    icon = Column(String, default="book")         # frontend Icon name for the nav
    summary = Column(Text, default="")            # one-line description (search/cards)
    body = Column(Text, default="")               # Markdown content
    # Portal routes this doc is the contextual help for (e.g. ["/search","/recover"]),
    # so the portal Help icon can deep-link to the right page.
    help_routes = Column(JSON, default=list)
    published = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class SupportSection(Base):
    """A documentation section (nav group). First-class so sections can be
    created, renamed, reordered and left empty independent of the docs in them.
    Docs reference a section by its ``name``."""

    __tablename__ = "support_sections"
    id = Column(String, primary_key=True, default=_uuid)
    name = Column(String, nullable=False, unique=True)
    order = Column(Integer, default=100)
    icon = Column(String, default="book")
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class SupportTicket(Base):
    """A customer support request. Lives on the control plane (auth-protected).
    Threaded messages are stored in TicketMessage."""

    __tablename__ = "support_tickets"
    id = Column(String, primary_key=True, default=_uuid)
    ref = Column(String, unique=True, index=True)  # short human reference, e.g. "ARK-4F2A"
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    subject = Column(String, nullable=False)
    category = Column(String, default="other")   # billing | technical | feature_request | account | other
    priority = Column(String, default="normal")  # low | normal | high | urgent
    status = Column(String, default="open", index=True)  # open | pending | resolved | closed
    # Denormalized for the admin list without a user join.
    requester_email = Column(String, default="")
    requester_name = Column(String, default="")
    assignee_user_id = Column(String, nullable=True)
    last_activity_at = Column(DateTime, default=_now, index=True)
    created_at = Column(DateTime, default=_now)

    messages = relationship("TicketMessage", back_populates="ticket",
                            cascade="all, delete-orphan", order_by="TicketMessage.created_at")


class TicketMessage(Base):
    """One message in a support ticket thread (customer or staff)."""

    __tablename__ = "ticket_messages"
    id = Column(String, primary_key=True, default=_uuid)
    ticket_id = Column(String, ForeignKey("support_tickets.id"), nullable=False, index=True)
    author_user_id = Column(String, nullable=True)  # NULL = system note
    author_name = Column(String, default="")
    is_staff = Column(Boolean, default=False)  # posted by support/admin
    body = Column(Text, default="")
    created_at = Column(DateTime, default=_now)

    ticket = relationship("SupportTicket", back_populates="messages")


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
    family = Column(String, nullable=True)  # admin override of the default source family grouping
    # Opt-in deep-history backfill for this source (only meaningful for connectors
    # whose capabilities advertise dual_track). Off by default.
    backfill_enabled = Column(Boolean, default=False)
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


STORAGE_CAPABILITIES = ("cloud", "backup")  # what a storage service may be "used for"


class ServiceObject(Base):
    """A named platform service instance configured on the Service Objects admin
    page: a storage backend (Amazon S3 / Azure Blob) or an email sender (SES).

    Credentials come from a linked ``ConfigObject``; non-secret routing (bucket,
    region, container, from address, storage tier…) lives in ``settings``. Each
    Node selects one storage service and one email service; the running node's
    selections determine where cloud objects are stored and how mail is sent.

    ``capabilities`` (storage only) records what a backend may be *used for* —
    ``cloud`` (Arkive Cloud object storage) and/or ``backup`` (infrastructure
    backups). Empty/unset means both, so existing services keep working."""

    __tablename__ = "service_objects"
    id = Column(String, primary_key=True, default=_uuid)
    name = Column(String, nullable=False)
    kind = Column(String, nullable=False)  # email-ses | storage-s3 | storage-azure
    enabled = Column(Boolean, default=True)
    config_object_id = Column(String, nullable=True)  # linked credentials
    settings = Column(JSON, default=dict)             # non-secret routing
    capabilities = Column(JSON, default=list)         # storage "used for": cloud | backup
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    def storage_capabilities(self) -> list[str]:
        """Normalized 'used for' list for a storage service (defaults to both
        when unset). Empty for non-storage services."""
        if not (self.kind or "").startswith("storage-"):
            return []
        caps = [c for c in (self.capabilities or []) if c in STORAGE_CAPABILITIES]
        return caps or list(STORAGE_CAPABILITIES)


class CustomerStorage(Base):
    """A customer's own cloud bucket/container used as a backup destination
    (bring-your-own-storage), the third protection tier alongside Arkive Cloud
    and on-prem appliances. Data is written already-encrypted (the same
    quantum-safe envelope as every other destination) — the provider only ever
    holds ciphertext.

    Credentials are split by capability: the WRITE credential lives server-side
    (encrypted at rest) so automated backups keep flowing unattended; the READ
    credential is only decrypted/used to serve a restore, which requires a
    passkey-verified session. Provider auto-provisioning (Scenario 2) drives the
    ``provision_*`` fields; a customer supplying existing details (Scenario 1)
    lands straight in ``provision_state='done'``."""

    __tablename__ = "customer_storages"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    owner_user_id = Column(String, nullable=True, index=True)
    name = Column(String, nullable=False)                 # user-facing label
    provider = Column(String, nullable=False)             # aws | azure | gcp
    config = Column(JSON, default=dict)                   # non-secret routing (bucket/region/prefix…)
    write_credentials = Column(Text, nullable=True)       # credstore-encrypted — automated backups
    read_credentials = Column(Text, nullable=True)        # credstore-encrypted — passkey-gated restores
    provision_mode = Column(String, default="existing")   # existing | provisioned
    provision_state = Column(String, default="done")      # done | starting | awaiting_otp | provisioning | error
    provision_message = Column(Text, nullable=True)
    status = Column(String, default="unknown")            # unknown | healthy | degraded | error
    used_bytes = Column(BigInteger, default=0)
    last_test_at = Column(DateTime, nullable=True)
    last_test_ok = Column(Boolean, default=False)
    last_test_error = Column(Text, nullable=True)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class UserInsights(Base):
    """Precomputed digital-footprint insights for one user, refreshed by a daily
    background job so the Insights page loads instantly. Holds the footprint
    timeline, the derived insight cards, and headline stats as ready-to-render
    JSON (no per-request mining of the index)."""

    __tablename__ = "user_insights"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, unique=True, index=True)
    status = Column(String, default="ready")  # ready | insufficient_data
    generated_at = Column(DateTime, default=_now)
    timeline = Column(JSON, default=dict)   # footprint-over-time series
    cards = Column(JSON, default=list)      # derived insight cards
    stats = Column(JSON, default=dict)      # headline summary numbers


class IntegrationConfig(Base):
    """Platform-wide admin setting for an integration type (mirrors SourceConfig):
    whether it is available to customers and its default configuration. One row
    per integration type (e.g. ``ubiquiti``)."""

    __tablename__ = "integration_configs"
    integration_type = Column(String, primary_key=True)
    enabled = Column(Boolean, default=True)
    config = Column(JSON, default=dict)  # platform defaults (interval overrides, etc.)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class IntegrationInstance(Base):
    """A customer's enabled integration. Unlike a source/connector, integrations
    gather *auxiliary intelligence* (network/app telemetry) rather than vaulted
    data. Runs on the tenant's appliance (LAN-local integrations) or a cloud node,
    per the integration's declared ``runs_on``."""

    __tablename__ = "integration_instances"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    owner_user_id = Column(String, ForeignKey("users.id"), nullable=True, index=True)
    integration_type = Column(String, nullable=False, index=True)
    label = Column(String, default="")
    enabled = Column(Boolean, default=True)
    runs_on = Column(String, default="appliance")  # appliance | cloud
    appliance_id = Column(String, nullable=True, index=True)  # where it runs (LAN integrations)
    node_id = Column(String, nullable=True)
    # Encrypted credentials (credstore blob) + non-secret config (host, interval).
    credentials = Column(Text, nullable=True)
    config = Column(JSON, default=dict)  # {host, poll_interval_minutes, ...}
    poll_interval_minutes = Column(Integer, default=60)
    status = Column(String, default="pending")  # pending | active | error | disabled
    # A user-requested immediate re-poll (cleared once the appliance reports back).
    repoll_requested = Column(Boolean, default=False)
    # Interactive setup (e.g. UniFi MFA): the appliance and portal coordinate an
    # OTP handshake through these fields until an API key is minted.
    provision_state = Column(String, default="idle")  # idle|starting|authenticating|awaiting_otp|verifying|done|error
    provision_message = Column(Text, nullable=True)
    provision_otp = Column(String, nullable=True)  # transient code the user submitted
    last_run_at = Column(DateTime, nullable=True)
    last_success_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    last_stats = Column(JSON, default=dict)  # {clients, apps, bytes} from the last run
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class NetworkClient(Base):
    """A device/client observed on the customer's network by an integration
    (e.g. a computer/phone seen by the UniFi controller)."""

    __tablename__ = "network_clients"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    integration_id = Column(String, index=True)
    client_key = Column(String, index=True)  # stable id (MAC) from the source
    name = Column(String, default="")
    hostname = Column(String, default="")
    ip = Column(String, default="")
    mac = Column(String, default="")
    is_wired = Column(Boolean, default=False)
    is_guest = Column(Boolean, default=False)
    device_type = Column(String, default="")  # inferred: computer | phone | iot | ...
    # normal | ignored (out of scope) | monitored (family system, keep building)
    monitor_state = Column(String, default="normal", index=True)
    of_interest = Column(Boolean, default=False)  # "Client of Interest"
    # User-set friendly name (overrides the source-reported name for display).
    nickname = Column(String, default="")
    # Whose device this is — drives per-user vs org/family insight scoping.
    # "" (unassigned) | personal (mine) | family | organization
    ownership = Column(String, default="", index=True)
    owner_user_id = Column(String, nullable=True, index=True)  # set when ownership=personal
    total_bytes = Column(BigInteger, default=0)
    tx_bytes = Column(BigInteger, default=0)
    rx_bytes = Column(BigInteger, default=0)
    first_seen = Column(DateTime, nullable=True)
    last_seen = Column(DateTime, nullable=True)
    meta = Column(JSON, default=dict)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class NetworkApp(Base):
    """An application / cloud service observed in network traffic, aggregated
    across the tenant (e.g. Gmail, Dropbox, Netflix), with mapped source_type when
    Arkive has a connector for it (drives shadow-source detection)."""

    __tablename__ = "network_apps"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    integration_id = Column(String, index=True)
    app_key = Column(String, index=True)  # stable id from the source (cat:app)
    name = Column(String, default="")
    category = Column(String, default="")
    source_type = Column(String, default="", index=True)  # mapped connector, if any
    total_bytes = Column(BigInteger, default=0)
    tx_bytes = Column(BigInteger, default=0)
    rx_bytes = Column(BigInteger, default=0)
    sessions = Column(Integer, default=0)
    client_count = Column(Integer, default=0)
    of_interest = Column(Boolean, default=False)  # "App of Interest"
    first_seen = Column(DateTime, nullable=True)
    last_seen = Column(DateTime, nullable=True)
    meta = Column(JSON, default=dict)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class NetworkUsage(Base):
    """A client×app usage edge for drill-downs: how much a given device used a
    given app (traffic volume, sessions, recency)."""

    __tablename__ = "network_usage"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    integration_id = Column(String, index=True)
    client_key = Column(String, index=True)
    app_key = Column(String, index=True)
    total_bytes = Column(BigInteger, default=0)
    tx_bytes = Column(BigInteger, default=0)
    rx_bytes = Column(BigInteger, default=0)
    sessions = Column(Integer, default=0)
    last_seen = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class IntegrationRun(Base):
    """One execution of an integration (for status history + monitoring)."""

    __tablename__ = "integration_runs"
    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    integration_id = Column(String, index=True)
    integration_type = Column(String, default="")
    appliance_id = Column(String, nullable=True)
    status = Column(String, default="ok")  # ok | error
    started_at = Column(DateTime, default=_now)
    finished_at = Column(DateTime, nullable=True)
    clients = Column(Integer, default=0)
    apps = Column(Integer, default=0)
    bytes_seen = Column(BigInteger, default=0)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_now)


