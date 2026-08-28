"""Public email open-tracking pixel.

The 1x1 pixel embedded in every outbound email points here (always on the control
plane). Hitting it records the open against the ``Communication`` row. No auth: the
opaque message id is the only token, and a hit reveals nothing.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from .. import comms

public_router = APIRouter(tags=["comms"])

_NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, private, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


@public_router.get("/c/o/{cid}")
def open_pixel(cid: str, request: Request):
    """Record an email open and return a 1x1 transparent GIF."""
    fwd = request.headers.get("x-forwarded-for", "")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")
    # The pixel is served as "<id>.gif"; strip any extension to get the id.
    comms.mark_opened(cid.split(".")[0], ip)
    return Response(content=comms.PIXEL_GIF, media_type="image/gif", headers=_NO_CACHE)
