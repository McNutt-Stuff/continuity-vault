"""Appliance agent configuration."""

from __future__ import annotations

import os
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApplianceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CVA_", env_file="/etc/continuity-vault/appliance.env", extra="ignore")

    cloud_base_url: str = "http://localhost:8000/api"
    data_dir: str = "./cv_appliance_data"
    # Linking code from the turnkey activation ceremony (entered once).
    linking_code: str = ""
    model: str = "CV Edge 8"
    software_version: str = "1.0.0"
    heartbeat_interval_seconds: int = 30
    # Require physical/local approval before recovery unseal (spec 7.3).
    require_local_recovery_approval: bool = True


@lru_cache(maxsize=1)
def get_settings() -> ApplianceSettings:
    return ApplianceSettings()
