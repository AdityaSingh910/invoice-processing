"""Outbound email -- the provider abstraction a rejection notice is sent through.

WHAT THIS MODULE IS, AND WHY IT IS SEPARATE FROM `email_provider.py`

`email_provider.py` answers "how do we READ a mailbox". This module answers
"how do we SEND from one" -- a different capability, a different Google scope,
and (for a future provider) potentially a different set of credentials
entirely, so it gets its own small interface rather than growing a `send()`
method onto the module built for reading. Same shape as `EmailProvider`
(`NullEmailProvider` / `ImapEmailProvider` / `GmailApiEmailProvider`), same
reason: the caller (`notifications.py`) never knows which concrete sender it
has, and a second provider (SMTP, SendGrid, Outlook) plugs in here without
touching anything upstream of `get_sender()`.

ONLY GMAIL IS IMPLEMENTED. That is deliberate, not an oversight -- the brief
this was built from says not to add providers nobody asked for. The interface
is written so a second one is a new class, not a redesign.

LEAST PRIVILEGE, AND WHY `gmail.send` RATHER THAN `mail.google.com`

`gmail.send` can send and can do nothing else -- it cannot read a single
message, list a label, or touch mailbox settings. That is the entire reason it
was added to `config.gmail_scopes()`'s supported set instead of reusing the
broader IMAP scope this application has refused since Phase G2 (§7h.2) and
still refuses. Sending a rejection notice needs exactly the authority to send
one; it needs nothing else, and nothing else is ever requested for it.

THE SCOPE IS CHECKED AGAINST THE LIVE CONNECTION, NEVER ASSUMED

A Gmail mailbox connected before this feature existed has a token scoped to
whatever the consent screen showed at the time -- which does not include
`gmail.send`, because Google fixes a token's scopes at the moment of consent
and cannot silently widen them later. `GmailApiEmailSender.send()` therefore
re-reads the stored connection's granted scopes on every call
(`oauth_google.can_send`) rather than trusting that a connection existing
means it can send. An administrator who wants this feature reconnects with
`GMAIL_OAUTH_SCOPES` including `gmail.send` -- the same re-consent Phase K's
account re-check already made routine for authority in this application
(§7e.2), applied here to a mailbox's authority rather than a user's.
"""
import base64
import sys
from email.message import EmailMessage

import config
import oauth_google

PROVIDER = "gmail"


class EmailSendError(RuntimeError):
    """A rejection notice could not be sent. Carries a short code, never a
    token, a body, or anything else that reached Google -- the same discipline
    `oauth_google.OAuthError` already keeps, because this exception is exactly
    as likely to be logged or shown to a reviewer."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class EmailSender:
    """What `notifications.py` needs from an outbound email provider."""

    def send(self, to: str, subject: str, body_text: str) -> dict:
        """Send one plain-text email. Returns {"provider", "message_id"}.

        Raises `EmailSendError` on any failure -- never returns a "sort of
        sent" result, because the caller's audit record and duplicate-send
        guard both depend on success meaning success.
        """
        raise NotImplementedError


class GmailApiEmailSender(EmailSender):
    """Sends through the same connected Gmail mailbox ingestion reads from.

    One mailbox, one connection, one provider row (`UNIQUE(provider)` on
    `email_oauth_connections`, §4) -- reading and sending share it rather than
    each holding a separate credential, because it is the same account either
    way and a second stored credential for the same mailbox would be a second
    thing that can drift out of sync with the first.
    """

    name = "gmail"

    def _access_token(self, connection: dict, force_refresh: bool = False) -> str:
        if force_refresh:
            import storage
            storage.clear_oauth_access_token(self.name)
        try:
            return oauth_google.gmail_access_token()
        except oauth_google.OAuthError as exc:
            raise EmailSendError(str(exc), code=exc.code or "oauth_error") from None

    def send(self, to: str, subject: str, body_text: str) -> dict:
        import storage

        connection = storage.get_oauth_connection(PROVIDER)
        if not connection or connection.get("status") != storage.OAUTH_CONNECTED:
            raise EmailSendError(
                "no connected Gmail mailbox can send this message; connect Gmail "
                "from Settings first", code="not_connected")

        granted = (connection.get("scopes") or "").split()
        if not oauth_google.can_send(granted):
            raise EmailSendError(
                "the connected Gmail mailbox was not granted permission to send. "
                "An administrator must reconnect Gmail with sending enabled "
                f"({config.GMAIL_SCOPE_SEND}).", code="no_send_scope")

        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        from_address = connection.get("email_address")
        if from_address:
            message["From"] = from_address
        message.set_content(body_text)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

        url = f"{config.GMAIL_API_ROOT}/messages/send"
        # Same retry-once-on-401 shape as GmailApiEmailProvider._api (§7h):
        # an access token can be rejected while our own clock still believes
        # it is valid. A second 401 after a genuine refresh means the grant
        # itself is gone, and looping on it would turn a revoked mailbox into
        # a request flood rather than a clean failure.
        for attempt in (0, 1):
            try:
                token = self._access_token(connection, force_refresh=bool(attempt))
                result = oauth_google.api_post_json(url, token, {"raw": raw})
                return {"provider": self.name, "message_id": result.get("id")}
            except oauth_google.OAuthError as exc:
                if exc.code == "http_401" and attempt == 0:
                    continue
                raise EmailSendError(str(exc), code=exc.code or "send_failed") from None
            except EmailSendError as exc:
                if exc.code == "http_401" and attempt == 0:
                    continue
                raise
        raise EmailSendError("Gmail refused an access token twice", code="send_failed")


def get_sender() -> EmailSender:
    """The outbound provider this deployment sends through.

    Gmail is the only implementation, so this is currently unconditional --
    the seam is here for the day a second provider (SMTP, SendGrid, Outlook)
    exists, not because one does yet. `GmailApiEmailSender.send()` itself is
    the "not configured" failure path: it raises a clear `EmailSendError`
    rather than this factory silently returning something inert, so a caller
    that only ever calls `.send()` cannot observe a difference.
    """
    return GmailApiEmailSender()
