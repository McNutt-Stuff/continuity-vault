"""Node telemetry sampler (control plane).

Records one ``NodeMetric`` per node roughly every minute so the admin can render
CPU / memory / disk / network trend lines, and prunes samples older than 90 days.
Runs only on the control plane, which aggregates the whole fleet's history.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from ..db import SessionLocal
from ..models import Node, NodeMetric

logger = logging.getLogger("cv.telemetry")

_thread: threading.Thread | None = None
_RETENTION_DAYS = 90
_INTERVAL = 60


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _sample_once() -> None:
    from ..api.admin import _node_live  # reuse self/remote telemetry resolution
    with SessionLocal() as db:
        for n in db.query(Node).all():
            try:
                tel = _node_live(db, n)
            except Exception:  # noqa: BLE001 - one node must not stop the rest
                continue
            mem = tel.get("memory") or {}
            stg = tel.get("storage") or {}
            net = tel.get("net") or {}
            load = tel.get("load") or [0]
            db.add(NodeMetric(
                node_id=n.id, ts=_now(),
                cpu_pct=float(tel.get("cpu_pct") or 0),
                mem_pct=float(mem.get("pct") or 0),
                disk_pct=float(stg.get("pct") or 0),
                mem_used=int(mem.get("used") or 0), mem_total=int(mem.get("total") or 0),
                disk_used=int(stg.get("used") or 0), disk_total=int(stg.get("total") or 0),
                net_sent_rate=int(net.get("sent_rate") or 0),
                net_recv_rate=int(net.get("recv_rate") or 0),
                load1=float(load[0] if load else 0)))
        cutoff = _now() - timedelta(days=_RETENTION_DAYS)
        db.query(NodeMetric).filter(NodeMetric.ts < cutoff).delete()
        db.commit()


def start_telemetry_sampler() -> None:
    global _thread
    if _thread is not None:
        return

    def loop() -> None:
        time.sleep(20)
        while True:
            try:
                _sample_once()
            except Exception:  # noqa: BLE001
                logger.exception("telemetry sample cycle failed")
            time.sleep(_INTERVAL)

    _thread = threading.Thread(target=loop, name="cv-telemetry", daemon=True)
    _thread.start()
    logger.info("node telemetry sampler started (1/min, %d-day retention)", _RETENTION_DAYS)
