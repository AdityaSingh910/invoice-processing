"""Phase G2: connecting a real Gmail mailbox by OAuth.

THE CLAIMS UNDER TEST

1. **No password, and no token anywhere it should not be.** Nothing in this
   integration accepts a Gmail password. The refresh token is encrypted at
   rest, is never selected by the endpoint projection, and never appears in a
   response body, a redirect URL, or a log line.
2. **The callback cannot be driven by anyone who did not start the flow.** The
   state is single-use, expiring, provider-bound, and consumed under a row
   lock, and a code presented without a valid one is never exchanged.
3. **Connecting and disconnecting is an ADMINISTRATOR's act.** Every mutating
   endpoint refuses viewer, analyst and reviewer alike.
4. **Gmail is a second door into the SAME pipeline.** A message fetched from
   Gmail goes through Phase F verification, Phase G triage and quarantine, and
   `run_pipeline` -- the same stages, the same audit keys, the same dedup as a
   message fetched over IMAP.
5. **IMAP is untouched.** Everything Phase G did before still does it.

WHERE GOOGLE IS MOCKED, AND WHY THERE

At `oauth_google._post_form` and `oauth_google.api_get` -- the two functions in
the codebase that actually open a socket. Everything above them is real: the
PKCE challenge, the authorization URL, the encryption, the storage, the
provider's paging and cursor arithmetic, the FastAPI endpoints and their
scopes. No test needs a Google account, a client secret, or a network.
"""
import base64
import json
import os
import sys
import time
import urllib.parse

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
TESTS = os.path.dirname(os.path.abspath(__file__))
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)

import config              # noqa: E402
import email_ingest        # noqa: E402
import email_provider      # noqa: E402
import email_triage        # noqa: E402
import extraction          # noqa: E402
import main                # noqa: E402
import oauth_google        # noqa: E402
import ratelimit           # noqa: E402
import storage             # noqa: E402
import pg_schema           # noqa: E402
from conftest import auth_headers                      # noqa: E402
from test_email_ingestion import (VENDOR_DOMAIN, invoice_email,  # noqa: E402
                                  message, pdf_bytes)

CLIENT_ID = "test-client-id.apps.googleusercontent.com"
CLIENT_SECRET = "test-client-secret-value"
REDIRECT_URI = "https://ap.example.com/api/email/oauth/gmail/callback"

# Distinctive values seeded into the fake Google so the no-leak greps below are
# searching for something that genuinely passed through the system.
REFRESH_TOKEN = "refresh-token-SECRETVALUE-1"
ACCESS_TOKEN = "access-token-SECRETVALUE-1"


# ==========================================================================
# The fake Google
# ==========================================================================
class FakeGoogle:
    """Google's token endpoint and the Gmail REST API, in memory.

    Implements enough of both to be worth testing against: the `after:` search
    bound is really applied, paging really pages, and a revoked refresh token
    really produces `invalid_grant` -- so the provider's cursor arithmetic and
    the refresh path are exercised rather than stubbed past.
    """

    def __init__(self):
        self.messages = {}            # id -> (raw bytes, internal_date_ms)
        self.token_requests = []
        self.api_requests = []
        self.revoked = []
        self.live_refresh_tokens = {REFRESH_TOKEN}
        self.access_token = ACCESS_TOKEN
        self.granted_scope = config.GMAIL_SCOPE_READONLY
        self.return_refresh_token = True
        self.expires_in = 3600
        self.exchange_error = None
        self.profile_email = "ap@buyer-corp.example"
        self.page_size = 100
        # Access tokens the API should reject with a 401, so the
        # refresh-and-retry path can be driven for real.
        self.rejected_access_tokens = set()

    # -- helpers ---------------------------------------------------------
    def add_message(self, message_id: str, raw: bytes, internal_date_ms: int):
        self.messages[message_id] = (raw, int(internal_date_ms))

    # -- the token endpoint ----------------------------------------------
    def post_form(self, url, fields):
        if url == config.GOOGLE_REVOKE_ENDPOINT:
            self.revoked.append(fields.get("token"))
            self.live_refresh_tokens.discard(fields.get("token"))
            return {}

        assert url == config.GOOGLE_TOKEN_ENDPOINT, url
        self.token_requests.append(dict(fields))

        if fields.get("grant_type") == "authorization_code":
            if self.exchange_error:
                raise oauth_google.OAuthError(
                    f"Google refused the request ({self.exchange_error})",
                    code=self.exchange_error,
                    terminal=self.exchange_error in ("invalid_grant", "invalid_client"))
            payload = {"access_token": self.access_token,
                       "expires_in": self.expires_in,
                       "scope": self.granted_scope,
                       "token_type": "Bearer"}
            if self.return_refresh_token:
                payload["refresh_token"] = REFRESH_TOKEN
            return payload

        if fields.get("grant_type") == "refresh_token":
            if fields.get("refresh_token") not in self.live_refresh_tokens:
                # Exactly what Google returns for a revoked, expired or
                # password-invalidated grant.
                raise oauth_google.OAuthError(
                    "Google refused the request (invalid_grant)",
                    code="invalid_grant", terminal=True)
            self.access_token = self.access_token + "+"
            return {"access_token": self.access_token,
                    "expires_in": self.expires_in,
                    "scope": self.granted_scope,
                    "token_type": "Bearer"}

        raise AssertionError(f"unexpected grant_type {fields.get('grant_type')!r}")

    # -- the Gmail API ---------------------------------------------------
    def api_get(self, url, access_token):
        self.api_requests.append((url, access_token))
        if access_token in self.rejected_access_tokens:
            raise oauth_google.OAuthError("Gmail API refused the request (HTTP 401)",
                                          code="http_401", terminal=False)

        parsed = urllib.parse.urlparse(url)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        path = parsed.path.split("/users/me", 1)[-1]

        if path == "/profile":
            return {"emailAddress": self.profile_email, "messagesTotal": len(self.messages)}

        if path == "/messages":
            after = self._after_bound(params.get("q", ""))
            # Gmail returns newest first.
            ordered = sorted(self.messages.items(), key=lambda kv: -kv[1][1])
            selected = [mid for mid, (_, when) in ordered if when >= after * 1000]
            start = int(params.get("pageToken") or 0)
            page = selected[start:start + self.page_size]
            out = {"messages": [{"id": m} for m in page]}
            if start + self.page_size < len(selected):
                out["nextPageToken"] = str(start + self.page_size)
            return out

        if path.startswith("/messages/"):
            message_id = path.rsplit("/", 1)[-1]
            if message_id not in self.messages:
                raise oauth_google.OAuthError("Gmail API refused the request (HTTP 404)",
                                              code="http_404", terminal=False)
            raw, when = self.messages[message_id]
            return {"id": message_id,
                    "internalDate": str(when),
                    "raw": base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")}

        raise AssertionError(f"unexpected Gmail API path {path!r}")

    @staticmethod
    def _after_bound(query: str) -> int:
        for token in (query or "").split():
            if token.startswith("after:"):
                try:
                    return int(token.split(":", 1)[1])
                except ValueError:
                    return 0
        return 0


# ==========================================================================
# Fixtures
# ==========================================================================
@pytest.fixture
def db(monkeypatch):
    schema = pg_schema.fresh_schema(monkeypatch)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    email_triage.reload_domain_policy()
    ratelimit.limiter.reset()
    yield schema
    pg_schema.drop_schema(schema)


@pytest.fixture
def oauth_env(db, monkeypatch):
    """A configured Google OAuth client, and a stable AUTH_SECRET.

    AUTH_SECRET is set explicitly because the token encryption key is derived
    from it: without one, `auth.signing_secret()` mints a fresh random secret
    per process and a stored token would be undecryptable on the next run.
    That behaviour has its own test below rather than being papered over here.
    """
    monkeypatch.setenv("AUTH_SECRET", "a-stable-test-signing-secret-value-32b")
    monkeypatch.setenv(config.GOOGLE_OAUTH_CLIENT_ID_ENV, CLIENT_ID)
    monkeypatch.setenv(config.GOOGLE_OAUTH_CLIENT_SECRET_ENV, CLIENT_SECRET)
    monkeypatch.setenv(config.GOOGLE_OAUTH_REDIRECT_URI_ENV, REDIRECT_URI)
    monkeypatch.delenv(config.GMAIL_SCOPES_ENV, raising=False)
    monkeypatch.delenv(config.EMAIL_PROVIDER_ENV, raising=False)
    monkeypatch.delenv(config.EMAIL_INGEST_ENABLED_ENV, raising=False)
    monkeypatch.delenv(config.GMAIL_BACKFILL_DAYS_ENV, raising=False)
    return True


@pytest.fixture
def google(monkeypatch):
    """Replace the two functions in this codebase that open a socket."""
    fake = FakeGoogle()
    monkeypatch.setattr(oauth_google, "_post_form", fake.post_form)
    monkeypatch.setattr(oauth_google, "api_get", fake.api_get)
    return fake


@pytest.fixture
def client(oauth_env, google):
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def dkim(monkeypatch):
    """A signer whose signatures genuinely verify, reused from Phase G's suite.

    A Gmail message has to clear Phase F on real cryptography exactly as an
    IMAP one does -- an admitted message here is admitted because a signature
    verified, not because a test said so.
    """
    import email_security
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from test_email_security import _sign

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    der = key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    txt = "v=DKIM1; k=rsa; p=" + base64.b64encode(der).decode()
    resolver = email_security.StaticDnsTxtResolver({f"s1._domainkey.{VENDOR_DOMAIN}": txt})
    monkeypatch.setattr(email_security, "resolver_from_config", lambda: resolver)
    return lambda raw: _sign(raw, key, domain=VENDOR_DOMAIN)


@pytest.fixture
def extraction_spy(monkeypatch):
    calls = []
    real = extraction.extract_invoice

    def spy(pdf, *args, **kwargs):
        calls.append(len(pdf) if pdf else 0)
        return real(pdf, *args, **kwargs)

    monkeypatch.setattr(extraction, "extract_invoice", spy)
    monkeypatch.setattr(main.extraction, "extract_invoice", spy)
    return calls


# -- helpers ---------------------------------------------------------------
def connect(client, google=None):
    """Drive the real flow end to end and return the callback's response."""
    started = client.post("/api/email/oauth/gmail/authorize", headers=auth_headers("admin"))
    assert started.status_code == 200, started.text
    state = dict(urllib.parse.parse_qsl(
        urllib.parse.urlparse(started.json()["authorization_url"]).query))["state"]
    return client.get("/api/email/oauth/gmail/callback",
                      params={"code": "auth-code-1", "state": state},
                      follow_redirects=False)


def connected_row():
    return storage.get_oauth_connection("gmail")


# ==========================================================================
# 1. Encryption at rest
# ==========================================================================
def test_a_token_round_trips_through_encryption(oauth_env):
    blob = oauth_google.encrypt_token(REFRESH_TOKEN)
    assert oauth_google.decrypt_token(blob) == REFRESH_TOKEN


def test_the_stored_ciphertext_does_not_contain_the_token(oauth_env):
    """The property that matters: reading the column tells you nothing."""
    blob = oauth_google.encrypt_token(REFRESH_TOKEN)
    assert REFRESH_TOKEN not in blob
    assert "refresh-token" not in blob


def test_encryption_is_not_deterministic(oauth_env):
    """Two encryptions of one token differ, so the column cannot be used as an
    oracle for 'is this the same token as that one'."""
    assert oauth_google.encrypt_token(REFRESH_TOKEN) != oauth_google.encrypt_token(REFRESH_TOKEN)


def test_a_token_encrypted_under_a_different_secret_will_not_decrypt(oauth_env, monkeypatch):
    """Rotating AUTH_SECRET must fail CLOSED -- never fall back to plaintext."""
    blob = oauth_google.encrypt_token(REFRESH_TOKEN)
    monkeypatch.setenv("AUTH_SECRET", "a-completely-different-signing-secret")
    with pytest.raises(oauth_google.OAuthError) as caught:
        oauth_google.decrypt_token(blob)
    assert caught.value.code == "undecryptable"
    # The remedy is specific, and the message says it.
    assert "reconnect" in str(caught.value).lower()


def test_none_is_not_encrypted_into_an_empty_string(oauth_env):
    assert oauth_google.encrypt_token(None) is None
    assert oauth_google.decrypt_token(None) is None


# ==========================================================================
# 2. PKCE and the authorization URL
# ==========================================================================
def test_the_code_challenge_is_the_s256_of_the_verifier(oauth_env):
    import hashlib
    verifier = oauth_google.new_code_verifier()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert oauth_google.code_challenge(verifier) == expected


def test_the_verifier_is_within_the_rfc_7636_length_range(oauth_env):
    for _ in range(20):
        assert 43 <= len(oauth_google.new_code_verifier()) <= 128


def test_state_values_are_unpredictable_and_unique(oauth_env):
    values = {oauth_google.new_state() for _ in range(200)}
    assert len(values) == 200
    assert all(len(v) >= 32 for v in values)


def test_the_authorization_url_asks_for_offline_access_and_pkce(oauth_env):
    verifier = oauth_google.new_code_verifier()
    url = oauth_google.build_authorization_url("state-1", verifier)
    params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    assert url.startswith(config.GOOGLE_AUTH_ENDPOINT)
    assert params["access_type"] == "offline"      # or ingestion dies in an hour
    assert params["prompt"] == "consent"           # or there is no refresh token
    assert params["code_challenge_method"] == "S256"
    assert params["code_challenge"] == oauth_google.code_challenge(verifier)
    assert params["response_type"] == "code"
    assert params["state"] == "state-1"
    assert params["client_id"] == CLIENT_ID


def test_the_verifier_itself_never_goes_to_google(oauth_env):
    """PKCE's entire value: the secret is not in the outbound request."""
    verifier = oauth_google.new_code_verifier()
    url = oauth_google.build_authorization_url("state-1", verifier)
    assert verifier not in url


def test_the_authorization_url_never_carries_the_client_secret(oauth_env):
    url = oauth_google.build_authorization_url("s", oauth_google.new_code_verifier())
    assert CLIENT_SECRET not in url


def test_the_default_scope_is_read_only(oauth_env):
    assert config.gmail_scopes() == [config.GMAIL_SCOPE_READONLY]
    url = oauth_google.build_authorization_url("s", oauth_google.new_code_verifier())
    params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    assert params["scope"] == config.GMAIL_SCOPE_READONLY


@pytest.mark.parametrize("scope", list(config.GMAIL_REFUSED_SCOPES))
def test_a_scope_carrying_send_or_delete_authority_is_refused(oauth_env, monkeypatch, scope):
    """The whole reason this uses the Gmail API rather than IMAP: an invoice
    reader must not be able to ask for delete or send."""
    monkeypatch.setenv(config.GMAIL_SCOPES_ENV, scope)
    with pytest.raises(ValueError):
        config.gmail_scopes()


def test_an_unknown_scope_raises_rather_than_being_dropped(oauth_env, monkeypatch):
    monkeypatch.setenv(config.GMAIL_SCOPES_ENV,
                       "https://www.googleapis.com/auth/drive.readonly")
    with pytest.raises(ValueError):
        config.gmail_scopes()


def test_the_modify_scope_is_permitted_for_a_deployment_that_wants_it(oauth_env, monkeypatch):
    monkeypatch.setenv(config.GMAIL_SCOPES_ENV, config.GMAIL_SCOPE_MODIFY)
    assert config.gmail_scopes() == [config.GMAIL_SCOPE_MODIFY]


# ==========================================================================
# 3. Starting the flow
# ==========================================================================
def test_authorize_returns_a_google_url_and_records_a_pending_state(client):
    response = client.post("/api/email/oauth/gmail/authorize", headers=auth_headers("admin"))
    assert response.status_code == 200
    url = response.json()["authorization_url"]
    assert url.startswith(config.GOOGLE_AUTH_ENDPOINT)
    state = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))["state"]
    # Recorded server-side, bound to the administrator who asked.
    row = storage.consume_pending_authorization(state, "gmail")
    assert row and row["requested_by"] == "test-admin"


def test_authorize_does_not_return_the_state_or_the_verifier(client):
    """A CSRF token handed to client-side JavaScript is one an XSS can read."""
    body = client.post("/api/email/oauth/gmail/authorize",
                       headers=auth_headers("admin")).json()
    assert "state" not in body
    assert "code_verifier" not in body
    assert "verifier" not in json.dumps(body)


def test_authorize_is_refused_when_no_oauth_client_is_configured(client, monkeypatch):
    monkeypatch.delenv(config.GOOGLE_OAUTH_CLIENT_ID_ENV, raising=False)
    response = client.post("/api/email/oauth/gmail/authorize", headers=auth_headers("admin"))
    assert response.status_code == 409
    # Names the variables to set, rather than saying "not configured".
    assert config.GOOGLE_OAUTH_CLIENT_ID_ENV in response.json()["detail"]


def test_a_misconfigured_scope_is_reported_at_authorize_not_at_google(client, monkeypatch):
    monkeypatch.setenv(config.GMAIL_SCOPES_ENV, "https://mail.google.com/")
    response = client.post("/api/email/oauth/gmail/authorize", headers=auth_headers("admin"))
    assert response.status_code == 400


def test_two_authorize_calls_produce_two_different_states(client):
    def state_of(r):
        return dict(urllib.parse.parse_qsl(
            urllib.parse.urlparse(r.json()["authorization_url"]).query))["state"]

    first = client.post("/api/email/oauth/gmail/authorize", headers=auth_headers("admin"))
    second = client.post("/api/email/oauth/gmail/authorize", headers=auth_headers("admin"))
    assert state_of(first) != state_of(second)


# ==========================================================================
# 4. State / CSRF validation
# ==========================================================================
def test_a_callback_with_no_state_is_refused(client):
    response = client.get("/api/email/oauth/gmail/callback",
                          params={"code": "x"}, follow_redirects=False)
    assert response.status_code == 303
    assert "gmail=invalid_state" in response.headers["location"]
    assert connected_row() is None


def test_a_callback_with_a_forged_state_is_refused(client, google):
    response = client.get("/api/email/oauth/gmail/callback",
                          params={"code": "x", "state": "not-a-state-we-issued"},
                          follow_redirects=False)
    assert "gmail=invalid_state" in response.headers["location"]
    assert connected_row() is None
    # And crucially: the code was never exchanged.
    assert google.token_requests == []


def test_a_state_cannot_be_replayed(client, google):
    """The single-use property, driven through the real endpoint twice."""
    started = client.post("/api/email/oauth/gmail/authorize", headers=auth_headers("admin"))
    state = dict(urllib.parse.parse_qsl(
        urllib.parse.urlparse(started.json()["authorization_url"]).query))["state"]

    first = client.get("/api/email/oauth/gmail/callback",
                       params={"code": "auth-code-1", "state": state},
                       follow_redirects=False)
    assert "gmail=connected" in first.headers["location"]

    exchanges = len(google.token_requests)
    second = client.get("/api/email/oauth/gmail/callback",
                        params={"code": "auth-code-1", "state": state},
                        follow_redirects=False)
    assert "gmail=invalid_state" in second.headers["location"]
    # The replay bought nothing: no second exchange happened.
    assert len(google.token_requests) == exchanges


def test_an_expired_state_is_refused(client, google, monkeypatch):
    monkeypatch.setattr(oauth_google, "state_expiry",
                        lambda: "2020-01-01T00:00:00+00:00")
    started = client.post("/api/email/oauth/gmail/authorize", headers=auth_headers("admin"))
    state = dict(urllib.parse.parse_qsl(
        urllib.parse.urlparse(started.json()["authorization_url"]).query))["state"]
    response = client.get("/api/email/oauth/gmail/callback",
                          params={"code": "auth-code-1", "state": state},
                          follow_redirects=False)
    assert "gmail=invalid_state" in response.headers["location"]
    assert google.token_requests == []


def test_a_state_issued_for_another_provider_is_refused(client, google):
    storage.create_pending_authorization(
        state="other-provider-state", provider="outlook", code_verifier="v",
        redirect_uri=REDIRECT_URI, requested_by="test-admin",
        expires_at=oauth_google.state_expiry())
    response = client.get("/api/email/oauth/gmail/callback",
                          params={"code": "x", "state": "other-provider-state"},
                          follow_redirects=False)
    assert "gmail=invalid_state" in response.headers["location"]
    assert google.token_requests == []


def test_a_state_without_a_code_is_refused(client, google):
    started = client.post("/api/email/oauth/gmail/authorize", headers=auth_headers("admin"))
    state = dict(urllib.parse.parse_qsl(
        urllib.parse.urlparse(started.json()["authorization_url"]).query))["state"]
    response = client.get("/api/email/oauth/gmail/callback",
                          params={"state": state}, follow_redirects=False)
    assert "gmail=invalid_state" in response.headers["location"]
    assert google.token_requests == []


def test_the_callback_is_rate_limited_so_the_state_cannot_be_brute_forced(client):
    """The callback is the one endpoint here that cannot require a token, so a
    bound on guessing the state is part of what makes it safe. Asserted rather
    than assumed -- a dependency declared and not wired would look identical
    from the outside until somebody tried."""
    ratelimit.limiter.reset()
    codes = [
        client.get("/api/email/oauth/gmail/callback",
                   params={"code": "x", "state": f"guess-{n}"},
                   follow_redirects=False).status_code
        for n in range(config.RATE_LIMIT_IP_PER_MINUTE + 3)
    ]
    assert 429 in codes
    # Every attempt before the limit was refused on its state, not accepted.
    assert set(codes) <= {303, 429}
    assert connected_row() is None
    ratelimit.limiter.reset()


def test_consuming_a_state_is_atomic_and_single_use(oauth_env):
    """Asserted at the storage layer too, because this is the lock that makes
    the endpoint-level property above true."""
    storage.create_pending_authorization(
        state="single-use", provider="gmail", code_verifier="v",
        redirect_uri=REDIRECT_URI, requested_by="admin",
        expires_at=oauth_google.state_expiry())
    assert storage.consume_pending_authorization("single-use", "gmail") is not None
    assert storage.consume_pending_authorization("single-use", "gmail") is None


# ==========================================================================
# 5. A successful callback
# ==========================================================================
def test_a_successful_callback_connects_the_mailbox(client, google):
    response = connect(client)
    assert response.status_code == 303
    assert "gmail=connected" in response.headers["location"]

    row = connected_row()
    assert row["status"] == "CONNECTED"
    assert row["email_address"] == "ap@buyer-corp.example"
    assert row["scopes"] == config.GMAIL_SCOPE_READONLY
    assert row["connected_by"] == "test-admin"


def test_the_exchange_sends_the_verifier_and_the_secret_to_google(client, google):
    connect(client)
    exchange = google.token_requests[0]
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["client_secret"] == CLIENT_SECRET
    assert exchange["redirect_uri"] == REDIRECT_URI
    assert exchange["code_verifier"]          # PKCE completed


def test_the_stored_refresh_token_is_encrypted_not_plaintext(client, google):
    connect(client)
    row = connected_row()
    assert row["refresh_token_encrypted"]
    assert REFRESH_TOKEN not in row["refresh_token_encrypted"]
    assert oauth_google.decrypt_token(row["refresh_token_encrypted"]) == REFRESH_TOKEN


def test_the_redirect_carries_no_token_or_code(client, google):
    """Everything in an address bar reaches browser history and every proxy log."""
    location = connect(client).headers["location"]
    assert REFRESH_TOKEN not in location
    assert ACCESS_TOKEN not in location
    assert "auth-code-1" not in location
    assert location == "/?gmail=connected"


def test_the_redirect_target_is_relative_so_it_cannot_be_an_open_redirect(client, google):
    assert connect(client).headers["location"].startswith("/?")


def test_the_cursor_starts_at_connect_time_not_at_the_beginning_of_the_mailbox(client, google):
    """Connecting must not ingest and rule on years of already-handled invoices."""
    import time
    before = int(time.time() * 1000)
    connect(client)
    cursor = connected_row()["cursor_internal_date"]
    assert cursor >= before - 5000


def test_the_backfill_setting_moves_the_starting_point_back(client, google, monkeypatch):
    import time
    monkeypatch.setenv(config.GMAIL_BACKFILL_DAYS_ENV, "7")
    connect(client)
    cursor = connected_row()["cursor_internal_date"]
    assert cursor <= int((time.time() - 6 * 86400) * 1000)


def test_connecting_really_starts_the_background_poller(client, google):
    """NOT monkeypatched, deliberately -- and the first version of this test WAS,
    which is exactly why it passed against a bug.

    `start_poller()` used to reach for the current event loop. That worked from
    the FastAPI startup handler, which runs on the loop, and silently did
    nothing from the OAuth callback, because a sync path operation runs in a
    worker thread where there is no running loop. Connecting a mailbox would
    have left the badge saying Connected while nothing polled it until the next
    restart. A test that stubs `start_poller` cannot see any of that.
    """
    email_ingest.stop_poller()
    assert email_ingest.poller_running() is False
    connect(client)
    # Scheduled onto the app's loop from a worker thread, so it may not have
    # been created by the time the redirect came back -- give the loop a moment
    # to run the callback it was handed.
    for _ in range(50):
        if email_ingest.poller_running():
            break
        time.sleep(0.02)
    assert email_ingest.poller_running() is True, \
        "connecting a mailbox must actually start polling it"
    email_ingest.stop_poller()


def test_the_poller_is_not_started_when_there_is_no_mailbox(client, google):
    """The same call, with nothing connected, must do nothing at all."""
    email_ingest.stop_poller()
    assert email_ingest.start_poller() is False
    assert email_ingest.poller_running() is False


def test_starting_the_poller_twice_does_not_run_two(client, google):
    connect(client)
    for _ in range(50):
        if email_ingest.poller_running():
            break
        time.sleep(0.02)
    first = email_ingest._poller_task
    email_ingest.start_poller()
    time.sleep(0.05)
    assert email_ingest._poller_task is first
    email_ingest.stop_poller()


def test_reconnecting_replaces_rather_than_accumulating(client, google):
    connect(client)
    google.profile_email = "invoices@buyer-corp.example"
    connect(client)
    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) n FROM email_oauth_connections")
            assert cur.fetchone()["n"] == 1
    finally:
        conn.close()
    assert connected_row()["email_address"] == "invoices@buyer-corp.example"


# ==========================================================================
# 6. Rejected and invalid callbacks
# ==========================================================================
def test_a_user_pressing_cancel_is_reported_as_denied(client, google):
    response = client.get("/api/email/oauth/gmail/callback",
                          params={"error": "access_denied", "state": "x"},
                          follow_redirects=False)
    assert "gmail=denied" in response.headers["location"]
    assert connected_row() is None
    assert google.token_requests == []


def test_a_failed_token_exchange_does_not_connect_anything(client, google):
    google.exchange_error = "invalid_grant"
    response = connect(client)
    assert "gmail=exchange_failed" in response.headers["location"]
    assert connected_row() is None


def test_googles_error_text_is_never_reflected_into_the_url(client, google):
    google.exchange_error = "invalid_grant"
    location = connect(client).headers["location"]
    assert "invalid_grant" not in location
    # Only the fixed vocabulary reaches the browser.
    assert location.split("gmail=")[1] in main._GMAIL_CALLBACK_RESULTS


def test_an_authorization_granted_without_a_usable_scope_is_refused(client, google):
    """The consent screen lets a user untick permissions. A connection granted
    nothing usable must fail HERE, where there is a person to tell -- not later
    inside a background poll."""
    google.granted_scope = "https://www.googleapis.com/auth/userinfo.email"
    response = connect(client)
    assert "gmail=insufficient_scope" in response.headers["location"]
    assert connected_row() is None
    # And the useless grant was handed back rather than left live.
    assert google.revoked


def test_an_authorization_with_no_refresh_token_is_refused(client, google):
    """A connection that works for one hour and then silently stops is worse
    than one that plainly did not connect."""
    google.return_refresh_token = False
    response = connect(client)
    assert "gmail=no_refresh_token" in response.headers["location"]
    assert connected_row() is None
    assert google.revoked


def test_a_storage_failure_hands_the_grant_back_to_google(client, google, monkeypatch):
    """Storing the tokens in the clear is not an available fallback."""
    def boom(**kwargs):
        raise RuntimeError("database is on fire")

    monkeypatch.setattr(storage, "save_oauth_connection", boom)
    response = connect(client)
    assert "gmail=exchange_failed" in response.headers["location"]
    assert REFRESH_TOKEN in google.revoked


@pytest.mark.parametrize("result", list(main._GMAIL_CALLBACK_RESULTS))
def test_every_advertised_callback_result_is_a_relative_redirect(result):
    response = main._gmail_redirect(result)
    assert response.headers["location"] == f"/?gmail={result}"
    assert response.status_code == 303


def test_an_unrecognised_result_word_cannot_be_injected(client):
    """The vocabulary is closed, so nothing arbitrary reaches the address bar."""
    response = main._gmail_redirect("javascript:alert(1)")
    assert response.headers["location"] == "/?gmail=exchange_failed"


# ==========================================================================
# 7. Token refresh
# ==========================================================================
def test_a_live_access_token_is_reused_without_calling_google(client, google):
    connect(client)
    google.token_requests.clear()
    assert oauth_google.gmail_access_token() == ACCESS_TOKEN
    assert google.token_requests == []


def test_an_expired_access_token_is_refreshed(client, google):
    connect(client)
    storage.update_oauth_tokens("gmail",
                                oauth_google.encrypt_token("stale-token"),
                                "2020-01-01T00:00:00+00:00")
    google.token_requests.clear()

    token = oauth_google.gmail_access_token()
    assert token != "stale-token"
    assert google.token_requests[0]["grant_type"] == "refresh_token"
    # And the new one was written down, so the next call does not refresh again.
    row = connected_row()
    assert oauth_google.decrypt_token(row["access_token_encrypted"]) == token


def test_a_token_about_to_expire_is_refreshed_early(client, google):
    """A token that expires between the check and the call it was fetched for
    is a race that only shows up under load."""
    from datetime import datetime, timedelta, timezone
    connect(client)
    soon = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    storage.update_oauth_tokens("gmail", oauth_google.encrypt_token("nearly-stale"), soon)
    google.token_requests.clear()
    assert oauth_google.gmail_access_token() != "nearly-stale"
    assert google.token_requests[0]["grant_type"] == "refresh_token"


def test_a_refresh_that_returns_no_new_refresh_token_keeps_the_stored_one(client, google):
    """Google usually does not reissue one. Blanking it would kill the
    connection at the following expiry."""
    connect(client)
    storage.update_oauth_tokens("gmail", oauth_google.encrypt_token("stale"),
                                "2020-01-01T00:00:00+00:00")
    oauth_google.gmail_access_token()
    row = connected_row()
    assert oauth_google.decrypt_token(row["refresh_token_encrypted"]) == REFRESH_TOKEN


def test_a_successful_refresh_clears_a_previous_error(client, google):
    connect(client)
    storage.set_oauth_status("gmail", storage.OAUTH_CONNECTED, "a transient failure")
    storage.update_oauth_tokens("gmail", oauth_google.encrypt_token("stale"),
                                "2020-01-01T00:00:00+00:00")
    oauth_google.gmail_access_token()
    assert connected_row()["last_error"] is None


# ==========================================================================
# 8. Revoked and expired authorization
# ==========================================================================
def test_a_revoked_grant_marks_the_connection_revoked(client, google):
    connect(client)
    google.live_refresh_tokens.clear()          # the user revoked it at Google
    storage.update_oauth_tokens("gmail", oauth_google.encrypt_token("stale"),
                                "2020-01-01T00:00:00+00:00")

    with pytest.raises(oauth_google.OAuthError) as caught:
        oauth_google.gmail_access_token()
    assert caught.value.terminal is True
    assert connected_row()["status"] == "REVOKED"


def test_a_network_failure_does_NOT_revoke_a_working_connection(client, google, monkeypatch):
    """The three-state discipline Phase F applies to SPF and DKIM, applied to a
    credential: "we could not check" is not "it failed"."""
    connect(client)
    storage.update_oauth_tokens("gmail", oauth_google.encrypt_token("stale"),
                                "2020-01-01T00:00:00+00:00")

    def unreachable(url, fields):
        raise oauth_google.OAuthError("could not reach Google (URLError)",
                                      code="unreachable", terminal=False)

    monkeypatch.setattr(oauth_google, "_post_form", unreachable)
    with pytest.raises(oauth_google.OAuthError):
        oauth_google.gmail_access_token()

    row = connected_row()
    assert row["status"] == "CONNECTED"           # still connected
    assert "reach Google" in row["last_error"]    # but the reason is recorded


def test_a_revoked_connection_is_refused_before_any_call_is_made(client, google):
    connect(client)
    storage.set_oauth_status("gmail", storage.OAUTH_REVOKED, "revoked")
    google.token_requests.clear()
    with pytest.raises(oauth_google.OAuthError) as caught:
        oauth_google.gmail_access_token()
    assert caught.value.code == "revoked"
    assert google.token_requests == []


def test_building_a_provider_on_a_revoked_connection_fails_clearly(client, google):
    connect(client)
    storage.set_oauth_status("gmail", storage.OAUTH_REVOKED, "revoked")
    with pytest.raises(email_provider.EmailProviderError) as caught:
        email_provider.build_gmail_provider()
    assert "revoked" in str(caught.value).lower()


def test_a_poll_against_a_revoked_mailbox_is_an_error_not_an_empty_poll(client, google):
    connect(client)
    google.live_refresh_tokens.clear()
    storage.update_oauth_tokens("gmail", oauth_google.encrypt_token("stale"),
                                "2020-01-01T00:00:00+00:00")
    result = email_ingest.poll_once(provider=email_provider.build_gmail_provider())
    assert result["ok"] is False
    assert result["fetched"] == 0


def test_the_api_refreshes_and_retries_once_after_a_401(client, google):
    """A token can be rejected while our own clock still believes in it."""
    connect(client)
    google.rejected_access_tokens.add(ACCESS_TOKEN)
    provider = email_provider.build_gmail_provider()
    # The refresh produces a new token the fake accepts, so this succeeds --
    # having genuinely gone through the 401 -> refresh -> retry path.
    assert provider.fetch(5) == []
    assert any(r["grant_type"] == "refresh_token" for r in google.token_requests)


def test_a_second_401_after_a_genuine_refresh_gives_up(client, google, monkeypatch):
    """Retried exactly ONCE. Looping on a revoked grant would turn a dead
    mailbox into a request flood."""
    connect(client)

    calls = []

    def always_401(url, access_token):
        calls.append(url)
        raise oauth_google.OAuthError("Gmail API refused the request (HTTP 401)",
                                      code="http_401", terminal=False)

    provider = email_provider.build_gmail_provider()
    monkeypatch.setattr(oauth_google, "api_get", always_401)
    with pytest.raises(email_provider.EmailProviderError):
        provider.fetch(5)
    # Two attempts at the same call, and no third.
    assert len(calls) == 2


# ==========================================================================
# 9. Connect / disconnect
# ==========================================================================
def test_disconnect_revokes_at_google_and_deletes_the_credential(client, google):
    connect(client)
    response = client.post("/api/email/oauth/gmail/disconnect", headers=auth_headers("admin"))
    assert response.status_code == 200
    assert response.json()["disconnected"] is True
    assert response.json()["revoked_at_google"] is True
    assert REFRESH_TOKEN in google.revoked
    assert connected_row() is None


def test_disconnect_deletes_locally_even_when_google_is_unreachable(client, google, monkeypatch):
    """An administrator must never be unable to disconnect a mailbox."""
    connect(client)

    def unreachable(url, fields):
        raise oauth_google.OAuthError("could not reach Google", code="unreachable")

    monkeypatch.setattr(oauth_google, "_post_form", unreachable)
    response = client.post("/api/email/oauth/gmail/disconnect", headers=auth_headers("admin"))
    assert response.status_code == 200
    assert response.json()["revoked_at_google"] is False
    assert connected_row() is None
    # And says so, because the grant may survive in the Google account.
    assert "myaccount.google.com" in response.json()["notice"]


def test_disconnect_with_nothing_connected_is_a_404(client):
    response = client.post("/api/email/oauth/gmail/disconnect", headers=auth_headers("admin"))
    assert response.status_code == 404


def test_disconnect_really_stops_the_poller(client, google):
    """Not stubbed, for the reason the start-poller test records: asserting
    that `stop_poller` was CALLED proves only that a call happened."""
    connect(client)
    for _ in range(50):
        if email_ingest.poller_running():
            break
        time.sleep(0.02)
    assert email_ingest.poller_running() is True

    client.post("/api/email/oauth/gmail/disconnect", headers=auth_headers("admin"))
    assert email_ingest.poller_running() is False
    assert email_ingest.ingestion_configured() is False


def test_disconnect_leaves_an_imap_poller_alone(client, google, monkeypatch):
    """A deployment configured for IMAP that also had Gmail connected keeps
    polling IMAP -- disconnecting Gmail is not a global off switch."""
    connect(client)
    monkeypatch.setenv(config.EMAIL_PROVIDER_ENV, "imap")
    monkeypatch.setenv(config.EMAIL_INGEST_ENABLED_ENV, "1")
    client.post("/api/email/oauth/gmail/disconnect", headers=auth_headers("admin"))
    # Still something to read, so the poller is not stopped.
    assert email_ingest.ingestion_configured() is True
    email_ingest.stop_poller()


def test_an_undecryptable_credential_can_still_be_disconnected(client, google, monkeypatch):
    """Otherwise rotating AUTH_SECRET would strand a connection forever."""
    connect(client)
    monkeypatch.setenv("AUTH_SECRET", "a-different-secret-entirely-now")
    response = client.post("/api/email/oauth/gmail/disconnect", headers=auth_headers("admin"))
    assert response.status_code == 200
    assert connected_row() is None


# ==========================================================================
# 10. Authorization enforcement
# ==========================================================================
GMAIL_ENDPOINTS = [
    ("GET", "/api/email/oauth/gmail/status"),
    ("POST", "/api/email/oauth/gmail/authorize"),
    ("POST", "/api/email/oauth/gmail/disconnect"),
]


@pytest.mark.parametrize("method,path", GMAIL_ENDPOINTS)
def test_every_gmail_endpoint_refuses_an_anonymous_caller(client, method, path):
    assert client.request(method, path).status_code == 401


@pytest.mark.parametrize("method,path", GMAIL_ENDPOINTS)
@pytest.mark.parametrize("role", ["viewer", "analyst", "reviewer"])
def test_only_an_administrator_may_manage_the_mailbox(client, method, path, role):
    """Connecting a mailbox is an administrative act. A reviewer approving
    invoices all day is still not the person who chooses which mailbox the
    company reads."""
    assert client.request(method, path, headers=auth_headers(role)).status_code == 403


@pytest.mark.parametrize("method,path", GMAIL_ENDPOINTS)
def test_a_client_portal_token_reaches_none_of_them(client, method, path):
    """Phase J's boundary, re-asserted for the routes this phase adds."""
    assert client.request(method, path,
                          headers=auth_headers("client")).status_code == 403


@pytest.mark.parametrize("method,path", GMAIL_ENDPOINTS)
def test_a_forged_token_is_refused(client, method, path):
    headers = {"Authorization": "Bearer not.a.real.token"}
    assert client.request(method, path, headers=headers).status_code == 401


def test_a_disabled_administrator_cannot_connect_a_mailbox(client, google, monkeypatch, tmp_path):
    """Phase K's live account re-check, on this phase's most sensitive route."""
    import auth
    store = tmp_path / "users.json"
    store.write_text(json.dumps([{
        "username": "gone", "roles": ["admin"], "disabled": True,
        "password_hash": auth.hash_password("x"),
    }]), encoding="utf-8")
    monkeypatch.setenv("AUTH_USERS_FILE", str(store))
    response = client.post("/api/email/oauth/gmail/authorize",
                           headers=auth_headers("admin", username="gone"))
    assert response.status_code == 401


def test_the_status_endpoint_is_readable_by_an_administrator(client, google):
    connect(client)
    body = client.get("/api/email/oauth/gmail/status",
                      headers=auth_headers("admin")).json()
    assert body["connection"]["status"] == "CONNECTED"
    assert body["connection"]["email_address"] == "ap@buyer-corp.example"
    assert body["oauth_configured"] is True


# ==========================================================================
# 11. Nothing leaks a token
# ==========================================================================
def test_the_status_endpoint_never_returns_a_token(client, google):
    connect(client)
    body = client.get("/api/email/oauth/gmail/status", headers=auth_headers("admin")).text
    for secret in (REFRESH_TOKEN, ACCESS_TOKEN, CLIENT_SECRET):
        assert secret not in body
    assert "refresh_token_encrypted" not in body
    assert "access_token_encrypted" not in body


def test_the_ingestion_endpoint_never_returns_a_token(client, google):
    connect(client)
    body = client.get("/api/email/ingestion", headers=auth_headers("admin")).text
    for secret in (REFRESH_TOKEN, ACCESS_TOKEN, CLIENT_SECRET):
        assert secret not in body


def test_the_public_projection_reports_only_whether_a_refresh_token_exists(client, google):
    connect(client)
    public = storage.public_oauth_connection("gmail")
    assert public["has_refresh_token"] is True
    assert "refresh_token_encrypted" not in public
    assert "access_token_encrypted" not in public


def test_the_provider_description_carries_no_token(client, google):
    connect(client)
    described = json.dumps(email_provider.build_gmail_provider().describe())
    for secret in (REFRESH_TOKEN, ACCESS_TOKEN, CLIENT_SECRET):
        assert secret not in described
    # It reports only that a credential is present, exactly as IMAP's does.
    assert json.loads(described)["credential_configured"] is True


def test_the_scrubber_catches_a_token_shaped_string(oauth_env):
    assert "redacted" in oauth_google._scrub('{"refresh_token": "abc"}')
    assert "redacted" in oauth_google._scrub('{"access_token": "abc"}')
    assert "redacted" in oauth_google._scrub('{"client_secret": "abc"}')
    # And leaves an ordinary error code alone.
    assert oauth_google._scrub("invalid_grant") == "invalid_grant"


def test_a_recorded_error_is_a_code_not_a_response_body(client, google, monkeypatch):
    connect(client)
    storage.update_oauth_tokens("gmail", oauth_google.encrypt_token("stale"),
                                "2020-01-01T00:00:00+00:00")

    def leaky(url, fields):
        raise oauth_google.OAuthError(
            f'Google said {{"refresh_token": "{REFRESH_TOKEN}"}}', code="weird")

    monkeypatch.setattr(oauth_google, "_post_form", leaky)
    with pytest.raises(oauth_google.OAuthError):
        oauth_google.gmail_access_token()
    assert REFRESH_TOKEN not in (connected_row()["last_error"] or "")


# ==========================================================================
# 12. Retrieving messages from Gmail
# ==========================================================================
def test_a_message_is_fetched_with_its_raw_bytes_intact(client, google, dkim):
    """Byte-exactness matters more here than anywhere: Phase F verifies DKIM
    over these bytes, and one re-encoded header turns a good signature into a
    failed one."""
    connect(client)
    raw = dkim(invoice_email())
    google.add_message("gmail-msg-1", raw, 1_800_000_000_000)

    fetched = email_provider.build_gmail_provider().fetch(10)
    assert len(fetched) == 1
    assert fetched[0].raw == raw
    assert fetched[0].provider == "gmail"
    assert fetched[0].provider_message_id == "gmail-msg-1"


def test_messages_are_returned_oldest_first(client, google):
    """A backlog must drain in arrival order, or advancing the cursor strands
    everything older than the newest batch."""
    connect(client)
    storage.advance_oauth_cursor("gmail", 1)
    for index, when in enumerate([1_800_000_003_000, 1_800_000_001_000,
                                  1_800_000_002_000]):
        google.add_message(f"m{index}", message(subject=f"Invoice INV-{index}"), when)

    fetched = email_provider.build_gmail_provider().fetch(10)
    assert [m.provider_message_id for m in fetched] == ["m1", "m2", "m0"]


def test_only_messages_after_the_cursor_are_listed(client, google):
    connect(client)
    google.add_message("old", message(subject="Invoice OLD"), 1_000_000_000_000)
    google.add_message("new", message(subject="Invoice NEW"), 2_000_000_000_000)
    storage.advance_oauth_cursor("gmail", 1_500_000_000_000)

    fetched = email_provider.build_gmail_provider().fetch(10)
    assert [m.provider_message_id for m in fetched] == ["new"]


def test_the_cursor_reads_a_short_overlap_behind_itself(client, google, monkeypatch):
    """A message delivered slightly out of order must still be seen."""
    monkeypatch.setenv(config.GMAIL_CURSOR_OVERLAP_ENV, "600")
    connect(client)
    storage.advance_oauth_cursor("gmail", 1_800_000_000_000)
    # 5 minutes BEFORE the mark -- inside the 10 minute overlap.
    google.add_message("slightly-late", message(subject="Invoice LATE"),
                       1_800_000_000_000 - 300_000)
    fetched = email_provider.build_gmail_provider().fetch(10)
    assert [m.provider_message_id for m in fetched] == ["slightly-late"]


def test_the_cursor_only_moves_forward(client, google):
    connect(client)
    storage.advance_oauth_cursor("gmail", 2_000_000_000_000)
    storage.advance_oauth_cursor("gmail", 1_000_000_000_000)
    assert connected_row()["cursor_internal_date"] == 2_000_000_000_000


def test_mark_handled_advances_the_cursor(client, google):
    connect(client)
    raw = message(subject="Invoice INV-1")
    google.add_message("m1", raw, 1_900_000_000_000)
    provider = email_provider.build_gmail_provider()
    fetched = provider.fetch(10)
    provider.mark_handled(fetched[0])
    assert connected_row()["cursor_internal_date"] == 1_900_000_000_000


def test_an_already_ingested_id_is_not_downloaded_again(client, google, dkim):
    """The overlap re-offers ids every poll. Paying a full message download for
    each of them is the difference between the overlap costing a few ids and
    costing the whole mailbox every two minutes."""
    connect(client)
    raw = dkim(invoice_email())
    google.add_message("gmail-dup", raw, 1_800_000_000_000)
    storage.advance_oauth_cursor("gmail", 1)

    email_ingest.poll_once(provider=email_provider.build_gmail_provider())
    downloads = [u for u, _ in google.api_requests if "/messages/gmail-dup" in u]
    assert len(downloads) == 1

    google.api_requests.clear()
    email_ingest.poll_once(provider=email_provider.build_gmail_provider())
    assert [u for u, _ in google.api_requests if "/messages/gmail-dup" in u] == []


def test_listing_walks_several_pages(client, google):
    connect(client)
    storage.advance_oauth_cursor("gmail", 1)
    google.page_size = 2
    for index in range(5):
        google.add_message(f"p{index}", message(subject=f"Invoice {index}"),
                           1_800_000_000_000 + index * 1000)
    fetched = email_provider.build_gmail_provider().fetch(10)
    assert len(fetched) == 5


def test_paging_is_bounded_so_one_poll_cannot_run_forever(client, google, monkeypatch):
    connect(client)
    storage.advance_oauth_cursor("gmail", 1)
    google.page_size = 1
    for index in range(config.GMAIL_MAX_LIST_PAGES + 5):
        google.add_message(f"b{index}", message(subject=f"Invoice {index}"),
                           1_800_000_000_000 + index * 1000)
    provider = email_provider.build_gmail_provider()
    assert len(provider._list_ids()) <= config.GMAIL_MAX_LIST_PAGES


def test_an_oversized_message_is_reported_rather_than_pulled_into_memory(client, google,
                                                                        monkeypatch):
    connect(client)
    storage.advance_oauth_cursor("gmail", 1)
    monkeypatch.setenv(config.EMAIL_MAX_MESSAGE_BYTES_ENV, "500")
    google.add_message("huge", message(body="x" * 5000), 1_800_000_000_000)
    fetched = email_provider.build_gmail_provider().fetch(10)
    assert len(fetched) == 1
    assert fetched[0].raw == b""
    assert fetched[0].id_source == "oversized"


def test_a_message_deleted_between_listing_and_fetching_does_not_abort_the_batch(
        client, google, dkim):
    """Gmail lists an id, the user deletes the message, we fetch a 404. The
    good invoice behind it must still be collected."""
    connect(client)
    storage.advance_oauth_cursor("gmail", 1)
    google.add_message("vanishes", message(subject="Invoice GONE"), 1_800_000_000_000)
    google.add_message("survives", dkim(invoice_email()), 1_800_000_001_000)
    # Listed, then removed before the body is fetched.
    listed = email_provider.build_gmail_provider()._list_ids()
    assert "vanishes" in listed
    original_get = google.api_get

    def deleted_mid_batch(url, access_token):
        if "/messages/vanishes" in url:
            del google.messages["vanishes"]
        return original_get(url, access_token)

    import oauth_google as og
    og.api_get = deleted_mid_batch
    try:
        fetched = email_provider.build_gmail_provider().fetch(10)
    finally:
        og.api_get = original_get
    assert [m.provider_message_id for m in fetched] == ["survives"]


def test_a_credential_failure_mid_batch_does_stop_it(client, google, monkeypatch):
    """The other side of the same rule: retrying the rest would fail
    identically, so the poller must report an unreachable mailbox instead."""
    connect(client)
    storage.advance_oauth_cursor("gmail", 1)
    google.add_message("m1", message(subject="Invoice 1"), 1_800_000_000_000)
    provider = email_provider.build_gmail_provider()

    def unreachable(url, access_token):
        raise oauth_google.OAuthError("could not reach the Gmail API",
                                      code="unreachable", terminal=False)

    monkeypatch.setattr(oauth_google, "api_get", unreachable)
    with pytest.raises(email_provider.EmailProviderError):
        provider.fetch(10)


def test_a_provider_cannot_be_built_with_no_connection(oauth_env):
    with pytest.raises(email_provider.EmailProviderError) as caught:
        email_provider.build_gmail_provider()
    assert "no Gmail mailbox is connected" in str(caught.value)


# ==========================================================================
# 13. Provider selection
# ==========================================================================
def test_an_unset_provider_defers_to_a_stored_connection(client, google):
    connect(client)
    assert email_provider.get_provider().name == "gmail"


def test_no_connection_and_no_setting_means_nothing_is_polled(oauth_env):
    assert email_provider.get_provider().name == "none"
    assert email_ingest.ingestion_configured() is False


def test_an_explicit_imap_setting_is_never_overridden_by_a_gmail_connection(client, google,
                                                                           monkeypatch):
    """An operator who named a provider gets the one they named."""
    connect(client)
    monkeypatch.setenv(config.EMAIL_PROVIDER_ENV, "imap")
    monkeypatch.setenv(config.IMAP_HOST_ENV, "imap.example.com")
    monkeypatch.setenv(config.IMAP_USER_ENV, "ap@example.com")
    monkeypatch.setenv(config.IMAP_PASSWORD_ENV, "hunter2")
    assert email_provider.get_provider().name == "imap"


def test_connecting_a_mailbox_makes_ingestion_active(client, google):
    assert email_ingest.ingestion_configured() is False
    connect(client)
    assert email_ingest.ingestion_configured() is True


def test_disconnecting_makes_ingestion_inactive_again(client, google):
    connect(client)
    client.post("/api/email/oauth/gmail/disconnect", headers=auth_headers("admin"))
    assert email_ingest.ingestion_configured() is False


def test_an_unknown_provider_still_raises(oauth_env, monkeypatch):
    monkeypatch.setattr(config, "email_provider", lambda: "exchange")
    with pytest.raises(email_provider.EmailProviderError) as caught:
        email_provider.get_provider()
    assert "gmail" in str(caught.value)      # the message lists what IS supported


def test_the_manual_poll_endpoint_works_for_a_connected_mailbox(client, google, dkim):
    connect(client)
    storage.advance_oauth_cursor("gmail", 1)
    google.add_message("poll-me", dkim(invoice_email()), 1_800_000_000_000)
    response = client.post("/api/email/ingestion/poll", headers=auth_headers("admin"))
    assert response.status_code == 200
    assert response.json()["fetched"] == 1


def test_the_manual_poll_endpoint_refuses_when_no_mailbox_exists(client):
    response = client.post("/api/email/ingestion/poll", headers=auth_headers("admin"))
    assert response.status_code == 409


# ==========================================================================
# 14. Phase F verification is not bypassed
# ==========================================================================
def test_an_unsigned_gmail_message_is_quarantined(client, google, extraction_spy):
    """Gmail delivering it proves Google accepted it, not that the sender is who
    they claim. Phase F still decides."""
    connect(client)
    storage.advance_oauth_cursor("gmail", 1)
    google.add_message("unsigned", invoice_email(), 1_800_000_000_000)

    email_ingest.poll_once(provider=email_provider.build_gmail_provider())
    stored = storage.get_email_message(storage.list_email_messages()[0]["id"])
    assert stored["status"] == "QUARANTINED"
    assert stored["ingest_status"] == "QUARANTINED"
    assert extraction_spy == []          # nothing expensive ran


def test_a_signed_gmail_message_is_admitted_and_processed(client, google, dkim,
                                                          extraction_spy):
    connect(client)
    storage.advance_oauth_cursor("gmail", 1)
    google.add_message("signed", dkim(invoice_email()), 1_800_000_000_000)

    email_ingest.poll_once(provider=email_provider.build_gmail_provider())
    stored = storage.get_email_message(storage.list_email_messages()[0]["id"])
    assert stored["status"] == "ADMITTED"
    assert len(extraction_spy) == 1


def test_a_newsletter_from_gmail_never_reaches_extraction(client, google, extraction_spy):
    """The cheap filter is still first, whichever door the message came in."""
    connect(client)
    storage.advance_oauth_cursor("gmail", 1)
    google.add_message("news", message(from_header="news@marketing.test",
                                       subject="Our autumn newsletter"),
                       1_800_000_000_000)

    email_ingest.poll_once(provider=email_provider.build_gmail_provider())
    stored = storage.get_email_message(storage.list_email_messages()[0]["id"])
    assert stored["ingest_status"] == "FILTERED_OUT"
    assert extraction_spy == []
    # Not deleted -- recorded, with the reasons, and re-runnable by a human.
    assert stored["reasons"]


def test_a_quarantined_gmail_message_cannot_be_processed_around_the_gate(client, google):
    connect(client)
    storage.advance_oauth_cursor("gmail", 1)
    google.add_message("held", invoice_email(), 1_800_000_000_000)
    email_ingest.poll_once(provider=email_provider.build_gmail_provider())

    email_id = storage.list_email_messages()[0]["id"]
    result = email_ingest.process_message_attachments(email_id)
    assert result["ok"] is False
    assert "release it first" in result["error"]


def test_releasing_a_quarantined_gmail_message_then_processing_it_works(client, google):
    """The Phase F release path, unchanged, over a Gmail-delivered message."""
    connect(client)
    storage.advance_oauth_cursor("gmail", 1)
    google.add_message("release-me", invoice_email(), 1_800_000_000_000)
    email_ingest.poll_once(provider=email_provider.build_gmail_provider())

    email_id = storage.list_email_messages()[0]["id"]
    released = client.post(f"/api/email/messages/{email_id}/release",
                           headers=auth_headers("reviewer"), json={})
    assert released.status_code == 200
    processed = client.post(f"/api/email/messages/{email_id}/process",
                            headers=auth_headers("analyst"))
    assert processed.status_code == 200
    assert processed.json()["runs"]


# ==========================================================================
# 15. The existing Phase G pipeline, reached through Gmail
# ==========================================================================
def test_a_gmail_invoice_becomes_a_run_through_the_existing_pipeline(client, google, dkim):
    connect(client)
    storage.advance_oauth_cursor("gmail", 1)
    google.add_message("pipeline", dkim(invoice_email()), 1_800_000_000_000)

    result = email_ingest.poll_once(provider=email_provider.build_gmail_provider())
    assert result["ok"] is True
    runs = storage.list_runs()
    assert len(runs) == 1
    assert runs[0]["status"] in ("APPROVED", "NEEDS_REVIEW", "REJECTED")


def test_a_gmail_run_is_recorded_with_the_email_source(client, google, dkim):
    """`source="EMAIL"` is right for Gmail: it IS email. A third source value
    would split the ingestion funnel in analytics for no reason."""
    connect(client)
    storage.advance_oauth_cursor("gmail", 1)
    google.add_message("sourced", dkim(invoice_email()), 1_800_000_000_000)
    email_ingest.poll_once(provider=email_provider.build_gmail_provider())

    run_id = storage.list_runs()[0]["id"]
    document = storage.get_document_for_run(run_id)
    assert document["source"] == "EMAIL"


def test_gmail_and_imap_produce_identical_run_structures(client, google, dkim):
    """There is ONE invoice pipeline, and this is the assertion that says so."""
    connect(client)
    storage.advance_oauth_cursor("gmail", 1)
    google.add_message("via-gmail", dkim(invoice_email(subject="Invoice INV-9001")),
                       1_800_000_000_000)
    email_ingest.poll_once(provider=email_provider.build_gmail_provider())
    gmail_run = storage.get_run(storage.list_runs()[0]["id"])

    upload = client.post("/api/runs/stream",
                         files={"file": ("invoice.pdf", pdf_bytes(), "application/pdf")},
                         headers=auth_headers("analyst"))
    assert upload.status_code == 200
    uploaded_run = storage.get_run(
        [r["id"] for r in storage.list_runs() if r["id"] != gmail_run["id"]][0])

    assert [s["name"] for s in gmail_run["stages"]] == \
           [s["name"] for s in uploaded_run["stages"]]
    assert set(gmail_run["audit"].keys()) == set(uploaded_run["audit"].keys())


def test_two_invoices_in_one_gmail_message_become_two_runs(client, google, dkim):
    """1 email == 1 invoice is not assumed on this door either.

    The second PDF is the first plus a trailing comment, which is what makes it
    a genuinely distinct attachment: the sample's text is compressed inside the
    PDF, so editing the invoice number in the raw bytes would not change them
    and the two would be correctly deduplicated to one.
    """
    connect(client)
    storage.advance_oauth_cursor("gmail", 1)
    first = pdf_bytes()
    second = first + b"\n% a second, distinct attachment\n"
    raw = dkim(message(subject="August invoices",
                       attachments=[("a.pdf", "application/pdf", first),
                                    ("b.pdf", "application/pdf", second)]))
    google.add_message("two-invoices", raw, 1_800_000_000_000)
    email_ingest.poll_once(provider=email_provider.build_gmail_provider())

    email_id = storage.list_email_messages()[0]["id"]
    linked = [r for r in storage.list_email_attachments(email_id) if r["run_id"]]
    assert len(linked) == 2
    assert len({r["run_id"] for r in linked}) == 2


def test_the_same_pdf_attached_twice_to_a_gmail_message_is_processed_once(client, google,
                                                                         dkim):
    """The other half of the property above, and the reason the test beside it
    has to build a genuinely different second file."""
    connect(client)
    storage.advance_oauth_cursor("gmail", 1)
    same = pdf_bytes()
    raw = dkim(message(subject="Invoice INV-9001",
                       attachments=[("a.pdf", "application/pdf", same),
                                    ("b.pdf", "application/pdf", same)]))
    google.add_message("same-twice", raw, 1_800_000_000_000)
    email_ingest.poll_once(provider=email_provider.build_gmail_provider())
    assert len(storage.list_runs()) == 1


# ==========================================================================
# 16. Duplicate handling
# ==========================================================================
def test_the_same_gmail_message_is_never_ingested_twice(client, google, dkim, extraction_spy):
    connect(client)
    storage.advance_oauth_cursor("gmail", 1)
    google.add_message("once-only", dkim(invoice_email()), 1_800_000_000_000)

    email_ingest.poll_once(provider=email_provider.build_gmail_provider())
    email_ingest.poll_once(provider=email_provider.build_gmail_provider())

    assert len(storage.list_email_messages()) == 1
    assert len(storage.list_runs()) == 1
    assert len(extraction_spy) == 1


def test_dedup_survives_the_seen_filter_being_unavailable(client, google, dkim,
                                                          extraction_spy):
    """The pre-filter is an optimisation. With it gone, the database's UNIQUE
    constraint is what still guarantees one run -- which is the claim Phase G
    has made about `mark_handled` all along."""
    connect(client)
    storage.advance_oauth_cursor("gmail", 1)
    google.add_message("uniq", dkim(invoice_email()), 1_800_000_000_000)

    def unfiltered():
        connection = storage.get_oauth_connection("gmail")
        return email_provider.GmailApiEmailProvider(connection=connection, seen_filter=None)

    email_ingest.poll_once(provider=unfiltered())
    email_ingest.poll_once(provider=unfiltered())

    assert len(storage.list_email_messages()) == 1
    assert len(storage.list_runs()) == 1
    assert len(extraction_spy) == 1


def test_a_gmail_id_is_the_idempotency_key(client, google, dkim):
    connect(client)
    storage.advance_oauth_cursor("gmail", 1)
    google.add_message("stable-id", dkim(invoice_email()), 1_800_000_000_000)
    email_ingest.poll_once(provider=email_provider.build_gmail_provider())
    assert storage.email_for_provider_message("gmail", "stable-id") is not None


def test_a_gmail_message_and_an_imap_message_do_not_collide(client, google, dkim):
    """The unique constraint is on (provider, id), so the same id string from
    two different mailboxes is two different messages."""
    connect(client)
    storage.advance_oauth_cursor("gmail", 1)
    google.add_message("shared-id", dkim(invoice_email()), 1_800_000_000_000)
    email_ingest.poll_once(provider=email_provider.build_gmail_provider())

    email_ingest.ingest_message(
        email_provider.IncomingEmail("imap", "shared-id", dkim(invoice_email())))
    assert len(storage.list_email_messages()) == 2


# ==========================================================================
# 17. IMAP is untouched
# ==========================================================================
def test_the_imap_provider_still_requires_its_own_settings(oauth_env, monkeypatch):
    monkeypatch.setenv(config.EMAIL_PROVIDER_ENV, "imap")
    monkeypatch.delenv(config.IMAP_HOST_ENV, raising=False)
    with pytest.raises(email_provider.EmailProviderError) as caught:
        email_provider.get_provider()
    assert config.IMAP_HOST_ENV in str(caught.value)


def test_imap_still_prefers_an_oauth_token_over_a_password(oauth_env, monkeypatch):
    monkeypatch.setenv(config.IMAP_HOST_ENV, "imap.example.com")
    monkeypatch.setenv(config.IMAP_USER_ENV, "ap@example.com")
    monkeypatch.setenv(config.IMAP_OAUTH_TOKEN_ENV, "a-token")
    monkeypatch.setenv(config.IMAP_PASSWORD_ENV, "a-password")
    assert email_provider.ImapEmailProvider().describe()["auth"] == "oauth2"


def test_imap_ingestion_still_turns_on_with_its_own_environment_variables(oauth_env,
                                                                         monkeypatch):
    monkeypatch.setenv(config.EMAIL_INGEST_ENABLED_ENV, "1")
    monkeypatch.setenv(config.EMAIL_PROVIDER_ENV, "imap")
    assert email_ingest.ingestion_configured() is True


def test_a_gmail_connection_does_not_switch_on_imap_ingestion(client, google, monkeypatch):
    monkeypatch.setenv(config.EMAIL_PROVIDER_ENV, "imap")
    connect(client)
    # Gmail is connected, but IMAP was named and IMAP was not enabled.
    assert email_ingest.ingestion_configured() is False


def test_the_null_provider_is_still_the_default_with_nothing_configured(oauth_env):
    provider = email_provider.get_provider()
    assert provider.name == "none"
    assert provider.fetch(10) == []


# ==========================================================================
# 18. The schema
# ==========================================================================
def test_the_new_tables_exist(oauth_env):
    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT table_name FROM information_schema.tables
                            WHERE table_schema = %s""", (storage.PG_SCHEMA,))
            tables = {r["table_name"] for r in cur.fetchall()}
    finally:
        conn.close()
    assert "email_oauth_connections" in tables
    assert "oauth_pending_authorizations" in tables


def test_only_one_connection_per_provider_is_possible(client, google):
    """Enforced by the database, not by the code that happens to call it."""
    import psycopg2
    connect(client)
    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.UniqueViolation):
                cur.execute("""INSERT INTO email_oauth_connections
                               (provider, status, connected_at, updated_at)
                               VALUES ('gmail','CONNECTED','now','now')""")
        conn.rollback()
    finally:
        conn.close()


def test_expired_pending_authorizations_are_cleared_lazily(oauth_env):
    """No sweeper job, the same way Phase D's review claims have none."""
    storage.create_pending_authorization(
        state="stale", provider="gmail", code_verifier="v", redirect_uri=REDIRECT_URI,
        requested_by="admin", expires_at="2020-01-01T00:00:00+00:00")
    storage.create_pending_authorization(
        state="fresh", provider="gmail", code_verifier="v", redirect_uri=REDIRECT_URI,
        requested_by="admin", expires_at=oauth_google.state_expiry())

    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT state FROM oauth_pending_authorizations")
            remaining = {r["state"] for r in cur.fetchall()}
    finally:
        conn.close()
    assert remaining == {"fresh"}
