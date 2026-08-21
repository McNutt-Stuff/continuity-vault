"""Cloud control-plane configuration."""

from __future__ import annotations

import os
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CV_", env_file=".env", extra="ignore")

    environment: str = "development"
    domain: str = "vault.arkive.life"
    api_base_url: str = "https://vault.arkive.life/api"

    # Database. Postgres in production, SQLite for local prototype runs.
    database_url: str = "sqlite:///./continuity_cloud.db"

    # Session / control-plane signing.
    session_secret: str = "dev-insecure-change-me"
    session_ttl_seconds: int = 3600

    # WebAuthn / passkey relying party.
    rp_id: str = "vault.arkive.life"
    rp_name: str = "Arkive"
    rp_origin: str = "https://vault.arkive.life"

    # Object storage (Model A / customer S3).
    s3_endpoint_url: str | None = None  # set for MinIO dev
    s3_region: str = "us-east-1"
    s3_bucket: str = "continuity-vault-recovery"
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None

    # Appliance fleet management.
    command_ttl_seconds: int = 900
    heartbeat_interval_seconds: int = 30
    # Long enough to survive a first appliance install (native liboqs build).
    linking_code_ttl_seconds: int = 3600

    # Software update publishing.
    release_channel: str = "stable"

    # Scheduled connector sync (background delta worker).
    sync_enabled: bool = True
    sync_interval_minutes: int = 30
    # Verbose per-source sync logging (cv.sync / cv.scheduler at DEBUG).
    sync_debug: bool = False
    # How often the scheduler wakes to check each mapping's per-mapping cadence.
    scheduler_tick_seconds: int = 60
    # Safety cap on how many items a single full backup pulls from a source.
    sync_max_items: int = 5000
    # Max raw content pulled/held per object during a sync (memory bound). Larger
    # items are indexed metadata-only.
    content_max_bytes: int = 268435456  # 256 MiB
    # Content larger than this is split into encrypted chunks at rest.
    content_chunk_bytes: int = 8388608  # 8 MiB
    # How long a recovered (decrypted) item stays viewable before auto-destroy.
    recovered_ttl_seconds: int = 1800  # 30 min

    # Authentication.
    allow_signup: bool = True
    email_code_ttl_seconds: int = 600
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str = "no-reply@arkive.life"
    smtp_starttls: bool = True

    # Connector OAuth apps. Set the client id/secret for each provider to enable
    # real linking; a provider is shown as "needs setup" until configured.
    oauth_redirect_base: str | None = None  # default: https://{domain}/api
    google_client_id: str | None = None
    google_client_secret: str | None = None
    microsoft_client_id: str | None = None
    microsoft_client_secret: str | None = None
    microsoft_tenant: str = "common"
    dropbox_client_id: str | None = None
    dropbox_client_secret: str | None = None
    # Google Contacts/Calendar reuse the Google client above. Social sources:
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    facebook_client_id: str | None = None
    facebook_client_secret: str | None = None
    instagram_client_id: str | None = None
    instagram_client_secret: str | None = None
    linkedin_client_id: str | None = None
    linkedin_client_secret: str | None = None
    # LinkedIn scopes must match the products enabled on your LinkedIn app. The
    # "Sign In with LinkedIn using OpenID Connect" product grants these; legacy
    # apps may need "r_liteprofile r_emailaddress". Override via CV_LINKEDIN_SCOPES.
    linkedin_scopes: str = "openid profile email"
    # Evernote's modern API is its MCP server (OAuth 2.0). No app registration is
    # offered — the client is registered dynamically (RFC 7591) and cached. Set
    # a static client only if Evernote later provides one.
    evernote_mcp_url: str = "https://mcp.evernote.com/mcp"
    evernote_client_id: str | None = None
    evernote_client_secret: str | None = None

    seed_demo_data: bool = True

    # Multi-node fleet. Non-control-plane nodes (customer-tenant, public-web)
    # heartbeat to the control plane using a shared node secret and receive their
    # role blueprint (config + target version). Set on every node.
    node_role: str = "control-plane"      # control-plane | customer-tenant | public-web
    node_name: str = ""                    # defaults to the domain
    node_secret: str | None = None         # shared secret for node heartbeat auth
    control_plane_url: str | None = None   # base URL of the control plane (for non-CP nodes)
    site_content_path: str = ""            # public-web: where to mirror CMS content (site.json)
    # When true, this instance only runs sync/backup work for the tenants assigned
    # to its node (Tenant.node_id). The control plane then handles only unassigned
    # tenants. REQUIRES every node to share the control-plane database so each node
    # sees the same tenants/sources and writes to the same search index.
    node_sync_scope: bool = False          # CV_NODE_SYNC_SCOPE


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
