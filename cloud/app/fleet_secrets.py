"""Fleet-shared secret adoption.

Every fleet member must share the SAME ``CV_KEK_SECRET`` (envelope + connector-
credential + vault-key encryption) and ``CV_SESSION_SECRET`` (session signing) so
data encrypted anywhere is usable everywhere. The control plane is authoritative;
a customer-tenant node ADOPTS the CP's values on its first pull and persists them
locally so they survive a restart.

Without this, the installer generates a per-node RANDOM ``CV_KEK_SECRET`` when the
env var is unset, so a credential encrypted under one KEK fails to decrypt on any
other node with ``InvalidTag`` — silently failing every backup after a tenant
moves nodes (HA switchover) or when a node was provisioned without the shared KEK.

``credstore`` / ``keybroker`` read ``os.environ["CV_KEK_SECRET"]`` directly, so
adoption just rewrites the process env (and persists it) — no call-site changes.
No-op on the control plane (its env value is authoritative).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .config import get_settings

logger = logging.getLogger("cv.fleet-secrets")


def _is_cp() -> bool:
    return (get_settings().node_role or "control-plane") == "control-plane"


def _path() -> Path:
    ks = Path(os.environ.get("CV_KEY_STORE", "./cv_keystore"))
    try:
        ks.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return ks / ".fleet_secrets.json"


def load_persisted() -> None:
    """Apply a previously-adopted fleet KEK/session to the running process BEFORE any
    crypto runs (called at startup) so it survives restarts. No-op on the CP."""
    if _is_cp():
        return
    try:
        d = json.loads(_path().read_text())
    except Exception:  # noqa: BLE001 — no adopted secrets yet
        return
    kek, sess = d.get("kek"), d.get("session")
    if kek:
        os.environ["CV_KEK_SECRET"] = kek
    if sess:
        try:
            from . import security
            security.set_session_secret(sess)
        except Exception:  # noqa: BLE001
            pass
    if kek or sess:
        logger.info("applied persisted fleet secrets (kek=%s session=%s)",
                    bool(kek), bool(sess))


def adopt(kek: str, session: str) -> bool:
    """Adopt the control plane's fleet secrets: apply to the running process AND
    persist so a restart keeps them. Returns True if anything changed. No-op on the CP."""
    if _is_cp():
        return False
    changed = False
    if kek and os.environ.get("CV_KEK_SECRET") != kek:
        os.environ["CV_KEK_SECRET"] = kek
        changed = True
    if session and (get_settings().session_secret or "") != session:
        try:
            from . import security
            security.set_session_secret(session)
            changed = True
        except Exception:  # noqa: BLE001
            pass
    if changed:
        try:
            p = _path()
            p.write_text(json.dumps({"kek": os.environ.get("CV_KEK_SECRET"),
                                     "session": get_settings().session_secret}))
            os.chmod(p, 0o600)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not persist fleet secrets: %s", exc)
        logger.warning("adopted control-plane fleet secrets — KEK/session aligned; "
                       "cross-node credential + vault-key decryption is now consistent")
    return changed


def cp_bundle() -> dict:
    """The control plane's fleet secrets, handed to an authenticated fleet member.
    NEVER expose outside the fleet-authenticated channel."""
    return {"kek": os.environ.get("CV_KEK_SECRET") or "",
            "session": get_settings().session_secret or ""}
