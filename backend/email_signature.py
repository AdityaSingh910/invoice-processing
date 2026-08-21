"""User-level digital signatures on email (S/MIME, PGP) -- Phase F.

WHY THIS IS A SEPARATE MODULE FROM email_security.py

Because it is a separate claim, and conflating the two would be the single
most misleading thing this feature could do.

    DKIM says:   "the domain acme.example asserts this message passed
                  through its infrastructure, and here is a signature over
                  the bytes to prove the domain said so."

    S/MIME says: "Dana Okafor, holder of a certificate issued by a CA you
                  chose to trust, asserts they composed this content."

A message can carry a perfect DKIM signature and no user signature at all --
that is the normal case for essentially all business email. A DKIM pass is
therefore not weak evidence of a user signature; it is evidence of an
entirely different proposition. `email_security.py` verifies DKIM and reports
it under `dkim`. This module reports, separately and never merged into that,
whether a *person* signed the content.

WHAT THIS MODULE ACTUALLY DOES TODAY

It DETECTS a signature and reports its status as unavailable. It does not
verify one, and it cannot report a pass. That is not an oversight to be
tidied up later -- it follows from what verification requires:

* An S/MIME signature verifies against a certificate chain, terminating in a
  root this organisation has decided to trust. There is no certificate store
  in this deployment, no configured trust anchors, and no revocation source
  (CRL or OCSP). Verifying a signature against a chain while accepting any
  root, or skipping revocation, produces a "valid" that means nothing: a
  self-signed certificate naming the CFO would pass.
* A PGP signature verifies against a keyring, and the same argument applies
  to key ownership and to key revocation.

So the honest options were: build the trust infrastructure (a phase of its
own, and not the one that was asked for), or report `unavailable` and provide
the interface a real verifier plugs into. This module does the latter. What
it will never do is return a pass it did not earn -- `UnavailableSignatureVerifier`
has no code path that produces one, by construction rather than by
convention.

DETECTION IS STILL WORTH DOING

Knowing that a message *carries* an S/MIME signature is real information: it
tells a reviewer there is something to check, tells a future verifier where
to look, and means the record can distinguish "this sender signs their
invoices and this one is signed" from "no signature was present". Detection
reads MIME structure only (RFC 8551 §3.4.3, RFC 3156 §5) and requires no
cryptography, so it is reliable here in a way verification is not.
"""
import abc

import config

# What kind of signature is attached, if any.
KIND_NONE = "none"
KIND_SMIME = "smime"
KIND_PGP = "pgp"

# The status of the signature ITSELF -- deliberately the same three-state
# vocabulary email_security.py uses for SPF/DKIM/DMARC, plus `not_present`,
# because "this message is not signed" and "this message is signed and we
# could not check it" are different facts and a reviewer needs both.
STATE_PASS = "pass"
STATE_FAIL = "fail"
STATE_UNAVAILABLE = "unavailable"
STATE_NOT_PRESENT = "not_present"

_SMIME_SIGNATURE_TYPES = {
    "application/pkcs7-signature",
    "application/x-pkcs7-signature",
}
_PGP_SIGNATURE_TYPES = {"application/pgp-signature"}


def detect(msg) -> dict:
    """What kind of user-level signature this message carries, from MIME alone.

    Two shapes exist and both are recognised:

    * `multipart/signed` with a `protocol` parameter and a detached signature
      part -- the common form, and the one that keeps the content readable by
      clients that cannot verify it.
    * `application/pkcs7-mime; smime-type=signed-data` -- opaque signed data,
      where the content is wrapped inside the signature object.
    """
    if msg is None:
        return {"kind": KIND_NONE, "detail": "the message could not be parsed"}

    try:
        top_type = (msg.get_content_type() or "").lower()
        protocol = (msg.get_param("protocol") or "").lower()
        smime_type = (msg.get_param("smime-type") or "").lower()
    except Exception:
        return {"kind": KIND_NONE, "detail": "the message headers could not be read"}

    if top_type == "multipart/signed":
        if protocol in _PGP_SIGNATURE_TYPES:
            return {"kind": KIND_PGP, "detail": "multipart/signed with an OpenPGP signature part"}
        if protocol in _SMIME_SIGNATURE_TYPES:
            return {"kind": KIND_SMIME, "detail": "multipart/signed with a PKCS#7 signature part"}

    if top_type in ("application/pkcs7-mime", "application/x-pkcs7-mime"):
        if smime_type in ("signed-data", "certs-only") or not smime_type:
            return {"kind": KIND_SMIME, "detail": f"application/pkcs7-mime ({smime_type or 'unspecified'})"}

    # Fall back to looking for a signature PART anywhere in the tree: some
    # senders emit multipart/signed without the protocol parameter, and a
    # signature that is present but described sloppily is still present.
    try:
        for part in msg.walk():
            ctype = (part.get_content_type() or "").lower()
            if ctype in _PGP_SIGNATURE_TYPES:
                return {"kind": KIND_PGP, "detail": f"a {ctype} part is attached"}
            if ctype in _SMIME_SIGNATURE_TYPES:
                return {"kind": KIND_SMIME, "detail": f"a {ctype} part is attached"}
    except Exception:
        pass

    return {"kind": KIND_NONE, "detail": "no S/MIME or PGP signature part is present"}


class SignatureVerifier(abc.ABC):
    """The interface a real verifier implements.

    Deliberately takes the parsed message and returns a result in the same
    shape as `email_security`'s mechanism results, so that when a verifier
    with a real trust anchor is added, nothing downstream -- the stored
    record, the classifier, the API response -- has to change shape to
    accommodate it.
    """

    @abc.abstractmethod
    def verify(self, msg) -> dict:
        ...

    @staticmethod
    def _result(kind, state, detail, signer=None):
        return {"kind": kind, "state": state, "detail": detail, "signer": signer,
                "verified": state == STATE_PASS}


class UnavailableSignatureVerifier(SignatureVerifier):
    """Detects a signature; never claims to have verified one.

    There is no branch in this class that returns STATE_PASS. That is the
    point: a stub that could be made to report success by an unexpected input
    would be worse than no stub at all, because every layer above it treats a
    pass as meaningful.
    """

    def verify(self, msg) -> dict:
        found = detect(msg)
        if found["kind"] == KIND_NONE:
            return self._result(KIND_NONE, STATE_NOT_PRESENT, found["detail"])
        return self._result(
            found["kind"], STATE_UNAVAILABLE,
            f"{found['detail']}; this deployment has no configured trust anchor "
            f"(certificate store or keyring) or revocation source, so the signature "
            f"is recorded as present but NOT verified. It is not evidence for or "
            f"against this message.")


def get_verifier() -> SignatureVerifier:
    """The configured verifier.

    Only 'none' exists. An unrecognised value raises here rather than falling
    back silently -- a deployment that asked for signature verification and
    quietly got detection instead would be exactly the false assurance this
    module exists to avoid. Same failure style as documents.get_store()'s
    treatment of an unconfigured S3 bucket: refuse at the point of use, with
    a message that says what to do.
    """
    choice = config.email_signature_verifier()
    if choice in ("none", ""):
        return UnavailableSignatureVerifier()
    raise RuntimeError(
        f"EMAIL_SIGNATURE_VERIFIER={choice!r} is not implemented. The only supported "
        "value is 'none', which detects an S/MIME or PGP signature and records its "
        "status as unavailable. Implementing another means providing a trust anchor "
        "and a revocation source -- see this module's docstring.")
