"""Built-in signal providers. Importing a module registers its provider.

All providers REUSE data an existing collector already retrieved — never a second
vendor API call (collect-once principle, ADR 0001).
"""

from __future__ import annotations

from . import arkive  # noqa: F401 — Arkive-native protection/recovery/device health
from . import endpoint  # noqa: F401 — endpoint posture + application inventory
from . import m365  # noqa: F401 — Entra identity signals
from . import ubiquiti  # noqa: F401 — network client/application signals
