"""In-process background-worker status registry.

Long-running workers (scheduler steps, index replication, integrity verification)
record their last run + outcome here so the admin Node → Processes tab can show
what the workers are doing and whether they're healthy — no DB table needed
(worker health is live, per-process, and reset on restart).
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

_LOCK = threading.Lock()
_STATUS: dict[str, dict] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def record(name: str, *, state: str = "idle", health: str = "ok",
           message: str = "", **extra) -> None:
    """Record a worker's latest state. health ∈ ok|warning|error; state is a short
    verb (running|idle|failed)."""
    with _LOCK:
        row = _STATUS.setdefault(name, {"name": name, "runs": 0})
        row.update(state=state, health=health, message=message,
                   updated_at=_now_iso(), **extra)
        if state == "running":
            row["started_at"] = _now_iso()
        else:
            row["last_finished_at"] = _now_iso()
            if state != "failed":
                row["runs"] = row.get("runs", 0) + 1


def snapshot() -> list[dict]:
    with _LOCK:
        return sorted((dict(v) for v in _STATUS.values()), key=lambda r: r["name"])
