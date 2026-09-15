"""Arkive compliance engine — platform-level, integration-driven.

Importing this package registers the models (so ``create_all`` provisions the
tables) and the built-in evidence providers. Integration drivers register
themselves when their package is imported (auto-discovery).
"""

from __future__ import annotations

from . import models  # noqa: F401 — register tables
from . import providers  # noqa: F401 — registers the "arkive" core provider
from . import registry  # noqa: F401
from . import engine  # noqa: F401
