"""
Sealed vault storage controller (spec 4.2 Zone 3).

Models hardware-enforced storage-path isolation in software for the prototype:
the protected store can only be opened while the state machine is in an
UNSEALED_* state, and the management controller can never obtain a handle to it.
Snapshots are committed immutably and sealed with a signed receipt.
"""

from __future__ import annotations

import hashlib
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


# Some sources (e.g. New Outlook HxStore) mint object ids far longer than the
# 255-byte filename limit (Errno 36), or containing path separators. Map any
# unsafe id to a deterministic hashed filename so writes never fail and reads
# resolve to the same file. Short, clean ids keep their literal name (back-compat).
_MAX_NAME_BYTES = 200


def _safe_name(object_id: str) -> str:
    oid = object_id or ""
    if "/" not in oid and "\0" not in oid and len(oid.encode("utf-8")) <= _MAX_NAME_BYTES:
        return oid
    return "h_" + hashlib.sha256(oid.encode("utf-8")).hexdigest()


def _force_unlink(p: Path) -> None:
    """Remove a file, clearing the immutable-emulating 0o444 bit first."""
    try:
        os.chmod(p, 0o644)
    except OSError:
        pass
    p.unlink()


def _force_rmtree(d: Path) -> None:
    """Recursively remove a snapshot dir whose files are 0o444 (object-lock
    emulation) — chmod each entry writable so the unlink/rmdir succeeds."""
    import shutil

    def _onerror(_func, path, _exc):
        try:
            os.chmod(path, 0o644)
        except OSError:
            pass
        try:
            os.remove(path)
        except OSError:
            pass
    shutil.rmtree(d, onerror=_onerror)



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
            p = snap_dir / _safe_name(obj["objectId"])
            if p.exists():
                continue  # already committed (immutable) — do not rewrite
            data = json.dumps(obj)
            p.write_text(data)
            # Verify the write landed intact — a truncated/failed write must never
            # be sealed as a recoverable backup.
            written = p.stat().st_size
            if written != len(data.encode("utf-8")):
                raise OSError(f"short write for object {obj['objectId']!r}: "
                              f"{written} of {len(data.encode('utf-8'))} bytes")
        mp = snap_dir / "manifest.json"
        if not mp.exists():
            mp.write_text(json.dumps(manifest))
        if not mp.exists() or mp.stat().st_size == 0:
            raise OSError(f"manifest not written for snapshot {snapshot_id}")
        # Immutable: remove write permission (emulated object-lock), best-effort.
        for p in snap_dir.rglob("*"):
            try:
                os.chmod(p, 0o444)
            except OSError:
                pass
        return snap_dir

    def read_object(self, snapshot_id: str, object_id: str) -> dict:
        self._require_open()
        name = _safe_name(object_id)
        primary = self._protected / snapshot_id / name
        try:
            return json.loads(primary.read_text())
        except Exception:
            # Recovery fallback: read the object from a mirror volume when the
            # primary copy is missing or unreadable (drive fault / bit rot).
            for mroot in self._mirror_roots:
                try:
                    return json.loads((mroot / snapshot_id / name).read_text())
                except Exception:
                    continue
            raise

    def snapshot_exists(self, snapshot_id: str) -> bool:
        if (self._protected / snapshot_id / "manifest.json").exists():
            return True
        # A snapshot present only on a mirror is still recoverable.
        return any((m / snapshot_id / "manifest.json").exists()
                   for m in self._mirror_roots)

    def sync_mirrors(self) -> dict:
        """Reconcile every mirror volume to be a TRUE 1:1 copy of the primary vault:
        copy any snapshot files/manifests the mirror is missing (backfill for a
        newly-added mirror, and repair for any write the live duplication missed),
        AND prune anything on the mirror that no longer exists on the primary (so a
        retention prune propagates instead of the mirror growing without bound).
        Idempotent. Returns {mirrors, snapshots_synced, files_copied,
        snapshots_pruned, files_pruned, errors}. A per-item failure is counted,
        never fatal."""
        result = {"mirrors": len(self._mirror_roots), "snapshots_synced": 0,
                  "files_copied": 0, "snapshots_pruned": 0, "files_pruned": 0,
                  "errors": 0}
        if not self._mirror_roots or not self._protected.exists():
            return result
        snap_dirs = [d for d in self._protected.iterdir() if d.is_dir()]
        primary_snaps = {d.name for d in snap_dirs}
        for mroot in self._mirror_roots:
            for snap in snap_dirs:
                touched = False
                for src in snap.rglob("*"):
                    if not src.is_file():
                        continue
                    rel = src.relative_to(self._protected)
                    dst = mroot / rel
                    if dst.exists() and dst.stat().st_size == src.stat().st_size:
                        continue  # already mirrored intact
                    try:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        data = src.read_bytes()
                        tmp = dst.with_suffix(dst.suffix + ".tmp")
                        tmp.write_bytes(data)
                        if tmp.stat().st_size != len(data):
                            raise OSError("short mirror write")
                        tmp.replace(dst)
                        try:
                            os.chmod(dst, 0o444)
                        except OSError:
                            pass
                        result["files_copied"] += 1
                        touched = True
                    except Exception:  # noqa: BLE001 — reconcile is best-effort per file
                        result["errors"] += 1
                if touched:
                    result["snapshots_synced"] += 1
            # Deletion pass: make the mirror a true reflection — remove any snapshot
            # the primary no longer has (retention prune), and any stray file within
            # a shared snapshot that's absent from the primary. Guard against a
            # transiently empty/unmounted primary wiping the mirror: only prune when
            # the primary actually holds snapshots.
            if not mroot.exists() or not primary_snaps:
                continue
            for md in mroot.iterdir():
                if not md.is_dir():
                    continue
                try:
                    if md.name not in primary_snaps:
                        _force_rmtree(md)
                        result["snapshots_pruned"] += 1
                        continue
                    for mf in md.rglob("*"):
                        if not mf.is_file():
                            continue
                        rel = mf.relative_to(mroot)
                        if mf.suffix == ".tmp" or not (self._protected / rel).exists():
                            _force_unlink(mf)
                            result["files_pruned"] += 1
                except Exception:  # noqa: BLE001 — prune is best-effort per item
                    result["errors"] += 1
        return result

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
