"""Sliding-window rate limiting for the endpoints that cost something.

WHAT IS ACTUALLY BEING PROTECTED

Extraction quota, not CPU. Gemini's free tier allows 20 requests per DAY and is
the only route that can read a scanned invoice, so an unattended script pointed
at the processing endpoint does not merely slow the app down -- it exhausts the
one capability that has no fallback, and every scan afterwards degrades to "we
could not read this". The limiter exists to make that take deliberate effort
rather than a stray `while true` loop.

WHY IN-PROCESS AND NOT REDIS

This is a single-process FastAPI app over one SQLite file. A shared store would
add an operational dependency to solve a problem this deployment does not have.
The honest limitation is written down rather than designed around: run several
workers and each keeps its own counters, so the effective limit multiplies by
the worker count. At that point the counters belong in Redis, and only this
module changes.

Counting is per authenticated user first, and per IP as a second line. Neither
alone is enough: an IP-only limit punishes everyone behind one office NAT, and a
user-only limit does nothing about the unauthenticated login endpoint.
"""
import threading
import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException, Request, Security, status

import auth
import config


class SlidingWindow:
    """Counts events per key inside a moving time window.

    A fixed-bucket counter would allow twice the limit across a boundary -- 20
    at 11:59:59 and 20 more at 12:00:01. Keeping the timestamps costs a little
    memory and removes that hole entirely.
    """

    def __init__(self):
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, window: float = 60.0):
        """(allowed, remaining, retry_after_seconds). Records the hit if allowed."""
        now = time.monotonic()
        cutoff = now - window
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) >= limit:
                retry_after = max(1, int(round(q[0] + window - now)))
                return False, 0, retry_after
            q.append(now)
            if len(self._hits) > 4096:
                self._evict(cutoff)
            return True, limit - len(q), 0

    def _evict(self, cutoff):
        """Drop keys with nothing left in the window. Called under the lock, and
        only when the table has grown, so a burst of one-off keys (a scan across
        many IPs) cannot grow memory without bound."""
        for k in [k for k, q in self._hits.items() if not q or q[-1] <= cutoff]:
            del self._hits[k]

    def reset(self):
        with self._lock:
            self._hits.clear()


limiter = SlidingWindow()


def client_ip(request: Request) -> str:
    """The caller's IP.

    X-Forwarded-For is honoured ONLY when the deployment says it is behind a
    proxy, because the header is client-controlled: trusting it unconditionally
    lets anyone reset their own counter by making one up. Behind a proxy the
    left-most entry is the original client.
    """
    if config.TRUST_PROXY_HEADERS:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _enforce(key: str, limit: int, what: str):
    if not config.RATE_LIMIT_ENABLED:
        return
    allowed, _, retry_after = limiter.check(key, limit)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded for {what}. Try again in {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )


def rate_limit_login(request: Request):
    """Guards the one endpoint that takes a password. Per IP, since by
    definition there is no authenticated identity to count against yet."""
    _enforce(f"login:{client_ip(request)}", config.RATE_LIMIT_LOGIN_PER_MINUTE,
             "sign-in attempts")


def rate_limit_processing(
    request: Request,
    principal: auth.Principal = Security(auth.current_principal, scopes=["invoice:process"]),
) -> auth.Principal:
    """Guards invoice processing: authenticate, authorize, then count.

    Order matters. Authentication runs first so an unauthenticated flood is
    refused with a 401 before it can consume anyone's per-user budget, and so
    the per-user counter is keyed to a verified identity rather than to
    something the caller supplied.

    Both counters are checked: the per-user limit is the real control, the
    per-IP one catches a single host cycling through several accounts.
    """
    _enforce(f"process-ip:{client_ip(request)}", config.RATE_LIMIT_IP_PER_MINUTE,
             "this address")
    _enforce(f"process-user:{principal.username}", config.RATE_LIMIT_PROCESS_PER_MINUTE,
             "invoice processing")
    return principal
