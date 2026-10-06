"""
Apple Passwords (iCloud Keychain) collector — macOS.

Backs up the user's saved website logins the way the 1Password collector does, so
they flow into the SAME login/credential models + the same recovered-item viewer.

macOS guards iCloud Keychain SECRETS behind per-item authorization, so a headless
background agent cannot read passwords unattended via ``security`` (every item
would pop an approval dialog). So this collector uses two complementary paths:

  * EXPORT (full secrets) — the user exports from the Passwords app
    (Passwords → ⋯ → Export All Passwords…) into the agent's import folder
    (``<data_dir>/apple-passwords``). Each ``*.csv`` is parsed into full login
    objects, then SECURELY DELETED so the plaintext export never lingers on disk.
  * INVENTORY (no secrets, automatic) — ``security dump-keychain`` enumerates the
    login keychain's internet/generic password ITEMS (service, account, url)
    WITHOUT the secret and WITHOUT a prompt, so even with no export the user keeps
    a current backup of WHICH credentials exist.

Each credential is keyed by a stable (domain, account) identity and a content hash
over its durable fields, so re-syncing an unchanged credential never re-versions
(same discipline as the 1Password/Outlook collectors). A credential already
captured WITH a secret is never downgraded back to inventory-only.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import logging
import os
import platform
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("arkive")

_VAULT = "iCloud Keychain"
_STATE_VERSION = 1
_SECURITY = "/usr/bin/security"


def available() -> bool:
    """Apple Passwords is a macOS feature (iCloud Keychain)."""
    return platform.system() == "Darwin"


def import_dir(data_dir: str) -> Path:
    """The folder the user exports the Passwords CSV into. Created on demand."""
    d = Path(data_dir) / "apple-passwords"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # noqa: BLE001
        log.debug("apple_passwords: could not create import dir %s: %s", d, exc)
    return d


# --------------------------------------------------------------------------- #
# Identity + object shaping (1Password-compatible so the viewer renders it)    #
# --------------------------------------------------------------------------- #

def _domain(url: str) -> str:
    u = re.sub(r"^[a-z]+://", "", (url or "").strip(), flags=re.I)
    return u.split("/")[0].split("?")[0].strip().lower()


def _identity(url: str, username: str, title: str) -> str:
    """Stable per-credential key: the site domain (or title) + account. Immune to
    re-export ordering, so re-syncing the same credential keeps the same object."""
    base = (_domain(url) or (title or "").strip().lower()) + "|" + (username or "").strip().lower()
    return hashlib.sha256(base.encode("utf-8", "replace")).hexdigest()[:24]


def _item_object(title: str, url: str, username: str, password: str,
                 notes: str, otp: str) -> dict:
    """Map a credential to the agent object shape, with a 1Password-style detail
    payload so the recovered-item viewer (OnePasswordCard) renders it natively."""
    fields: List[dict] = []
    if username:
        fields.append({"label": "username", "value": username, "type": "STRING", "purpose": "USERNAME"})
    if password:
        fields.append({"label": "password", "value": password, "type": "CONCEALED", "purpose": "PASSWORD"})
    if otp:
        fields.append({"label": "one-time password", "value": otp, "type": "OTP"})
    if notes:
        fields.append({"label": "notes", "value": notes, "type": "STRING", "purpose": "NOTES"})
    disp = (title or "").strip() or _domain(url) or (username or "").strip() or "Login"
    detail = {"title": disp, "category": "LOGIN", "vault": _VAULT,
              "urls": [{"href": url}] if url else [], "fields": fields}
    payload = json.dumps(detail, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    oid = "apple_passwords:" + _identity(url, username, title)
    # Content hash over DURABLE fields only (stable text — the OTP value here is the
    # otpauth seed from the export, which is stable; never a live rotating code), so
    # an unchanged credential re-syncs to the same hash and doesn't re-version.
    sig = json.dumps({"t": disp, "d": _domain(url), "user": username or "",
                      "pw": password or "", "notes": notes or "", "otp": otp or ""},
                     sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    chash = hashlib.sha256(("apple_passwords:" + oid + ":" + sig).encode("utf-8", "replace")).hexdigest()
    return {
        "object_id": oid, "kind": "login", "title": disp,
        "content_b64": base64.b64encode(payload).decode(),
        "content_hash": chash,
        "preview": "Login · " + (_domain(url) or _VAULT) + (" · 2FA" if otp else ""),
        "meta": {"vault": _VAULT, "category": "login", "url": url or None,
                 "username": username or None, "has_password": bool(password),
                 "has_2fa": bool(otp), "source": "apple-passwords"},
        "labels": [_VAULT, "Passwords"],
        "size_bytes": len(payload),
    }


# --------------------------------------------------------------------------- #
# CSV export path (full secrets)                                              #
# --------------------------------------------------------------------------- #
_CSV_KEYS = {
    "title": ("title", "name"),
    "url": ("url", "website", "urls"),
    "username": ("username", "user", "login", "email"),
    "password": ("password", "pass"),
    "notes": ("notes", "note"),
    "otp": ("otpauth", "otp", "totp"),
}


def _pick(row: Dict[str, str], keys: tuple) -> str:
    for k in keys:
        v = row.get(k)
        if v:
            return v
    return ""


def _parse_csv(path: Path) -> List[dict]:
    """Parse one exported Passwords CSV (Title,URL,Username,Password,Notes,OTPAuth)."""
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:  # noqa: BLE001
        log.warning("apple_passwords: cannot read export %s: %s", path.name, exc)
        return []
    out: List[dict] = []
    try:
        reader = csv.DictReader(io.StringIO(text))
        for raw in reader:
            row = {str(k).strip().lower(): (str(v).strip() if v is not None else "")
                   for k, v in raw.items() if k}
            title = _pick(row, _CSV_KEYS["title"])
            url = _pick(row, _CSV_KEYS["url"])
            username = _pick(row, _CSV_KEYS["username"])
            password = _pick(row, _CSV_KEYS["password"])
            notes = _pick(row, _CSV_KEYS["notes"])
            otp = _pick(row, _CSV_KEYS["otp"])
            if not (url or username or title):
                continue
            out.append(_item_object(title, url, username, password, notes, otp))
    except csv.Error as exc:
        log.warning("apple_passwords: malformed export %s: %s", path.name, exc)
    log.info("apple_passwords: parsed %d credential(s) from export %s", len(out), path.name)
    return out


def _secure_delete(path: Path) -> None:
    """Overwrite then remove a plaintext export so it never lingers on disk."""
    try:
        size = path.stat().st_size
        with open(path, "r+b") as f:
            f.write(os.urandom(min(size, 8 * 1024 * 1024)))
            f.flush()
            os.fsync(f.fileno())
    except OSError as exc:  # noqa: BLE001
        log.debug("apple_passwords: could not scrub %s: %s", path.name, exc)
    try:
        path.unlink()
        log.info("apple_passwords: removed plaintext export %s after ingest", path.name)
    except OSError as exc:  # noqa: BLE001
        log.warning("apple_passwords: could not remove export %s: %s", path.name, exc)


# --------------------------------------------------------------------------- #
# Keychain inventory path (metadata only — no secrets, no prompt)             #
# --------------------------------------------------------------------------- #
_ATTR_RE = re.compile(r'"(\w{4})"<[^>]*>="(.*)"')
_LABEL_RE = re.compile(r'0x00000007 <[^>]*>="(.*)"')


def _inventory() -> List[dict]:
    """Enumerate login/internet keychain ITEMS without their secrets (headless,
    no auth prompt) so the credential inventory is always backed up."""
    kc = Path.home() / "Library" / "Keychains" / "login.keychain-db"
    try:
        r = subprocess.run([_SECURITY, "dump-keychain", str(kc)],
                           capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("apple_passwords: keychain inventory unavailable: %s", exc)
        return []
    if r.returncode != 0:
        log.info("apple_passwords: dump-keychain exit %s: %s", r.returncode,
                 (r.stderr or "").strip()[:160])
        return []
    items = _parse_dump(r.stdout or "")
    log.info("apple_passwords: keychain inventory enumerated %d credential item(s)", len(items))
    return items


def _parse_dump(text: str) -> List[dict]:
    """Parse ``security dump-keychain`` output into inventory objects (no secrets).
    Defensive: only internet (inet) / generic (genp) password classes, skip rest."""
    out: List[dict] = []
    cur: Dict[str, str] = {}
    cls: Optional[str] = None

    def _flush():
        if cls in ("inet", "genp"):
            acct = cur.get("acct", "")
            srvr = cur.get("srvr", "")
            svce = cur.get("svce", "")
            label = cur.get("label", "")
            url = srvr or ""
            title = label or svce or srvr or acct or "Login"
            if acct or srvr or svce or label:
                out.append(_item_object(title, url, acct, "", "", ""))  # no secret

    for line in text.splitlines():
        s = line.strip()
        if s.startswith("class:"):
            _flush()
            cur = {}
            m = re.search(r'"(\w+)"', s) or re.search(r'class:\s*(\w+)', s)
            cls = m.group(1) if m else None
            continue
        ml = _LABEL_RE.search(line)
        if ml:
            cur["label"] = ml.group(1)
            continue
        ma = _ATTR_RE.search(line)
        if ma:
            cur[ma.group(1)] = ma.group(2)
    _flush()
    return out


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #

def collect(data_dir: str, state: Optional[dict] = None) -> Tuple[List[dict], dict]:
    """Collect Apple Passwords: full secrets from any export CSV (then scrub it),
    plus the headless keychain inventory for credentials not yet captured with a
    secret. ``state`` records which credentials have been captured WITH a secret so
    the inventory never downgrades a full login back to metadata-only."""
    state = state or {}
    full_ids = set(state.get("full_ids") or []) if state.get("v") == _STATE_VERSION else set()
    objects: List[dict] = []
    seen: set = set()

    # 1) Full secrets from exported CSV(s).
    idir = import_dir(data_dir)
    exports = sorted(idir.glob("*.csv"))
    log.info("apple_passwords: %d export file(s) in %s", len(exports), idir)
    for path in exports:
        for obj in _parse_csv(path):
            oid = obj["object_id"]
            if oid in seen:
                continue
            seen.add(oid)
            objects.append(obj)
            if obj["meta"].get("has_password"):
                full_ids.add(oid)
        _secure_delete(path)

    # 2) Headless inventory — only credentials NOT already captured with a secret
    #    (current run or a prior one), so we never downgrade a full login.
    if available():
        for obj in _inventory():
            oid = obj["object_id"]
            if oid in seen or oid in full_ids:
                continue
            seen.add(oid)
            objects.append(obj)

    full = sum(1 for o in objects if o["meta"].get("has_password"))
    log.info("apple_passwords: %d object(s) to push (%d with secrets, %d inventory-only)",
             len(objects), full, len(objects) - full)
    return objects, {"v": _STATE_VERSION, "full_ids": sorted(full_ids)}
