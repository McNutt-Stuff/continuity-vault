"""
1Password collector — extracts item metadata + detail via the `op` CLI and
normalizes to the platform's canonical kinds. Secret field values are carried
only inside the item detail payload (encrypted server-side); metadata used for
preview/search excludes secrets.

Auth: uses a 1Password service account token when provided
(OP_SERVICE_ACCOUNT_TOKEN), otherwise relies on an interactive `op` session
(1Password desktop app + CLI integration).
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
from typing import List

log = logging.getLogger("arkive")

# Short-lived cache so telemetry doesn't run `op whoami` on every heartbeat.
_auth_cache = {"ts": 0.0, "state": None, "key": None}

# 1Password item category -> canonical kind (category is derived server-side).
_OP_KIND = {
    "LOGIN": "login", "PASSWORD": "password", "API_CREDENTIAL": "api_key",
    "SSH_KEY": "ssh_key", "DATABASE": "database", "SERVER": "server",
    "WIRELESS_ROUTER": "wifi", "CRYPTO_WALLET": "crypto_wallet",
    "SECURE_NOTE": "secure_note", "SOFTWARE_LICENSE": "software_license",
    "CREDIT_CARD": "credit_card", "BANK_ACCOUNT": "bank_account",
    "MEMBERSHIP": "membership", "REWARD_PROGRAM": "membership",
    "IDENTITY": "identity", "PASSPORT": "passport",
    "DRIVER_LICENSE": "drivers_license", "SOCIAL_SECURITY_NUMBER": "ssn",
    "MEDICAL_RECORD": "medical_record", "DOCUMENT": "generic",
}


def _op_path() -> str:
    # Prefer a bundled op binary, then PATH.
    return os.environ.get("ARKIVE_OP_PATH") or shutil.which("op") or ""


def available() -> bool:
    return bool(_op_path())


def _env(token: str) -> dict:
    env = os.environ.copy()
    if token:
        env["OP_SERVICE_ACCOUNT_TOKEN"] = token
    else:
        # An empty OP_SERVICE_ACCOUNT_TOKEN (launchd sets the key blank when no
        # token is configured) makes op attempt service-account auth and fail.
        # Remove it so op falls back to the 1Password app CLI integration.
        env.pop("OP_SERVICE_ACCOUNT_TOKEN", None)
    return env


def auth_state(token: str = "", max_age: float = 300.0) -> str:
    """Report whether op can authenticate (surfaced in telemetry).

    Cached briefly so telemetry doesn't spawn `op whoami` on every heartbeat."""
    if not available():
        return "absent"
    now = time.time()
    key = hash(token)
    if _auth_cache["state"] and _auth_cache["key"] == key \
            and now - _auth_cache["ts"] < max_age:
        return _auth_cache["state"]
    env = _env(token)
    try:
        r = subprocess.run([_op_path(), "whoami"], capture_output=True, text=True,
                           env=env, timeout=20)
        if r.returncode == 0:
            log.debug("op whoami ok: %s", (r.stdout or "").strip())
            state = "service-account" if token else "interactive"
        else:
            log.debug("op whoami failed (exit %s): %s", r.returncode,
                      (r.stderr or r.stdout or "").strip())
            state = "unauthenticated"
    except Exception as exc:
        log.debug("op whoami error: %s", exc)
        state = "unauthenticated"
    _auth_cache.update(ts=now, state=state, key=key)
    return state


def _op(args: List[str], env: dict) -> str:
    op = _op_path()
    if not op:
        raise RuntimeError("1Password CLI (op) not found")
    log.debug("running: op %s", " ".join(args))
    proc = subprocess.run([op, *args], capture_output=True, text=True,
                          env=env, timeout=60)
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        log.debug("op %s -> exit %s: %s", args[0] if args else "", proc.returncode, msg)
        raise RuntimeError(
            f"op {' '.join(args)} failed (exit {proc.returncode}): {msg}")
    return proc.stdout


# Keys whose values change every read (not real edits) and would otherwise make
# an unchanged item hash differently each run, creating a spurious new version.
_VOLATILE_FIELD_KEYS = ("totp",)

# Metadata keys that 1Password bumps WITHOUT the secret's content changing (an
# internal re-save, a version tick, a "last edited by" refresh). Excluding them —
# plus anything ending in "_at" (updated_at/created_at/…) — from the dedup hash
# means an item only re-versions when its actual content changes, not when its
# timestamp moves. NOTE: user-set date FIELD values live in field["value"], not
# in these metadata keys, so a real edit to a date field still re-versions.
_HASH_DROP_KEYS = {"updated_at", "created_at", "last_edited_by", "version",
                   "totp", "last_used", "recorded_at", "state"}


def _canonical_payload(detail: dict) -> bytes:
    """Deterministic serialization for content-hash dedup: drop live/volatile
    values (e.g. the current 30-second TOTP code) and sort keys so re-indexing an
    unchanged item produces an identical hash instead of a fresh version."""
    clean = copy.deepcopy(detail)
    for f in clean.get("fields", []) or []:
        if isinstance(f, dict):
            for k in _VOLATILE_FIELD_KEYS:
                f.pop(k, None)
    return json.dumps(clean, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def _scrub_for_hash(obj):
    """Recursively strip volatile/timestamp metadata so the dedup hash reflects
    only the item's real content."""
    if isinstance(obj, dict):
        return {k: _scrub_for_hash(v) for k, v in obj.items()
                if k not in _HASH_DROP_KEYS and not str(k).endswith("_at")}
    if isinstance(obj, list):
        return [_scrub_for_hash(v) for v in obj]
    return obj


def _stable_content_hash(item_id: str, detail: dict) -> str:
    """Content-based dedup key immune to 1Password's timestamp/version churn: hash
    the scrubbed item content, NOT its updated_at — so an item that 1Password
    re-touched without a real edit keeps the same hash and never re-versions."""
    scrubbed = _scrub_for_hash(detail)
    blob = json.dumps(scrubbed, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(f"onepassword:{item_id}:".encode() + blob).hexdigest()


def collect(op_token: str = "") -> List[dict]:
    """Return normalized agent objects for every reachable 1Password item.

    Runs op directly; if no account is signed in, op raises a clear error that the
    caller classifies as a skip (see agent collect loop)."""
    env = _env(op_token)
    items = json.loads(_op(["item", "list", "--format=json"], env))
    objects: List[dict] = []
    for it in items:
        try:
            detail = json.loads(_op(["item", "get", it["id"], "--format=json"], env))
        except Exception:
            detail = it
        category = it.get("category", "")
        kind = _OP_KIND.get(str(category).upper(), "secret")
        vault = (it.get("vault") or {}).get("name", "")
        urls = [u.get("href") for u in it.get("urls", []) if u.get("href")]
        username = ""
        for f in detail.get("fields", []):
            if f.get("purpose") == "USERNAME":
                username = f.get("value", "")
        payload = _canonical_payload(detail)
        # Dedup on the item's STABLE content (timestamps/version scrubbed) rather
        # than 1Password's updated_at — which bumps on internal re-saves and made
        # unchanged items re-version every run. A real content edit still changes
        # the hash; a spurious timestamp move no longer does.
        content_hash = _stable_content_hash(it["id"], detail)
        objects.append({
            "object_id": f"onepassword:{it['id']}",
            "kind": kind,
            "title": it.get("title", "(untitled)"),
            "content_b64": base64.b64encode(payload).decode(),
            "content_hash": content_hash,
            "preview": f"{category} · {vault}",
            "meta": {"vault": vault, "category": category, "tags": it.get("tags", []),
                     "url": urls[0] if urls else None, "username": username},
            "labels": [vault, *it.get("tags", [])],
            "size_bytes": len(payload),
        })
    return objects
