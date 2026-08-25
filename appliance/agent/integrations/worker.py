"""Appliance integrations worker.

Polls the node / control plane for this appliance's enabled integrations, runs
each due integration (in a thread, since the runners are blocking) on its own
interval, and ships the normalized report back. Runs in parallel with the
heartbeat loop and never lets one integration's failure stop the others.
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable

import httpx

from . import get_runner


class IntegrationWorker:
    def __init__(self, base_getter: Callable[[], str], headers_getter: Callable[[], dict], log):
        self._base = base_getter
        self._headers = headers_getter
        self.log = log
        self._last_run: dict[str, float] = {}

    async def tick(self) -> None:
        base = self._base()
        instances = await self._pull(base)
        loop = asyncio.get_event_loop()
        due = []
        now = time.time()
        for inst in instances:
            iid = inst.get("id")
            if not iid or not inst.get("enabled", True):
                continue
            interval = max(5, int(inst.get("poll_interval_minutes") or 60)) * 60
            if now - self._last_run.get(iid, 0.0) < interval:
                continue
            self._last_run[iid] = now
            due.append(inst)
        if not due:
            return
        # Run all due integrations in parallel (each in its own thread).
        await asyncio.gather(*(loop.run_in_executor(None, self._run_one, base, inst)
                               for inst in due), return_exceptions=True)

    async def _pull(self, base: str) -> list:
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(f"{base}/appliance/integrations/pull", headers=self._headers())
                if r.status_code != 200:
                    return []
                return r.json().get("integrations", []) or []
        except Exception as exc:  # noqa: BLE001
            self.log.debug("integrations pull failed: %s", exc)
            return []

    def _run_one(self, base: str, inst: dict) -> None:
        itype = inst.get("integration_type", "")
        runner = get_runner(itype)
        if runner is None:
            self.log.warning("no runner for integration type %s", itype)
            return
        config = inst.get("config") or {}
        creds = inst.get("credentials") or {}
        cred_update = None
        # Easy setup: mint/refresh a reusable credential the first time so future
        # polls are headless (and the raw password can be discarded upstream).
        if not creds.get("api_key") and hasattr(runner, "provision"):
            try:
                new_creds = runner.provision(config, creds, self.log)
                if new_creds:
                    cred_update = new_creds
                    creds = {**creds, **new_creds}
            except Exception as exc:  # noqa: BLE001
                self.log.warning("%s provision failed: %s", itype, exc)
        report, status, error = {"clients": [], "apps": [], "usage": [], "stats": {}}, "ok", None
        try:
            report = runner.collect(config, creds, self.log)
        except Exception as exc:  # noqa: BLE001
            status, error = "error", str(exc)[:400]
            self.log.warning("%s collect failed: %s", itype, exc)
        payload = {
            "integration_id": inst.get("id"),
            "integration_type": itype,
            "status": status,
            "error": error,
            "clients": report.get("clients", []),
            "apps": report.get("apps", []),
            "usage": report.get("usage", []),
            "stats": report.get("stats", {}),
        }
        if cred_update:
            payload["credentials_update"] = cred_update
        try:
            with httpx.Client(timeout=60) as c:
                r = c.post(f"{base}/appliance/integrations/report",
                           json=payload, headers=self._headers())
                if r.status_code >= 300:
                    self.log.warning("integration report rejected (%s) for %s",
                                     r.status_code, itype)
                else:
                    self.log.info("integration %s reported: %s", itype, report.get("stats", {}))
        except Exception as exc:  # noqa: BLE001
            self.log.warning("integration report POST failed for %s: %s", itype, exc)
