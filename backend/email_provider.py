"""Where incoming messages come FROM (Phase G).

WHY THIS IS ITS OWN MODULE

Phase F verifies a message that is handed to the application. This is the part
that goes and gets one, and it is deliberately the only place in the codebase
that knows a mailbox exists. Everything downstream -- triage, verification,
extraction, the pipeline -- takes raw RFC 5322 bytes and a message id, and
would work identically if those arrived by webhook, by S3 drop, or by carrier
pigeon. Keeping the provider behind one small interface is what makes that
true rather than merely aspirational.

WHAT IS ACTUALLY IMPLEMENTED

* `ImapEmailProvider` -- a real, working IMAP client built on the standard
  library's `imaplib`. No new dependency, and IMAP is the one protocol every
  mailbox worth integrating supports, including Gmail and Microsoft 365. It
  authenticates with **XOAUTH2 when a token is configured** and falls back to
  a password only when one is not, because a long-lived mailbox password is
  the credential you least want sitting in an environment variable.
* `GmailApiEmailProvider` (Phase G2) -- a Gmail mailbox connected by OAuth,
  read through Google's REST API under `gmail.readonly`. It exists BESIDE the
  IMAP client rather than replacing it because Google only grants IMAP under
  `https://mail.google.com/`, which carries send and delete authority
  ingestion has no use for. See its class docstring, and CLAUDE.md §7h.
* `NullEmailProvider` -- the default. Ingestion is switched off; it fetches
  nothing and says so. This is not a stand-in for missing work: an install
  that does not want email ingestion should make no outbound connection at
  all, and this is what guarantees it.

There is no mock provider here. Test doubles live in the test suite, where
they cannot be selected by a production configuration.

IDEMPOTENCY IS NOT THIS LAYER'S JOB

A provider may hand the same message over twice -- a retry, a poll that
overlaps the previous one, a restart before the mailbox flag was written, a
server that quietly drops \\Seen. `mark_handled()` is therefore an
optimisation, never a correctness mechanism. Correctness comes from the
UNIQUE constraint on (provider, provider_message_id) in the database, which
holds even if every mailbox flag in the folder is lost.
"""
import abc
import base64
import email.utils
import hashlib
import re
import urllib.parse
from datetime import datetime, timedelta, timezone

import config


class IncomingEmail:
    """One message as it arrived: raw bytes plus how to refer to it again.

    `provider_message_id` is the idempotency key, and must be **stable across
    fetches** -- the same message fetched tomorrow has to produce the same id,
    or duplicate suppression silently stops working. It is preferred in this
    order: the provider's own immutable id, the RFC 5322 `Message-ID` header,
    and finally a SHA-256 of the raw bytes. The last is a genuine fallback
    rather than a good answer (two identical messages sent deliberately twice
    would collapse into one), which is why it is recorded in `id_source` so
    an operator can see which was used.
    """

    __slots__ = ("provider", "provider_message_id", "id_source", "raw",
                 "received_at", "folder", "handle")

    def __init__(self, provider, provider_message_id, raw, received_at=None,
                 folder=None, handle=None, id_source="provider"):
        self.provider = provider
        self.provider_message_id = str(provider_message_id)[:400]
        self.id_source = id_source
        self.raw = raw or b""
        self.received_at = received_at
        self.folder = folder
        self.handle = handle          # provider-private (an IMAP UID, say)

    def __repr__(self):
        return (f"<IncomingEmail {self.provider}:{self.provider_message_id} "
                f"{len(self.raw)} bytes>")


class EmailProviderError(RuntimeError):
    """A provider could not be reached, authenticated to, or read.

    Raised rather than swallowed: an unreachable mailbox must be visible as a
    failure an operator can see, not as a poll that quietly found nothing --
    those two look identical from the outside and mean very different things.
    """


class EmailProvider(abc.ABC):
    """Fetch messages, and note which have been dealt with."""

    name = "abstract"

    @abc.abstractmethod
    def fetch(self, limit: int) -> list:
        """Up to `limit` candidate messages. May legitimately return []."""

    def mark_handled(self, message: IncomingEmail) -> None:
        """Best-effort: stop the next poll seeing this again. Never required
        for correctness -- see the module docstring."""

    def close(self) -> None:
        """Release the connection. Must be safe to call twice."""

    def describe(self) -> dict:
        """Non-secret configuration, for the ingestion-status endpoint.

        Implementations must never put a password, a token, or anything
        derived from one in here.
        """
        return {"provider": self.name}


class NullEmailProvider(EmailProvider):
    """Ingestion disabled. The default."""

    name = "none"

    def fetch(self, limit: int) -> list:
        return []

    def describe(self) -> dict:
        return {"provider": "none",
                "detail": "email ingestion is not configured; nothing is polled"}


# --------------------------------------------------------------------------
# IMAP
# --------------------------------------------------------------------------
_UID_RE = re.compile(rb"UID (\d+)")
_MESSAGE_ID_RE = re.compile(rb"^message-id:\s*(.+)$", re.I | re.M)


class ImapEmailProvider(EmailProvider):
    """A real IMAP mailbox, over the standard library.

    Connection is always TLS (`IMAP4_SSL`). There is no plaintext option and
    no STARTTLS-optional path: mailbox credentials and invoice attachments are
    not things to make optional-in-transit.
    """

    name = "imap"

    def __init__(self, settings: dict = None):
        self.settings = dict(settings or config.imap_settings())
        if not self.settings.get("host"):
            raise EmailProviderError(
                f"EMAIL_PROVIDER=imap needs {config.IMAP_HOST_ENV} set")
        if not self.settings.get("username"):
            raise EmailProviderError(
                f"EMAIL_PROVIDER=imap needs {config.IMAP_USER_ENV} set")
        if not (self.settings.get("oauth_token") or self.settings.get("password")):
            raise EmailProviderError(
                f"EMAIL_PROVIDER=imap needs {config.IMAP_OAUTH_TOKEN_ENV} "
                f"(preferred) or {config.IMAP_PASSWORD_ENV}")
        self._conn = None

    # -- connection ---------------------------------------------------------
    def _connect(self):
        if self._conn is not None:
            return self._conn
        import imaplib
        import ssl
        try:
            context = ssl.create_default_context()
            conn = imaplib.IMAP4_SSL(self.settings["host"], self.settings["port"],
                                     ssl_context=context)
        except Exception as exc:
            raise EmailProviderError(
                f"could not connect to {self.settings['host']}: "
                f"{exc.__class__.__name__}") from exc

        token = self.settings.get("oauth_token")
        try:
            if token:
                # SASL XOAUTH2 (RFC 7628 style, as Google and Microsoft
                # implement it). Preferred over a password wherever the
                # provider offers it -- an access token is short-lived and
                # scoped, a mailbox password is neither.
                sasl = f"user={self.settings['username']}\x01auth=Bearer {token}\x01\x01"
                conn.authenticate("XOAUTH2", lambda _: sasl.encode())
            else:
                conn.login(self.settings["username"], self.settings["password"])
        except Exception as exc:
            try:
                conn.logout()
            except Exception:
                pass
            # Deliberately does not echo the credential, the token, or the
            # server's response body -- an auth failure message is a place
            # secrets leak into logs.
            raise EmailProviderError(
                f"IMAP authentication failed for {self.settings['username']} "
                f"({'oauth token' if token else 'password'})") from exc

        try:
            typ, _ = conn.select(self.settings["folder"])
            if typ != "OK":
                raise EmailProviderError(f"cannot select folder {self.settings['folder']!r}")
        except EmailProviderError:
            raise
        except Exception as exc:
            raise EmailProviderError(
                f"cannot select folder {self.settings['folder']!r}") from exc

        self._conn = conn
        return conn

    # -- fetching -----------------------------------------------------------
    def fetch(self, limit: int) -> list:
        conn = self._connect()
        try:
            typ, data = conn.uid("SEARCH", None, self.settings["search"])
        except Exception as exc:
            raise EmailProviderError(f"IMAP search failed: {exc.__class__.__name__}") from exc
        if typ != "OK":
            raise EmailProviderError(f"IMAP search returned {typ}")

        uids = (data[0] or b"").split()
        # Oldest first: a mailbox that has been building up should drain in
        # arrival order, so the invoice that has been waiting longest is the
        # one that gets processed first.
        uids = uids[:max(0, int(limit))]

        out = []
        for uid in uids:
            try:
                typ, parts = conn.uid("FETCH", uid, "(BODY.PEEK[] INTERNALDATE)")
            except Exception:
                # One unreadable message must not abort the whole batch and
                # strand every message behind it.
                continue
            if typ != "OK" or not parts:
                continue
            raw = next((p[1] for p in parts
                        if isinstance(p, tuple) and isinstance(p[1], (bytes, bytearray))), None)
            if not raw:
                continue
            raw = bytes(raw)
            if len(raw) > config.email_max_message_bytes():
                # Recorded by the caller as an oversized message rather than
                # pulled into memory repeatedly on every poll: mark it handled
                # so it does not block the folder, and let the ingestion layer
                # write the failure record.
                out.append(IncomingEmail(
                    self.name, self._message_id(raw, uid), b"",
                    received_at=self._internal_date(parts),
                    folder=self.settings["folder"], handle=uid,
                    id_source="oversized"))
                continue
            out.append(IncomingEmail(
                self.name, self._message_id(raw, uid), raw,
                received_at=self._internal_date(parts),
                folder=self.settings["folder"], handle=uid,
                id_source="message-id" if _MESSAGE_ID_RE.search(raw[:100_000]) else "uid"))
        return out

    def _message_id(self, raw: bytes, uid: bytes) -> str:
        """Stable across fetches -- see IncomingEmail's docstring.

        The RFC 5322 Message-ID is preferred over the IMAP UID because a UID is
        only unique within one folder on one server: a message moved between
        folders, or a mailbox restored from backup, gets a new UID and would be
        reprocessed. Message-ID survives both.
        """
        match = _MESSAGE_ID_RE.search(raw[:100_000])
        if match:
            value = match.group(1).decode("utf-8", "replace").strip()
            if value:
                return value[:400]
        if uid:
            return f"uid:{self.settings['folder']}:{uid.decode('ascii', 'replace')}"
        return "sha256:" + hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _internal_date(parts) -> str:
        for p in parts:
            blob = p[0] if isinstance(p, tuple) else p
            if not isinstance(blob, (bytes, bytearray)):
                continue
            match = re.search(rb'INTERNALDATE "([^"]+)"', bytes(blob))
            if not match:
                continue
            try:
                import imaplib
                stamp = imaplib.Internaldate2tuple(b'INTERNALDATE "'
                                                   + match.group(1) + b'"')
                if stamp:
                    return datetime.fromtimestamp(
                        __import__("time").mktime(stamp), timezone.utc).isoformat()
            except Exception:
                pass
        return datetime.now(timezone.utc).isoformat()

    def mark_handled(self, message: IncomingEmail) -> None:
        if not self.settings.get("mark_seen") or not message.handle:
            return
        try:
            self._connect().uid("STORE", message.handle, "+FLAGS", "(\\Seen)")
        except Exception:
            # Best-effort by design. A failure here means the next poll sees
            # the message again, and the database's unique constraint refuses
            # it -- which is exactly the outcome that mechanism exists for.
            pass

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        for step in (conn.close, conn.logout):
            try:
                step()
            except Exception:
                pass

    def describe(self) -> dict:
        """Never includes the password or the token -- only whether one is set."""
        return {
            "provider": "imap",
            "host": self.settings.get("host"),
            "port": self.settings.get("port"),
            "username": self.settings.get("username"),
            "folder": self.settings.get("folder"),
            "search": self.settings.get("search"),
            "auth": "oauth2" if self.settings.get("oauth_token") else "password",
            "credential_configured": bool(self.settings.get("oauth_token")
                                          or self.settings.get("password")),
        }


# --------------------------------------------------------------------------
# Gmail (Phase G2)
# --------------------------------------------------------------------------
class GmailApiEmailProvider(EmailProvider):
    """A Gmail mailbox connected by OAuth, read through the Gmail REST API.

    WHY THIS EXISTS BESIDE ImapEmailProvider RATHER THAN REPLACING IT.

    The IMAP provider above already speaks XOAUTH2, so it could talk to Gmail
    with an OAuth token and no new code at all. What stops that is the SCOPE:
    Google only grants IMAP under `https://mail.google.com/`, which is full
    read, write, send and delete over the whole mailbox. This provider asks for
    `gmail.readonly` instead -- it can read messages and download attachments
    and it can do nothing else, which is exactly the authority ingestion uses.

    IMAP remains the right answer for every non-Google mailbox and is untouched.

    HOW "DO NOT SHOW ME THIS AGAIN" WORKS WITHOUT WRITE ACCESS.

    Read-only cannot set a flag, so this keeps a high-water CURSOR over Gmail's
    own `internalDate` instead, advanced through `mark_handled()` -- the hook
    the poller already calls after an outcome is committed. The properties are
    the ones the module docstring claims for `mark_handled` generally: losing
    an update costs a refetch, never a duplicate, because correctness is the
    UNIQUE (provider, provider_message_id) constraint in the database.

    Each poll re-reads a short overlap behind the mark, so a message Google
    delivers slightly out of order is still seen. Those refetches cost only
    their ids -- an already-ingested message is refused before its body is
    fetched, because `fetch()` asks the database which ids are new.
    """

    name = "gmail"

    def __init__(self, connection=None, seen_filter=None):
        """`connection` and `seen_filter` are injected so this class can be
        tested without a database. `get_provider()` supplies the real ones."""
        self._connection = connection
        self._seen_filter = seen_filter
        self._token = None
        self._scopes = None

    # -- credentials --------------------------------------------------------
    def _access_token(self, force_refresh: bool = False) -> str:
        import oauth_google
        if force_refresh:
            import storage
            # Drop the cached access token so the next call is forced through a
            # refresh. Only the ACCESS token goes: the refresh token beside it
            # is very probably still good, and this path is reached most often
            # by clock skew rather than by a revocation.
            storage.clear_oauth_access_token(self.name)
            self._token = None
        if self._token is None:
            try:
                self._token = oauth_google.gmail_access_token()
            except oauth_google.OAuthError as exc:
                # Surfaced as the error type the poller already understands, so
                # an expired grant reads as an unreachable mailbox rather than
                # as an unhandled exception in a background task.
                raise EmailProviderError(str(exc)) from None
        return self._token

    def _api(self, path: str, params: dict = None) -> dict:
        """One authenticated Gmail API call, with a single refresh-and-retry.

        The retry exists because an access token can be rejected while our own
        clock still believes it is valid -- Google revoking it, or a few
        seconds of skew. Retried exactly ONCE: a second 401 after a genuine
        refresh means the grant is gone, and looping on it would turn a revoked
        mailbox into a request flood.
        """
        import oauth_google
        url = config.GMAIL_API_ROOT + path
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v not in (None, "")})
        for attempt in (0, 1):
            try:
                return oauth_google.api_get(url, self._access_token(force_refresh=bool(attempt)))
            except oauth_google.OAuthError as exc:
                if exc.code == "http_401" and attempt == 0:
                    continue
                # The code is carried across onto the provider-level error so
                # `fetch` can tell "this one message could not be read" from
                # "our credential is no longer accepted". Without it every
                # failure looks the same, and one message deleted between the
                # list and the fetch would abort the whole batch.
                error = EmailProviderError(str(exc))
                error.code = exc.code
                raise error from None
        raise EmailProviderError("the Gmail API refused an access token twice")

    # -- the window this poll reads ----------------------------------------
    def _cursor_epoch(self) -> int:
        """The Gmail `after:` bound for this poll, in whole seconds.

        Derived from the stored high-water mark less the configured overlap.
        With no mark at all -- a connection saved before this column existed --
        it falls back to the backfill window rather than to the beginning of
        the mailbox, because ingesting years of already-handled invoices is a
        far worse first impression than missing a few hours.
        """
        connection = self._connection or {}
        cursor_ms = connection.get("cursor_internal_date")
        if cursor_ms:
            seconds = int(cursor_ms) // 1000
        else:
            seconds = int((datetime.now(timezone.utc)
                           - timedelta(days=config.gmail_backfill_days())).timestamp())
        return max(0, seconds - config.gmail_cursor_overlap_seconds())

    def _list_ids(self) -> list:
        """Every candidate message id since the cursor, oldest first.

        TAKES NO LIMIT, DELIBERATELY. Gmail returns newest-first, so trimming
        here would keep the NEWEST few of a backlog -- and advancing the cursor
        through those would strand everything older behind it permanently. The
        ids are collected across a BOUNDED number of pages, reversed into
        arrival order, and `fetch` takes the oldest few: the same "drain in
        arrival order" the IMAP provider documents, and the ordering the cursor
        needs in order to be safe to advance at all.

        Listing is cheap -- ids only, 100 to a page -- so the bound that
        matters is the page cap, not a row count.
        """
        query = f"{config.gmail_search_query()} after:{self._cursor_epoch()}".strip()
        ids, page_token = [], None
        for _ in range(config.GMAIL_MAX_LIST_PAGES):
            payload = self._api("/messages", {
                "q": query,
                "maxResults": 100,
                "pageToken": page_token,
                # Ingestion reads mail. A draft is not mail that arrived, and a
                # message in the bin was deliberately thrown away.
                "includeSpamTrash": "false",
            })
            for item in (payload.get("messages") or []):
                if item.get("id"):
                    ids.append(item["id"])
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
        ids.reverse()
        return ids

    # -- fetching -----------------------------------------------------------
    def fetch(self, limit: int) -> list:
        candidates = self._list_ids()

        # Ask the database which of these are new BEFORE downloading any
        # bodies. The overlap window deliberately re-offers recent ids every
        # poll, and `ingest_message` would refuse them anyway -- but it would
        # refuse them after this provider had already paid for a full message
        # download apiece. This is the difference between the overlap costing
        # a few ids and it costing the whole mailbox, every two minutes.
        if self._seen_filter is not None:
            candidates = [i for i in candidates if not self._seen_filter(i)]

        out = []
        for message_id in candidates[:max(0, int(limit))]:
            try:
                # Percent-encoded even though a Gmail id is hex: it is a value
                # that arrived over the network being interpolated into a URL
                # path, and that is the shape of thing that should never be
                # trusted to be well-formed just because it usually is.
                payload = self._api(
                    f"/messages/{urllib.parse.quote(str(message_id), safe='')}",
                    {"format": "raw"})
            except EmailProviderError as exc:
                # A CREDENTIAL failure is not about this one message: retrying
                # the rest would fail identically, so the batch stops and the
                # poller reports an unreachable mailbox.
                #
                # Anything else IS about this message -- most often a 404,
                # because Gmail listed it and the user deleted it moments
                # later -- and must not strand every message behind it.
                if getattr(exc, "code", None) in (None, "http_401", "unreachable"):
                    raise
                continue
            except Exception:
                continue

            raw = self._decode_raw(payload.get("raw"))
            received = self._received_at(payload.get("internalDate"))
            if raw is None:
                continue
            if len(raw) > config.email_max_message_bytes():
                # Handed over empty and flagged, exactly as the IMAP provider
                # does: the ingestion layer writes the oversized-message
                # failure record, so both doors report it identically.
                out.append(IncomingEmail(
                    self.name, message_id, b"", received_at=received,
                    folder="gmail", handle=payload.get("internalDate"),
                    id_source="oversized"))
                continue
            out.append(IncomingEmail(
                self.name, message_id, raw, received_at=received,
                folder="gmail", handle=payload.get("internalDate"),
                # Gmail's own id is immutable and unique per mailbox -- it
                # survives a label change, a move, and a thread being replied
                # to, which is more than the RFC 5322 Message-ID guarantees and
                # far more than an IMAP UID does.
                id_source="gmail-id"))
        return out

    @staticmethod
    def _decode_raw(blob):
        """Gmail's base64url message body, back to RFC 5322 bytes.

        Byte-exactness matters here more than anywhere else in the flow: Phase
        F verifies DKIM over these bytes, and a single re-encoded header would
        turn a good signature into a failed one -- which quarantines a
        legitimate vendor and reads as an accusation.
        """
        if not blob:
            return None
        try:
            padded = blob + "=" * (-len(blob) % 4)
            return base64.urlsafe_b64decode(padded.encode("ascii"))
        except Exception:
            return None

    @staticmethod
    def _received_at(internal_date):
        if not internal_date:
            return datetime.now(timezone.utc).isoformat()
        try:
            return datetime.fromtimestamp(int(internal_date) / 1000.0,
                                          timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            return datetime.now(timezone.utc).isoformat()

    # -- after the outcome is committed -------------------------------------
    def mark_handled(self, message: IncomingEmail) -> None:
        """Advance the cursor past this message. Best-effort, by design.

        The poller calls this only after the message's outcome is committed, so
        the cursor can never move past something that was not recorded. If it
        fails, the next poll offers the message again and the unique constraint
        refuses it -- the same consequence a lost IMAP flag has.
        """
        import storage
        try:
            if message.handle:
                storage.advance_oauth_cursor(self.name, int(message.handle))
            else:
                storage.touch_oauth_poll(self.name)
        except Exception:
            pass

    def close(self) -> None:
        """Nothing to release. Each API call is its own HTTPS request, so there
        is no connection held open between polls -- and the in-memory access
        token is dropped so it does not outlive the object holding it."""
        self._token = None

    def describe(self) -> dict:
        """Never includes a token, an authorization code, or the client secret."""
        connection = self._connection or {}
        return {
            "provider": "gmail",
            "mailbox": connection.get("email_address"),
            "status": connection.get("status"),
            "scopes": connection.get("scopes"),
            "auth": "oauth2",
            "credential_configured": bool(connection.get("refresh_token_encrypted")),
            "query": config.gmail_search_query(),
            "cursor_internal_date": connection.get("cursor_internal_date"),
            "last_polled_at": connection.get("last_polled_at"),
            "last_error": connection.get("last_error"),
        }


def build_gmail_provider() -> "GmailApiEmailProvider":
    """A Gmail provider wired to the live connection and the live database.

    The two injected pieces are what let the class itself be tested with no
    database: here is where the real ones are attached, and this is the only
    place that knows both halves exist.
    """
    import storage
    connection = storage.get_oauth_connection("gmail")
    if not connection:
        raise EmailProviderError(
            "no Gmail mailbox is connected. An administrator connects one from "
            "Settings -> Email integration.")
    if connection.get("status") == storage.OAUTH_REVOKED:
        raise EmailProviderError(
            "the Gmail authorization has been revoked or has expired; reconnect "
            "the mailbox from Settings -> Email integration.")

    def already_ingested(message_id: str) -> bool:
        try:
            return storage.email_for_provider_message("gmail", message_id) is not None
        except Exception:
            # If the lookup itself fails, do NOT claim the message is new and
            # do not claim it is seen: fetching it and letting the unique
            # constraint decide is the branch that cannot lose an invoice.
            return False

    return GmailApiEmailProvider(connection=connection, seen_filter=already_ingested)


def gmail_connection_is_live() -> bool:
    """Whether a usable Gmail connection is stored.

    Used to decide whether ingestion has a mailbox to poll. Deliberately
    tolerant of a database that is unreachable or has never been initialised --
    this is asked at startup, and an install with no Gmail connection must not
    fail to boot because the question could not be answered.
    """
    try:
        import storage
        connection = storage.get_oauth_connection("gmail")
    except Exception:
        return False
    return bool(connection and connection.get("status") == "CONNECTED"
                and connection.get("refresh_token_encrypted"))


def get_provider() -> EmailProvider:
    """The configured provider.

    An unrecognised name raises rather than quietly falling back to doing
    nothing: a deployment that set EMAIL_PROVIDER to something this build does
    not implement has a mailbox nobody is reading, and the failure should be
    loud at startup rather than discovered when an invoice goes missing. Same
    posture as documents.get_store() and email_signature.get_verifier().
    """
    choice = config.email_provider()
    if choice == "imap":
        return ImapEmailProvider()
    if choice == "gmail":
        return build_gmail_provider()
    if choice in ("none", ""):
        # NOT CONFIGURED IS NOT THE SAME AS NOT CONNECTED (Phase G2).
        #
        # An administrator who completed the Google consent screen has said
        # "poll this mailbox" more concretely than an environment variable
        # ever could, and a mailbox that shows as connected in the UI while
        # nothing reads it would make that screen a lie. So an unset
        # EMAIL_PROVIDER defers to a stored connection.
        #
        # An EXPLICIT setting always wins -- `EMAIL_PROVIDER=imap` selects
        # IMAP even with Gmail connected, because an operator who named a
        # provider should get the one they named. Only the absence of a
        # choice is filled in from stored state.
        if gmail_connection_is_live():
            return build_gmail_provider()
        return NullEmailProvider()
    raise EmailProviderError(
        f"{config.EMAIL_PROVIDER_ENV}={choice!r} is not implemented. Supported: "
        f"'imap', 'gmail', or 'none' to disable ingestion.")
