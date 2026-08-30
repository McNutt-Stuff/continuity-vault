"""Web-based remote terminal for appliances (spec: secure remote administration).

An admin opens a session from the fleet UI; the control plane issues a signed
``OPEN_TERMINAL`` command over the appliance's heartbeat channel. The appliance
verifies the command, spawns a local PTY, and dials an outbound WebSocket back to
the control plane. This module is the in-memory relay that bridges the admin's
browser terminal to the appliance's PTY.

The relay holds no shell itself and stores nothing durably: sessions live only in
process memory and are torn down when either side disconnects. Because the bridge
is in-memory it assumes a single control-plane worker process.
"""

from __future__ import annotations

import asyncio
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect

from .. import fleet, security
from ..db import get_db
from ..models import Appliance
from sqlalchemy.orm import Session

admin_terminal_router = APIRouter(prefix="/admin", tags=["appliance-terminal"])
agent_terminal_router = APIRouter(prefix="/appliance", tags=["appliance-terminal"])

_SESSION_TTL = 15 * 60  # seconds a session may stay open before it is reaped


class _Session:
    def __init__(self, appliance_id: str, appliance_token: str, admin_token: str):
        self.appliance_id = appliance_id
        self.appliance_token = appliance_token
        self.admin_token = admin_token
        self.to_appliance: asyncio.Queue[str] = asyncio.Queue()
        self.to_admin: asyncio.Queue[str] = asyncio.Queue()
        self.created_at = time.time()
        self.appliance_connected = False
        self.admin_connected = False
        self.closed = False


_SESSIONS: dict[str, _Session] = {}


def _reap() -> None:
    now = time.time()
    for sid in [s for s, v in _SESSIONS.items()
                if v.closed or (now - v.created_at) > _SESSION_TTL]:
        _SESSIONS.pop(sid, None)


@admin_terminal_router.post("/appliances/{aid}/terminal")
def open_terminal(aid: str,
                  principal: security.Principal = Depends(security.require_platform_admin),
                  db: Session = Depends(get_db)):
    """Open a remote-terminal session and dispatch a signed OPEN_TERMINAL command.

    Returns the admin WebSocket URL (carrying a one-time session token). The
    appliance receives its own session token inside the signed command."""
    _reap()
    a = db.get(Appliance, aid)
    if not a:
        raise HTTPException(404, "appliance not found")
    session_id = secrets.token_urlsafe(12)
    appliance_token = secrets.token_urlsafe(24)
    admin_token = secrets.token_urlsafe(24)
    _SESSIONS[session_id] = _Session(aid, appliance_token, admin_token)
    fleet.issue_command(db, a, "OPEN_TERMINAL", principal.user_id, {
        "sessionId": session_id,
        "wsPath": f"/api/appliance/terminal/{session_id}",
        "sessionToken": appliance_token,
    })
    return {
        "session_id": session_id,
        "ws_url": f"/api/admin/appliances/{aid}/terminal/{session_id}?token={admin_token}",
    }


async def _bridge(ws: WebSocket, recv_queue: asyncio.Queue, send_queue: asyncio.Queue,
                  sess: _Session) -> None:
    """Pump this socket: inbound frames -> recv_queue, send_queue -> outbound."""

    async def inbound() -> None:
        while True:
            data = await ws.receive_text()
            await recv_queue.put(data)

    async def outbound() -> None:
        while True:
            data = await send_queue.get()
            await ws.send_text(data)

    tasks = [asyncio.create_task(inbound()), asyncio.create_task(outbound())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        sess.closed = True


@agent_terminal_router.websocket("/terminal/{session_id}")
async def appliance_terminal_ws(ws: WebSocket, session_id: str, token: str = ""):
    """The appliance connects here (outbound). Frames from the appliance's PTY are
    forwarded to the admin; typed input is forwarded to the appliance."""
    sess = _SESSIONS.get(session_id)
    if not sess or not token or not secrets.compare_digest(token, sess.appliance_token):
        await ws.close(code=4401)
        return
    await ws.accept()
    sess.appliance_connected = True
    try:
        # appliance receives typed input (to_appliance), emits PTY output (to_admin)
        await _bridge(ws, sess.to_admin, sess.to_appliance, sess)
    except WebSocketDisconnect:
        pass
    finally:
        sess.closed = True
        _SESSIONS.pop(session_id, None)


@admin_terminal_router.websocket("/appliances/{aid}/terminal/{session_id}")
async def admin_terminal_ws(ws: WebSocket, aid: str, session_id: str, token: str = ""):
    """The admin's browser terminal connects here. Auth is the one-time session
    token minted by ``open_terminal`` (browsers cannot set WS auth headers)."""
    sess = _SESSIONS.get(session_id)
    if (not sess or sess.appliance_id != aid or not token
            or not secrets.compare_digest(token, sess.admin_token)):
        await ws.close(code=4401)
        return
    await ws.accept()
    sess.admin_connected = True
    try:
        # admin emits typed input (to_appliance), receives PTY output (to_admin)
        await _bridge(ws, sess.to_appliance, sess.to_admin, sess)
    except WebSocketDisconnect:
        pass
    finally:
        sess.closed = True
