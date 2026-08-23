"""Reusable, header-aware API rate limiting for connector pulls.

Providers such as GitHub cap authenticated calls per hour (GitHub: 5,000/hr).
This gate reads the standard ``X-RateLimit-*`` response headers and, when the
remaining quota runs low, waits for the window to reset instead of failing —
keeping a long backup under the limit. It reports a "waiting" status so a
running job stays visible, and bounds how long it will sleep inline: waits
longer than the budget raise ``RateLimitExceeded`` so the caller can stop the
chunk early and resume on the next cycle (see the sync worker's crawl loop).

Designed to be shared by any connector: pass the header names for the provider
(the defaults match GitHub / the common convention).
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

import httpx

logger = logging.getLogger("cv.ratelimit")

StatusFn = Optional[Callable[[str], None]]


class RateLimitExceeded(Exception):
    """A provider rate limit could not be waited out within the allowed budget.

    Carries ``reset_at`` (epoch seconds) so the caller can schedule a resume."""

    def __init__(self, message: str, reset_at: Optional[float] = None) -> None:
        super().__init__(message)
        self.reset_at = reset_at


class RateLimiter:
    """Tracks a provider's remaining quota + reset time from response headers and
    paces requests to stay under the limit.

    ``floor`` leaves a safety buffer (stop before hitting zero). ``max_wait_seconds``
    bounds a single inline sleep — a longer required wait raises so the caller can
    defer the rest of the work (the background job loop then waits and resumes)."""

    def __init__(self, *, name: str = "api", floor: int = 50,
                 max_wait_seconds: int = 30,
                 remaining_header: str = "x-ratelimit-remaining",
                 reset_header: str = "x-ratelimit-reset",
                 retry_after_header: str = "retry-after") -> None:
        self.name = name
        self.floor = floor
        self.max_wait = max_wait_seconds
        self.remaining_header = remaining_header
        self.reset_header = reset_header
        self.retry_after_header = retry_after_header
        self.remaining: Optional[int] = None
        self.reset_at: Optional[float] = None

    def update(self, resp: httpx.Response) -> None:
        h = resp.headers
        rem = h.get(self.remaining_header)
        if rem is not None:
            try:
                self.remaining = int(rem)
            except ValueError:
                pass
        rst = h.get(self.reset_header)
        if rst is not None:
            try:
                self.reset_at = float(rst)
            except ValueError:
                pass

    def _sleep(self, seconds: float, status: StatusFn, why: str) -> None:
        seconds = max(0.0, seconds)
        if seconds <= 0:
            return
        logger.warning("%s rate limit: sleeping %.0fs (%s)", self.name, seconds, why)
        end = time.time() + seconds
        # Sleep in short slices so the status message (and any cancellation raised
        # by it) stays responsive during the wait.
        while True:
            left = end - time.time()
            if left <= 0:
                break
            if status:
                status(f"Waiting {int(left)}s for {self.name} rate limit to reset…")
            time.sleep(min(left, 10))

    def before(self, status: StatusFn = None) -> None:
        """Wait when the known remaining quota is at/below the floor. Raises if the
        wait until reset exceeds the inline budget."""
        if self.remaining is not None and self.remaining <= self.floor and self.reset_at:
            wait = self.reset_at - time.time() + 2
            if wait > 0:
                if wait > self.max_wait:
                    raise RateLimitExceeded(
                        f"{self.name} quota low; resets in {int(wait)}s", self.reset_at)
                self._sleep(wait, status, "quota low")
                self.remaining = None  # unknown until the next response refreshes it

    def is_rate_limited(self, resp: httpx.Response) -> bool:
        if resp.status_code not in (403, 429):
            return False
        rem = resp.headers.get(self.remaining_header)
        if rem is not None:
            try:
                if int(rem) <= 0:
                    return True
            except ValueError:
                pass
        if resp.headers.get(self.retry_after_header):
            return True
        try:
            body = resp.text[:300].lower()
        except Exception:
            body = ""
        return "rate limit" in body or "secondary rate" in body

    def wait_after(self, resp: httpx.Response, status: StatusFn = None) -> None:
        """Handle a rate-limited response: sleep for ``Retry-After`` or until the
        window resets. Raises when the wait exceeds the inline budget."""
        wait: Optional[float] = None
        ra = resp.headers.get(self.retry_after_header)
        if ra:
            try:
                wait = float(ra)
            except ValueError:
                wait = None
        if wait is None:
            self.update(resp)
            if self.reset_at:
                wait = self.reset_at - time.time() + 2
        if wait is None:
            wait = 60.0
        if wait > self.max_wait:
            # Defer for the ACTUAL retry window (a short Retry-After from a
            # secondary rate limit), NOT the far-off primary hourly reset — else
            # a 60s throttle would stall the whole crawl for an hour.
            resume_at = time.time() + wait
            raise RateLimitExceeded(
                f"{self.name} rate limited; retry in {int(wait)}s", resume_at)
        self._sleep(wait, status, "throttled")

    def get(self, client: httpx.Client, url: str, *, status: StatusFn = None,
            retries: int = 4, **kw) -> httpx.Response:
        """GET with rate-limit awareness: pre-wait when the quota is low, and on a
        throttled response sleep (bounded) and retry. Non-rate-limit responses are
        returned as-is for the caller to handle."""
        last: Optional[httpx.Response] = None
        for _ in range(max(1, retries)):
            self.before(status)
            resp = client.get(url, **kw)
            self.update(resp)
            if self.is_rate_limited(resp):
                self.wait_after(resp, status)  # sleeps or raises RateLimitExceeded
                last = resp
                continue
            return resp
        raise RateLimitExceeded(
            f"{self.name} rate limit did not clear after {retries} retries",
            self.reset_at if last is not None else None)
