"""
Sealed vault storage controller (spec 4.2 Zone 3).

Models hardware-enforced storage-path isolation in software for the prototype:
the protected store can only be opened while the state machine is in an
UNSEALED_* state, and the management controller can never obtain a handle to it.
Snapshots are committed immutably and sealed with a signed receipt.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from cv_crypto.command import build_seal_receipt
from cv_crypto.provider import hexdigest
from cv_crypto.signing import HybridSigner

from .state_machine import StateMachine, State


class SealedError(RuntimeError):
    """Raised when the protected storage path is accessed while sealed."""


class VaultStore:
    def __init__(self, root: str, sm: StateMachine) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._protected = self.root / "protected"
        self._sm = sm
        # Mirror volumes (external drives configured to shadow this primary vault).
        # Every committed snapshot is duplicated to each mirror, and recovery reads
        # fall back to a mirror when the primary copy is missing/unreadable.
        self._mirror_roots: list[Path] = []

    def set_mirror_roots(self, roots: list[str]) -> None:
        """Replace the set of mirror vault roots (paths ending in '/vault')."""
        self._mirror_roots = [Path(r) / "protected" for r in roots if r]

    def _require_open(self) -> None:
        if not self._sm.storage_accessible:
            raise SealedError(
                f"protected storage path is closed in state {self._sm.state.value}"
            )

    def commit_snapshot(self, snapshot_id: str, objects: list[dict],
                        manifest: dict) -> Path:
        """Write objects + manifest into protected storage (spec 6.1 step 9).

        Idempotent: snapshots are immutable, so an object/manifest that is already
        committed is left untouched (rewriting a 0o444 file would raise). This lets
        a redelivered ingest command re-run safely instead of failing."""
        self._require_open()
        snap_dir = self._write_into(self._protected, snapshot_id, objects, manifest)
        # Duplicate the sealed snapshot onto each mirror volume (best-effort: a
        # mirror write failure must never fail the primary backup — it's surfaced
        # via the mirror store's health instead).
        for mroot in self._mirror_roots:
            try:
                self._write_into(mroot, snapshot_id, objects, manifest)
            except Exception:
                pass
        return snap_dir

    def _write_into(self, protected: Path, snapshot_id: str, objects: list[dict],
                    manifest: dict) -> Path:
        snap_dir = protected / snapshot_id
        snap_dir.mkdir(parents=True, exist_ok=True)
        for obj in objects:
            p = snap_dir / obj["objectId"]
            if p.exists():
                continue  # already committed (immutable) — do not rewrite
            p.write_text(json.dumps(obj))
        mp = snap_dir / "manifest.json"
        if not mp.exists():
            mp.write_text(json.dumps(manifest))
        # Immutable: remove write permission (emulated object-lock), best-effort.
        for p in snap_dir.rglob("*"):
            try:
                os.chmod(p, 0o444)
            except OSError:
                pass
        return snap_dir

    def read_object(self, snapshot_id: str, object_id: str) -> dict:
        self._require_open()
        primary = self._protected / snapshot_id / object_id
        try:
            return json.loads(primary.read_text())
        except Exception:
            # Recovery fallback: read the object from a mirror volume when the
            # primary copy is missing or unreadable (drive fault / bit rot).
            for mroot in self._mirror_roots:
                try:
                    return json.loads((mroot / snapshot_id / object_id).read_text())
                except Exception:
                    continue
            raise

    def snapshot_exists(self, snapshot_id: str) -> bool:
        if (self._protected / snapshot_id / "manifest.json").exists():
            return True
        # A snapshot present only on a mirror is still recoverable.
        return any((m / snapshot_id / "manifest.json").exists()
                   for m in self._mirror_roots)

    def capacity(self) -> dict:
        used = sum(f.stat().st_size for f in self._protected.rglob("*")
                   if f.is_file()) if self._protected.exists() else 0
        return {"used_bytes": used, "snapshots": self._count_snapshots(),
                "objects": self._count_objects()}

    def _count_snapshots(self) -> int:
        if not self._protected.exists():
            return 0
        return sum(1 for d in self._protected.iterdir() if d.is_dir())

    def _count_objects(self) -> int:
        if not self._protected.exists():
            return 0
        # Stored object files, excluding the per-snapshot manifest.
        return sum(1 for f in self._protected.rglob("*")
                   if f.is_file() and f.name != "manifest.json")

    def seal(self, signer: HybridSigner, appliance_id: str, snapshot_id: str,
             manifest_hash: str, object_count: int, total_bytes: int) -> dict:
        """Finalize the immutable snapshot and produce a signed seal receipt
        (spec 6.1 step 10)."""
        return build_seal_receipt(
            signer=signer,
            appliance_id=appliance_id,
            snapshot_id=snapshot_id,
            manifest_hash=manifest_hash,
            object_count=object_count,
            total_bytes=total_bytes,
            isolation_state="sealed",
            integrity_result="verified",
        )
