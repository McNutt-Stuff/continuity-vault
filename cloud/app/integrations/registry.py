"""Deprecated: integrations are now self-contained packages.

Each integration lives in its own sub-package under ``cloud/app/integrations/``
(e.g. ``ubiquiti/``, ``microsoft365/``) and self-registers via auto-discovery in
``integrations/__init__``. This module is retained only for backward-compatible
imports and intentionally registers nothing.
"""

from __future__ import annotations
