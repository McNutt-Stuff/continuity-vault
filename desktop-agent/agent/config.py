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
        # 1Password service-account token for unattended collection. Read from the
        # environment, else from a local 0600 file so it can be supplied without a
        # reinstall: echo 'ops_...' > ~/.arkive-agent/op_token
        self.op_service_account_token = self._read_op_token()
        self.version = self._read_version()
        self.home = os.environ.get("ARKIVE_AGENT_HOME", str(Path(__file__).resolve().parents[1]))

    def _read_op_token(self) -> str:
        tok = os.environ.get("OP_SERVICE_ACCOUNT_TOKEN", "").strip()
        if tok:
            return tok
        try:
            return (self.data_dir / "op_token").read_text().strip()
        except Exception:
            return ""

    @staticmethod
    def _read_version() -> str:
        env = os.environ.get("ARKIVE_AGENT_VERSION")
        if env:
            return env
        try:
            return (Path(__file__).resolve().parents[1] / "VERSION").read_text().strip()
        except Exception:
            return "1.0.0"

    @property
    def registration_file(self) -> Path:
        return self.data_dir / "registration.json"

    @property
    def status_file(self) -> Path:
        return self.data_dir / "status.json"

    @property
    def log_file(self) -> Path:
        return self.data_dir / "agent.log"
