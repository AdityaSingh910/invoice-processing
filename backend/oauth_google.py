"""Google OAuth 2.0 for the Gmail mailbox connection (Phase G2).

WHAT THIS MODULE IS

The credential half of the Gmail integration: the authorization-code flow with
PKCE, the token exchange, the refresh, the revoke, and the encryption that
keeps a refresh token from sitting in the database in the clear.

It is deliberately SEPARATE from `email_provider.py`, which is the module that
knows a mailbox exists. That one fetches messages; this one answers "may we,
and with what". Keeping them apart is what makes it possible to say that the
IMAP provider is untouched by any of this, and to test the whole credential
lifecycle without a mailbox anywhere in sight.

THREE THINGS THIS MODULE WILL NOT DO

1. **It will not put a token in a log line, an exception message, or an API
   response.** Google's error bodies are parsed for their short `error` CODE
   (`invalid_grant`, `invalid_client`) and nothing else is ever carried
   forward -- not the body, not the description, not the request that produced
   it. `_scrub()` is applied to every message that can reach a caller, as the
   second line of defence rather than the first.
2. **It will not accept an endpoint from configuration.** Google's addresses
   are constants in `config.py`. An operator who could repoint the token
   endpoint could collect an authorization code and the refresh token it buys.
3. **It will not treat "we could not check" as "it failed".** The same
   three-state discipline Phase F applies to SPF and DKIM (§7a.4) applies here:
   a network error refreshing a token is transient and leaves the connection
   CONNECTED with an error noted, while `invalid_grant` -- Google saying the
   grant is gone -- is the one answer that moves it to REVOKED. Treating a DNS
   blip as a revocation would disconnect a working mailbox and make an
   administrator walk the consent screen again for nothing.

WHY urllib AND NOT httpx OR THE GOOGLE SDK

`httpx` is in requirements.txt as a TEST dependency (it is what
fastapi.testclient needs), and `google-auth`/`google-api-python-client` are not
present at all. `email_provider.py` built a working IMAP client on the
standard library's `imaplib` rather than take a dependency, and this follows
it: four HTTPS calls -- authorize, exchange, refresh, revoke -- do not justify
a new package in a deployment's supply chain, particularly not one that would
be handling the most sensitive credential in the application.

WHERE THE ENCRYPTION KEY COMES FROM

Derived by HKDF-SHA256 from `AUTH_SECRET`, the secret this deployment already
has to set and protect -- production refuses to start without it (§8).

A separate `GMAIL_TOKEN_KEY` was considered and rejected. It would add a second
mandatory secret, and therefore a second way to misconfigure a deployment,
without adding security: both would live in the same environment, so anything
able to read one can read the other. What it WOULD add is a new failure mode
where half the secrets were rotated.

The consequence is real and is stated rather than hidden: **rotating
AUTH_SECRET makes the stored Gmail tokens undecryptable.** That is already the
rotation's semantics for every session in the application, it fails closed
(the connection reports that it needs reconnecting; nothing falls back to
plaintext), and the remedy is one click of Connect Gmail.
"""
import base64
import hashlib
import hmac
import json
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import config

PROVIDER = "gmail"

# Google is asked for offline access so that ingestion keeps working when
# nobody is signed in -- which is the entire point of a background poller.
_ACCESS_TYPE = "offline"

# An access token is refreshed this many seconds BEFORE Google's stated expiry.
# A token that expires between the check and the call it was fetched for is a
# race that shows up as an intermittent 401 under load and nowhere else.
_EXPIRY_SKEW_SECONDS = 120

_HTTP_TIMEOUT_SECONDS = 30

# A refusal Google expresses as `invalid_grant` is terminal: the refresh token
# has been revoked, has expired, or the account's password changed. It is the
# only error that means "this connection is over" rather than "try again".
_TERMINAL_ERRORS = ("invalid_grant", "unauthorized_client", "invalid_client")


class OAuthError(RuntimeError):
    """An OAuth step failed. Carries a short code, never a token or a body."""

    def __init__(self, message, code=None, terminal=False):
        super().__init__(message)
        self.code = code
        # `terminal` distinguishes "Google says this grant is gone" from "we
        # could not reach Google". Only the first should ever disconnect a
        # mailbox; see the module docstring.
        self.terminal = terminal


class OAuthNotConfigured(OAuthError):
    """No Google OAuth client is configured in this deployment."""


# --------------------------------------------------------------------------
# Encryption at rest
# --------------------------------------------------------------------------
def _fernet():
    """A Fernet built from a key derived from AUTH_SECRET.

    `cryptography` is already a declared runtime dependency -- Phase F uses it
    for real RFC 6376 DKIM verification -- so this adds no package. Fernet is
    used rather than raw AES because it is authenticated (AES-128-CBC plus
    HMAC-SHA256) and versioned, which removes the two things a hand-rolled
    scheme here would most likely get wrong.
    """
    from cryptography.fernet import Fernet

    import auth   # deferred: auth imports config, and this module is imported early
    secret = auth.signing_secret()
    if not secret:
        raise OAuthError("no signing secret is available to encrypt stored tokens")

    # HKDF-SHA256 (RFC 5869) with a fixed info string, so this key is
    # cryptographically separated from the one signing JWTs even though both
    # descend from AUTH_SECRET. Reusing the raw secret as key material for two
    # different primitives is the mistake this avoids.
    salt = b"invoice-processing/oauth-token-encryption/v1"
    prk = hmac.new(salt, secret.encode("utf-8"), hashlib.sha256).digest()
    okm = hmac.new(prk, b"gmail-refresh-token\x01", hashlib.sha256).digest()
    return Fernet(base64.urlsafe_b64encode(okm))


def encrypt_token(token: str) -> str:
    """Ciphertext for storage. `None` in, `None` out -- an absent token is not
    an empty one, and a column of encrypted empty strings would hide that."""
    if token is None:
        return None
    return _fernet().encrypt(str(token).encode("utf-8")).decode("ascii")


def decrypt_token(blob: str) -> str:
    """Plaintext, or a clear failure.

    A blob that will not decrypt is almost always AUTH_SECRET having changed
    since it was written. That is reported as its own condition rather than as
    a generic error, because the remedy -- reconnect the mailbox -- is specific
    and an administrator reading "invalid token" would go looking at Google.
    """
    if blob is None:
        return None
    from cryptography.fernet import InvalidToken
    try:
        return _fernet().decrypt(str(blob).encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise OAuthError(
            "the stored credential could not be decrypted; AUTH_SECRET has most "
            "likely changed since the mailbox was connected. Reconnect Gmail.",
            code="undecryptable", terminal=True) from exc


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def _scrub(text: str) -> str:
    """Last-ditch redaction for anything about to be surfaced or stored.

    Nothing in this module deliberately puts a token in a message. This exists
    because "deliberately" is a claim about today's code, and the cost of being
    wrong once is a refresh token written into a database column an
    administrator can read in the UI.
    """
    if not text:
        return text
    out = str(text)
    for marker in ("refresh_token", "access_token", "client_secret", "id_token",
                   "code_verifier"):
        if marker in out:
            return f"<redacted: response mentioned {marker}>"
    return out[:400]


def _post_form(url: str, fields: dict) -> dict:
    """POST an application/x-www-form-urlencoded body and parse the JSON reply.

    HTTPS is asserted rather than assumed. The endpoints are module constants
    in `config.py` so this cannot currently fail -- which is exactly why it is
    cheap to check, and why it will still hold if someone later makes one of
    them configurable without reading this file.
    """
    if not url.startswith("https://"):
        raise OAuthError("refusing to send credentials over a non-HTTPS endpoint")

    body = urllib.parse.urlencode(
        {k: v for k, v in fields.items() if v is not None}).encode("ascii")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        # Google reports OAuth failures as a 4xx with a JSON body carrying a
        # short, stable `error` code. ONLY that code is kept: the body also
        # carries a description, and a description is free-text that this
        # module would then be persisting and displaying.
        code = None
        try:
            code = (json.loads(exc.read().decode("utf-8") or "{}") or {}).get("error")
        except Exception:
            pass
        code = _scrub(code) if code else None
        raise OAuthError(
            f"Google refused the request ({code or 'HTTP ' + str(exc.code)})",
            code=code, terminal=(code in _TERMINAL_ERRORS)) from None
    except urllib.error.URLError as exc:
        # Could not reach Google. Transient by default -- see the module
        # docstring on why this must not read as a revocation.
        raise OAuthError(
            f"could not reach Google ({exc.__class__.__name__})",
            code="unreachable", terminal=False) from None
    except (ValueError, TypeError) as exc:
        raise OAuthError(f"Google returned an unreadable response "
                         f"({exc.__class__.__name__})", code="malformed") from None


def api_get(url: str, access_token: str) -> dict:
    """An authenticated GET against a Google API, returning parsed JSON.

    The bearer token goes in the Authorization header and never into the URL --
    the same rule the frontend follows for the document preview (§7e.5), and
    for the same reason: a URL is logged by every proxy between here and there.
    """
    if not url.startswith("https://"):
        raise OAuthError("refusing to send a token over a non-HTTPS endpoint")
    request = urllib.request.Request(
        url, method="GET",
        headers={"Authorization": f"Bearer {access_token}",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        code = None
        try:
            payload = json.loads(exc.read().decode("utf-8") or "{}")
            code = ((payload.get("error") or {}).get("status")
                    if isinstance(payload.get("error"), dict) else payload.get("error"))
        except Exception:
            pass
        # 401 means this access token is no longer accepted. That is NOT
        # terminal on its own: the usual cause is expiry, and the refresh token
        # beside it may well still be good. The caller refreshes and retries
        # once; only a refresh failing with invalid_grant ends the connection.
        raise OAuthError(f"Gmail API refused the request (HTTP {exc.code}"
                         f"{': ' + _scrub(code) if code else ''})",
                         code=f"http_{exc.code}", terminal=False) from None
    except urllib.error.URLError as exc:
        raise OAuthError(f"could not reach the Gmail API ({exc.__class__.__name__})",
                         code="unreachable", terminal=False) from None
    except (ValueError, TypeError) as exc:
        raise OAuthError(f"the Gmail API returned an unreadable response "
                         f"({exc.__class__.__name__})", code="malformed") from None


# --------------------------------------------------------------------------
# The authorization-code flow
# --------------------------------------------------------------------------
def _require_client():
    if not config.google_oauth_configured():
        raise OAuthNotConfigured(
            f"Google OAuth is not configured. Set {config.GOOGLE_OAUTH_CLIENT_ID_ENV}, "
            f"{config.GOOGLE_OAUTH_CLIENT_SECRET_ENV} and "
            f"{config.GOOGLE_OAUTH_REDIRECT_URI_ENV}.", code="not_configured")


def new_state() -> str:
    """A CSRF state value. 256 bits from the OS CSPRNG, URL-safe."""
    return secrets.token_urlsafe(32)


def new_code_verifier() -> str:
    """A PKCE verifier (RFC 7636): 43-128 unreserved characters."""
    return secrets.token_urlsafe(64)[:96]


def code_challenge(verifier: str) -> str:
    """S256 challenge: base64url(sha256(verifier)), unpadded."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_authorization_url(state: str, verifier: str, login_hint: str = None) -> str:
    """Where to send the administrator's browser to consent.

    PKCE IS USED EVEN THOUGH THIS IS A CONFIDENTIAL CLIENT. A web-server client
    holds a secret, so PKCE is not strictly required -- it is included because
    it costs one hash and it closes authorization-code interception: a code
    captured from the redirect (a proxy log, browser history, a referrer on a
    misconfigured page) cannot be exchanged without the verifier, which never
    leaves this server.

    `prompt=consent` with `access_type=offline` is what guarantees a refresh
    token. Google returns one only on the FIRST authorization for a given
    client and account otherwise, so a re-connect after a disconnect would
    silently produce a connection that dies at the first token expiry.
    """
    _require_client()
    params = {
        "client_id": config.google_oauth_client_id(),
        "redirect_uri": config.google_oauth_redirect_uri(),
        "response_type": "code",
        "scope": " ".join(config.gmail_scopes()),
        "access_type": _ACCESS_TYPE,
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": code_challenge(verifier),
        "code_challenge_method": "S256",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return config.GOOGLE_AUTH_ENDPOINT + "?" + urllib.parse.urlencode(params)


def exchange_code(code: str, verifier: str, redirect_uri: str) -> dict:
    """Trade an authorization code for tokens.

    `redirect_uri` is the one recorded when the flow STARTED, not the currently
    configured value: Google requires the exchange to present the same URI the
    authorization used, and a deployment whose configuration changed mid-flow
    should get a clean refusal rather than a confusing mismatch.
    """
    _require_client()
    payload = _post_form(config.GOOGLE_TOKEN_ENDPOINT, {
        "code": code,
        "client_id": config.google_oauth_client_id(),
        "client_secret": config.google_oauth_client_secret(),
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    })
    if not payload.get("access_token"):
        raise OAuthError("Google returned no access token", code="no_access_token")
    return payload


def refresh_access_token(refresh_token: str) -> dict:
    """Exchange a refresh token for a fresh access token.

    An `invalid_grant` here is the one signal that the user or Google has ended
    the grant -- revoked in the account's security settings, expired after long
    disuse, or invalidated by a password change. `_post_form` marks it terminal
    and the caller acts on that by disconnecting; every other failure leaves the
    connection alone to be retried on the next poll.
    """
    _require_client()
    payload = _post_form(config.GOOGLE_TOKEN_ENDPOINT, {
        "refresh_token": refresh_token,
        "client_id": config.google_oauth_client_id(),
        "client_secret": config.google_oauth_client_secret(),
        "grant_type": "refresh_token",
    })
    if not payload.get("access_token"):
        raise OAuthError("Google returned no access token on refresh",
                         code="no_access_token", terminal=True)
    return payload


def revoke(token: str) -> bool:
    """Ask Google to invalidate a token. Best effort, and says which it was.

    Returns False rather than raising when Google cannot be reached or has
    already forgotten the token. A disconnect must not be blocked by the remote
    side: the local credential is deleted either way, and a token Google still
    believes in but nobody holds is a smaller problem than a mailbox an
    administrator cannot disconnect.
    """
    if not token:
        return False
    try:
        _post_form(config.GOOGLE_REVOKE_ENDPOINT, {"token": token})
        return True
    except OAuthError as exc:
        print(f"[oauth] revoke did not complete: {exc.code or 'error'}", file=sys.stderr)
        return False


def granted_scopes(payload: dict) -> list:
    return [s for s in (payload.get("scope") or "").split(" ") if s]


def scopes_are_sufficient(scopes) -> bool:
    """Whether what Google actually granted covers what ingestion needs.

    Checked because the consent screen lets a user UNTICK individual scopes.
    A connection that looks successful but was granted nothing usable would
    fail later, at poll time, in a background task nobody is watching -- so it
    is refused at the callback, where there is a person to tell.
    """
    have = set(scopes or [])
    return bool(have & {config.GMAIL_SCOPE_READONLY, config.GMAIL_SCOPE_MODIFY})


def can_modify(scopes) -> bool:
    """Whether the granted scopes permit marking a message read."""
    return config.GMAIL_SCOPE_MODIFY in set(scopes or [])


def expiry_from(payload: dict) -> str:
    """Absolute UTC expiry from Google's relative `expires_in`.

    Stored absolute, in this application's one timestamp spelling, so it
    compares directly against every other timestamp on file rather than needing
    the moment of issue to be remembered alongside it.
    """
    try:
        seconds = int(payload.get("expires_in") or 0)
    except (TypeError, ValueError):
        seconds = 0
    if seconds <= 0:
        seconds = 3600
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def is_expired(expires_at: str) -> bool:
    """True if the token is gone, or close enough that using it is a race."""
    if not expires_at:
        return True
    cutoff = (datetime.now(timezone.utc)
              + timedelta(seconds=_EXPIRY_SKEW_SECONDS)).isoformat()
    return str(expires_at) <= cutoff


def state_expiry() -> str:
    return (datetime.now(timezone.utc)
            + timedelta(seconds=config.OAUTH_STATE_TTL_SECONDS)).isoformat()


# --------------------------------------------------------------------------
# The stored connection
# --------------------------------------------------------------------------
def mailbox_address(access_token: str) -> str:
    """The connected mailbox's own address, via users.getProfile.

    Needs no scope beyond the one already granted, and it is the only identity
    this integration keeps. An OpenID `email` scope would give the same string
    for the price of asking a customer to hand over their profile as well, to
    display a label.

    Failure is not fatal: a connection whose address could not be read is still
    a working connection, and is shown as connected with an unknown address
    rather than refused.
    """
    try:
        profile = api_get(f"{config.GMAIL_API_ROOT}/profile", access_token)
        address = (profile or {}).get("emailAddress")
        return str(address)[:320] if address else None
    except OAuthError:
        return None


def access_token_for(connection: dict) -> tuple:
    """A usable access token for this connection, refreshing if it has to.

    Returns `(token, refreshed)`. The caller uses the flag to decide whether it
    has something new worth persisting -- this function does no database work
    of its own, so that it stays a pure credential operation and the storage
    writes all happen in one place (`gmail_access_token`, below).
    """
    stored = connection.get("access_token_encrypted")
    if stored and not is_expired(connection.get("access_token_expires_at")):
        return decrypt_token(stored), False

    refresh_blob = connection.get("refresh_token_encrypted")
    if not refresh_blob:
        raise OAuthError(
            "this connection has no refresh token, so its access cannot be renewed. "
            "Reconnect Gmail.", code="no_refresh_token", terminal=True)
    payload = refresh_access_token(decrypt_token(refresh_blob))
    return payload["access_token"], payload


def gmail_access_token() -> str:
    """A usable access token for the connected Gmail mailbox.

    THE ONE PLACE THAT REFRESHES AND PERSISTS. Everything that calls the Gmail
    API goes through here, so the refresh happens once, is written down once,
    and a terminal failure disconnects the mailbox once -- rather than each
    call site having to remember all three.

    Raises `OAuthError` when there is no usable credential; the caller turns
    that into whatever its own layer reports (the provider turns it into an
    `EmailProviderError`, which the poller already knows how to surface).
    """
    import storage   # deferred: storage is heavy and this module is imported early

    connection = storage.get_oauth_connection(PROVIDER)
    if not connection:
        raise OAuthError("no Gmail mailbox is connected", code="not_connected",
                         terminal=True)
    if connection.get("status") == storage.OAUTH_REVOKED:
        raise OAuthError("the Gmail authorization has been revoked; reconnect the mailbox",
                         code="revoked", terminal=True)

    try:
        token, refreshed = access_token_for(connection)
    except OAuthError as exc:
        # Only Google saying the grant is gone disconnects the mailbox. A
        # network failure records the reason and leaves it connected, so a
        # transient outage does not cost an administrator a trip through the
        # consent screen (see this module's docstring).
        if exc.terminal:
            storage.set_oauth_status(PROVIDER, storage.OAUTH_REVOKED, _scrub(str(exc)))
        else:
            storage.set_oauth_status(PROVIDER, connection.get("status")
                                     or storage.OAUTH_CONNECTED, _scrub(str(exc)))
        raise

    if refreshed:
        # Google may or may not return a NEW refresh token on a refresh. When
        # it does not, `update_oauth_tokens` keeps the stored one -- passing
        # None here must never blank a working credential.
        storage.update_oauth_tokens(
            PROVIDER,
            encrypt_token(token),
            expiry_from(refreshed),
            encrypt_token(refreshed.get("refresh_token"))
            if refreshed.get("refresh_token") else None)
    return token
