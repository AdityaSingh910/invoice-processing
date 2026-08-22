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


async def rate_limit_login(request: Request):
    """Guards the one endpoint that takes a password.

    TWO COUNTERS, and the second one is the point (Phase K).

    Per IP was the original control, and on its own it is the wrong shape for
    the attack it exists to stop. Password guessing does not have to come from
    one address: a botnet, a VPN pool or a cloud provider's range resets the
    per-IP counter with every single request, so the account being guessed at
    is protected by nothing at all. Counting the TARGET USERNAME as well means
    the account is covered however many sources the attempts arrive from.

    The username is read from the form body rather than from a token, because
    at this point in the request there is no authenticated identity -- that is
    what the caller is trying to obtain. It is used ONLY as a counter key: it
    is lower-cased and truncated so a caller cannot mint unbounded distinct
    keys, it is never trusted for identity, and reading it here does not
    change what `authenticate_user` is later told.

    Not a lockout. The window slides shut on its own, so there is no state an
    attacker can leave behind to keep a colleague locked out -- which is the
    failure mode a "disable the account after N failures" design ships with.
    """
    _enforce(f"login:{client_ip(request)}", config.RATE_LIMIT_LOGIN_PER_MINUTE,
             "sign-in attempts")

    username = await _login_username(request)
    if username:
        _enforce(f"login-user:{username}",
                 config.RATE_LIMIT_LOGIN_PER_USER_PER_MINUTE,
                 "sign-in attempts for this account")


async def _login_username(request: Request) -> str:
    """The username being attempted, for use as a rate-limit key only.

    Returns "" on anything unexpected. A malformed body must fall through to
    the endpoint's own validation and produce its normal 400/422 -- never a
    500 from the limiter, and never an unlimited path around it: the per-IP
    counter above has already been applied either way.
    """
    try:
        form = await request.form()
        raw = form.get("username")
    except Exception:
        return ""
    if not isinstance(raw, str):
        return ""
    return raw.strip().lower()[:120]


def rate_limit_chat(
    request: Request,
    principal: auth.Principal = Security(auth.current_principal, scopes=["invoice:read"]),
) -> auth.Principal:
    """Guards the assistant (Phase K2): authenticate, authorize, then count.

    Chat spends a provider request per question, so it belongs with processing
    rather than with reading -- the thing being protected is the same scarce
    resource, and an unattended script asking questions in a loop is the same
    problem as one uploading in a loop. Same order as `rate_limit_processing`
    for the same reason: authentication first, so an unauthenticated flood is
    refused before it can consume anyone's budget, and the per-user counter is
    keyed to a verified identity rather than to something the caller supplied.

    The daily budget (quota.CHAT) sits behind this as the slower breaker, the
    same pairing extraction already has.
    """
    _enforce(f"chat-ip:{client_ip(request)}", config.RATE_LIMIT_IP_PER_MINUTE,
             "this address")
    _enforce(f"chat-user:{principal.username}", config.RATE_LIMIT_CHAT_PER_MINUTE,
             "assistant questions")
    return principal


def rate_limit_reporting(
    request: Request,
    principal: auth.Principal = Security(auth.current_principal, scopes=["invoice:read"]),
) -> auth.Principal:
    """Guards analytics, log search and the CSV exports (Phase K).

    WHY READS NEEDED A LIMIT AT ALL. Every limiter above this one protects
    either a password or extraction quota, which left the reporting surface --
    added by Phases H and I -- with nothing. Those endpoints are not ordinary
    reads: an export streams up to `logs.MAX_EXPORT_ROWS` rows, and the rule
    and stage filters parse the JSON of every run in the window (both modules
    say so in their own comments). So the cheapest credential in the system, a
    read-only `viewer`, could loop an export and keep the database busy
    indefinitely. Authentication was never the missing control here; a ceiling
    on volume was.

    Per user AND per IP, the same pair the processing limiter uses and for the
    same reasons. The limit is deliberately generous -- a dashboard opening
    several panels at once, or a person paging a log and then exporting it,
    must never see a 429. This bounds automation, not use.

    Returns the principal so an endpoint can depend on this INSTEAD of
    `current_principal` and still receive the caller, without authorising
    anything twice.
    """
    _enforce(f"report-ip:{client_ip(request)}", config.RATE_LIMIT_IP_PER_MINUTE,
             "this address")
    _enforce(f"report-user:{principal.username}", config.RATE_LIMIT_REPORTING_PER_MINUTE,
             "reporting queries")
    return principal


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


def rate_limit_portal(
    request: Request,
    principal: auth.Principal = Security(auth.current_principal, scopes=["portal:read"]),
) -> auth.Principal:
    """Guards the client portal's read surface (Phase J).

    WHY THE PORTAL GETS ITS OWN LIMITER RATHER THAN REUSING THE REPORTING ONE.

    Not because the queries are expensive -- they are not, next to an export
    that streams fifty thousand rows -- but because of WHO makes them. Every
    other limiter in this file counts requests from people inside the company,
    on accounts an administrator provisioned and can watch. These endpoints are
    the only ones reachable by someone outside it, and a shared counter would
    mean one vendor's runaway integration script could exhaust a budget an
    employee also draws on. Keeping them apart means the outside surface can
    misbehave without degrading anything an employee is doing.

    Counted per USER rather than per client id. Two logins belonging to one
    vendor are two people, and one of them leaving a tab refreshing must not
    lock the other out; the per-IP counter beside it is what catches a single
    host cycling through several accounts. The ceiling is generous for the
    reason the reporting limiter records -- a portal screen opening its
    invoice list and its purchase orders at once must never see a 429. This
    bounds automation, not use.
    """
    _enforce(f"portal-ip:{client_ip(request)}", config.RATE_LIMIT_IP_PER_MINUTE,
             "this address")
    _enforce(f"portal-user:{principal.username}", config.RATE_LIMIT_PORTAL_PER_MINUTE,
             "portal requests")
    return principal


def rate_limit_portal_submit(
    request: Request,
    principal: auth.Principal = Security(auth.current_principal, scopes=["portal:submit"]),
) -> auth.Principal:
    """Guards invoice submission through the portal (Phase J).

    The same order and the same pair of counters as `rate_limit_processing`,
    for the same reasons -- authentication before counting, so an
    unauthenticated flood is refused before it can consume anyone's budget.

    The ceiling is much lower than the internal processing one, and that is the
    point. This endpoint drives the full extraction pipeline, which spends the
    SHARED provider budget: an external caller looping it does not merely slow
    the portal down, it exhausts the vision quota that internal invoices also
    need and that has no fallback. The per-minute limit here stops a runaway
    script; `quota.portal_key()` is the slower per-client daily breaker behind
    it, and between them an outside party can spend its own allowance without
    reaching anybody else's.
    """
    _enforce(f"portal-submit-ip:{client_ip(request)}", config.RATE_LIMIT_IP_PER_MINUTE,
             "this address")
    _enforce(f"portal-submit-user:{principal.username}",
             config.RATE_LIMIT_PORTAL_SUBMIT_PER_MINUTE, "invoice submissions")
    return principal


def rate_limit_notify(
    request: Request,
    principal: auth.Principal = Security(auth.current_principal, scopes=["invoice:review"]),
) -> auth.Principal:
    """Guards sending a rejection notification email.

    Scoped to `invoice:review` -- the same authority that accepts or rejects a
    held invoice -- because emailing a vendor on the company's behalf is that
    same kind of act, not an ordinary read. Counted per user and per IP, the
    same pair every other limiter here uses; the ceiling
    (`RATE_LIMIT_NOTIFY_PER_MINUTE`) is deliberately low, because each
    successful call sends a real message to a real vendor.
    """
    _enforce(f"notify-ip:{client_ip(request)}", config.RATE_LIMIT_IP_PER_MINUTE,
             "this address")
    _enforce(f"notify-user:{principal.username}", config.RATE_LIMIT_NOTIFY_PER_MINUTE,
             "rejection notifications")
    return principal


def rate_limit_oauth_callback(request: Request) -> None:
    """Guards the Google OAuth callback (Phase G2). Per IP only -- there is no
    authenticated user here, and that is not an oversight.

    The callback is the one endpoint in this integration that CANNOT require a
    bearer token: Google redirects the administrator's BROWSER to it, and a
    top-level browser navigation carries no Authorization header. Its security
    is the single-use `state` value instead -- 256 bits from the OS CSPRNG,
    bound to the administrator who started the flow, expiring in ten minutes,
    and consumed under a row lock so it can be exchanged at most once.

    What a limiter adds on top is a bound on GUESSING that state. Without one,
    an unauthenticated caller could hammer the callback indefinitely; with it,
    the search space stays astronomically out of reach in any practical time.
    It is per-IP because there is no identity to key it to, and the ceiling is
    the general per-address one -- a real administrator hits this endpoint
    once per connection, so nothing legitimate comes near it.
    """
    _enforce(f"oauth-callback-ip:{client_ip(request)}", config.RATE_LIMIT_IP_PER_MINUTE,
             "this address")
