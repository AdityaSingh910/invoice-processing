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
import email.utils
import hashlib
import re
from datetime import datetime, timezone

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
    if choice in ("none", ""):
        return NullEmailProvider()
    raise EmailProviderError(
        f"{config.EMAIL_PROVIDER_ENV}={choice!r} is not implemented. Supported: "
        f"'imap', or 'none' to disable ingestion.")
