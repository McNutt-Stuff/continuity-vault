"""Arkive desktop agent configuration."""

from __future__ import annotations

import os
from pathlib import Path


class Config:
    def __init__(self) -> None:
        self.cloud_base_url = os.environ.get(
            "ARKIVE_CLOUD_URL", "https://vault.arkive.life/api").rstrip("/")
        self.data_dir = Path(os.environ.get(
            "ARKIVE_AGENT_DIR", str(Path.home() / ".arkive-agent")))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.linking_code = os.environ.get("ARKIVE_LINKING_CODE", "")
        # Optional 1Password service account token for unattended collection.
        self.op_service_account_token = os.environ.get("OP_SERVICE_ACCOUNT_TOKEN", "")
        self.version = os.environ.get("ARKIVE_AGENT_VERSION", "1.0.0")
        self.home = os.environ.get("ARKIVE_AGENT_HOME", str(Path(__file__).resolve().parents[1]))

    @property
    def registration_file(self) -> Path:
        return self.data_dir / "registration.json"

    @property
    def status_file(self) -> Path:
        return self.data_dir / "status.json"
