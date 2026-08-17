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
    linking_code_ttl_seconds: int = 900

    # Software update publishing.
    release_channel: str = "stable"

    seed_demo_data: bool = True


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
