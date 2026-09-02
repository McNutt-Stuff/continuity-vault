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
        # In-progress interactive auth sessions, keyed by instance id. Holds the
        # open controller session between the login and the OTP submission.
        self._sessions: dict[str, dict] = {}

    @staticmethod
    def _is_auth_error(exc: Exception) -> bool:
        s = str(exc).lower()
        return any(t in s for t in (
            "401", "403", "unauthorized", "forbidden", "invalid token",
            "invalid credentials", "invalid_grant", "access_denied", "reauth",
        ))

    @staticmethod
    def _is_timeout_error(exc: Exception) -> bool:
        if isinstance(exc, TimeoutError):
            return True
        s = str(exc).lower()
        return any(t in s for t in ("timeout", "timed out", "read timeout", "connect timeout"))

    def _normalize_collect_error(self, exc: Exception) -> str:
        msg = (str(exc).strip() or exc.__class__.__name__)
        if self._is_auth_error(exc):
            return ("Authentication failed: " + msg)[:400]
        if self._is_timeout_error(exc):
            return ("Connection timeout: " + msg)[:400]
        return msg[:400]

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
            # A user-requested re-poll runs now, regardless of the interval.
            if not inst.get("repoll") and now - self._last_run.get(iid, 0.0) < interval:
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
        self.log.info("integration %s (%s): starting run", itype, str(inst.get("id", ""))[:8])
        config = inst.get("config") or {}
        creds = inst.get("credentials") or {}
        report, status, error = {"clients": [], "apps": [], "usage": [], "stats": {}}, "ok", None
        try:
            report = runner.collect(config, creds, self.log)
        except Exception as exc:  # noqa: BLE001
            status, error = "error", self._normalize_collect_error(exc)
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

    # ------------------------------------------------------------------ #
    # Interactive provisioning (OTP handshake with the controller)       #
    # ------------------------------------------------------------------ #
    async def provision_tick(self) -> None:
        base = self._base()
        pending = await self._provision_pending(base)
        self._reap_sessions()
        if not pending:
            return
        loop = asyncio.get_event_loop()
        await asyncio.gather(*(loop.run_in_executor(None, self._provision_one, base, p)
                               for p in pending), return_exceptions=True)

    async def _provision_pending(self, base: str) -> list:
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(f"{base}/appliance/integrations/provision/pending",
                                headers=self._headers())
                if r.status_code != 200:
                    return []
                return r.json().get("pending", []) or []
        except Exception as exc:  # noqa: BLE001
            self.log.debug("provision pending fetch failed: %s", exc)
            return []

    def _reap_sessions(self) -> None:
        cutoff = time.time() - 600  # sessions are only valid for a few minutes
        for iid in [k for k, v in self._sessions.items() if v.get("ts", 0) < cutoff]:
            try:
                self._close_session_client(self._sessions.pop(iid)["session"], iid, "reap")
            except Exception as exc:  # noqa: BLE001
                self.log.debug("integration session reap failed (%s): %s", iid, exc)

    def _report_provision(self, base: str, iid: str, state: str,
                          message: str | None = None, cred_update: dict | None = None) -> None:
        payload = {"integration_id": iid, "provision_state": state, "message": message}
        if cred_update:
            payload["credentials_update"] = cred_update
        try:
            with httpx.Client(timeout=60) as c:
                r = c.post(f"{base}/appliance/integrations/provision/report",
                           json=payload, headers=self._headers())
                if r.status_code >= 300:
                    self.log.warning("provision report rejected for %s: %s %s",
                                     iid, r.status_code, r.text[:200])
        except Exception as exc:  # noqa: BLE001
            self.log.warning("provision report POST failed for %s: %s", iid, exc)

    def _close_session_client(self, session: dict, iid: str, context: str) -> None:
        try:
            client = session.get("client")
            if client is not None:
                client.close()
        except Exception as exc:  # noqa: BLE001
            self.log.debug("integration session close failed (%s, %s): %s", iid, context, exc)

    def _provision_one(self, base: str, p: dict) -> None:
        iid = p.get("id")
        itype = p.get("integration_type", "")
        runner = get_runner(itype)
        if runner is None or not hasattr(runner, "begin_auth"):
            self._report_provision(base, iid, "error", "This integration can't be set up here.")
            return
        config = p.get("config") or {}
        creds = p.get("credentials") or {}
        action = p.get("action", "start")

        if action == "otp":
            entry = self._sessions.get(iid)
            if not entry:
                self._report_provision(base, iid, "error",
                                       "Setup session expired — please start over.")
                return
            result = runner.submit_otp(entry["session"], creds, p.get("otp", ""), self.log)
            if result.get("state") == "authenticated":
                self._finish(base, iid, itype, entry.pop("session"), config)
                self._sessions.pop(iid, None)
            else:
                # Keep the session so the user can retry the code.
                self._report_provision(base, iid, "awaiting_otp",
                                       result.get("message") or "That code didn't work — try again.")
            return

        # action == "start": begin the login.
        self.log.info("integration %s (%s): beginning auth", itype, str(iid)[:8])
        self._report_provision(base, iid, "authenticating", "Contacting your controller…")
        try:
            session, result = runner.begin_auth(config, creds, self.log)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("%s begin_auth failed: %s", itype, exc)
            self._report_provision(base, iid, "error", f"Could not reach the controller: {exc}")
            return
        state = result.get("state")
        if state == "authenticated":
            self._finish(base, iid, itype, session, config)
        elif state == "mfa_required":
            self._sessions[iid] = {"session": session, "ts": time.time()}
            self._report_provision(base, iid, "awaiting_otp",
                                   result.get("message") or "Enter your verification code.")
        else:
            if session:
                self._close_session_client(session, str(iid), "begin_auth")
            self._report_provision(base, iid, "error",
                                   result.get("message") or "Could not sign in to the controller.")

    def _finish(self, base: str, iid: str, itype: str, session: dict, config: dict) -> None:
        """Mint/persist a reusable credential and report success (or a clear error
        if no durable credential could be secured)."""
        try:
            cred_update = get_runner(itype).finalize(session, config, self.log)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("%s finalize failed: %s", itype, exc)
            cred_update = None
        finally:
            self._close_session_client(session, str(iid), "finalize")
        if not cred_update or not (cred_update.get("api_key") or cred_update.get("cookies")):
            self._report_provision(base, iid, "error",
                                   "Signed in, but couldn't secure an API key on the controller.")
            return
        self.log.info("integration %s (%s): provisioning complete", itype, str(iid)[:8])
        self._report_provision(base, iid, "done", "Connected and secured.", cred_update)
