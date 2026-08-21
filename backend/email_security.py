"""Email trusted-source verification (Phase F).

WHAT THIS PROVES, AND WHAT IT DOES NOT

This module answers exactly one question about an incoming message: **how
much of its claimed origin can this process actually prove?** It never
answers "is this invoice legitimate". That stays where it already is --
`rules.decide()`, running the same deterministic checks it runs for a
manually uploaded PDF. An authenticated sender can still send a wrong
invoice, a duplicate, or one over its PO; a compromised-but-authenticated
mailbox passes every check here perfectly. Email authentication is a signal
about the ENVELOPE, and this module is careful never to let it read as a
verdict about the CONTENT.

THE ONE IDEA THE WHOLE MODULE IS BUILT ON

`From:` is a display header. Anyone can type anything into it. So can anyone
into `Authentication-Results:`, `Received-SPF:`, or any `Received:` chain --
a message arrives as a blob of bytes, and *every* byte of it was chosen by
whoever sent it. There are only two things in that blob this process can
believe:

1. **A header stamped by a boundary we control and can name.** That is what
   `config.email_trusted_authserv_ids()` is: an allowlist of authserv-ids.
   Any `Authentication-Results` header carrying a different authserv-id is
   DISCARDED -- recorded as discarded, so an auditor can see the attempt, but
   never counted. With no ids configured (the default), nothing is believed
   and every message reads UNVERIFIED. That is the safe direction and it is
   why there is no "trust whatever the header says" fallback anywhere here.

2. **A cryptographic signature we verify ourselves.** DKIM is real
   public-key cryptography over the message's own bytes, so it does not
   matter who relayed it. `verify_dkim()` below does the actual RFC 6376
   work -- canonicalisation, body hash, signature verification -- against a
   public key supplied by a `DnsTxtResolver`.

WHAT IS AND IS NOT VERIFIABLE HERE (read before extending this)

* **DKIM -- genuinely verified**, when a public key can be obtained. The
  cryptography is done in this process against the message's real bytes.
* **SPF -- NOT verifiable from a stored message, ever.** SPF authorises the
  IP address that connected to the receiving server. A `.eml` does not
  contain that IP in any trustworthy form: `Received:` headers are just more
  attacker-chosen text, and the one Received header that IS trustworthy was
  written by the boundary -- which is the trusted-header case, not a local
  computation. So SPF is reported from a trusted `Authentication-Results`
  header or reported `unavailable`. It is never guessed, and `Received-SPF:`
  is never believed (it carries no authserv-id, so it cannot be attributed
  to our boundary at all).
* **DMARC alignment -- computed locally, and worth it.** Alignment is a
  comparison, not a lookup: does the domain that DKIM signed for, or that
  SPF authorised, match the domain in the visible `From`? That is exactly
  the check that catches a spoofed From riding on a real signature from
  somewhere else, and it needs no network. The DMARC *policy* (`p=`) does
  need DNS, and is reported `unavailable` when there is no resolver.
* **S/MIME and PGP -- a different thing entirely**, handled in
  `email_signature.py`. See that module: DKIM is a *domain* asserting it
  relayed a message; an S/MIME signature is a *person* asserting they wrote
  one. They are not substitutes and this codebase never treats them as such.

THREE STATES, NOT TWO

Every mechanism reports `pass`, `fail`, or `unavailable`, and the third is
load-bearing. "We checked and it failed" and "we could not check" are
different facts about a message, and collapsing them would either flag
honest senders as hostile or wave through unverified ones. They stay
distinct all the way through to the stored record.
"""
import base64
import binascii
import email
import email.policy
import email.utils
import hashlib
import re
import time

import config

# --------------------------------------------------------------------------
# Result vocabulary
# --------------------------------------------------------------------------

# The normalised three states every mechanism collapses to for decision
# purposes. The RFC 8601 result word that produced it is ALWAYS kept beside
# it (`result`), because "fail" and "softfail" want different responses and
# an audit trail that only recorded "fail" could not tell them apart later.
PASS = "pass"
FAIL = "fail"
UNAVAILABLE = "unavailable"

# RFC 8601 §2.7 result words -> our three states.
#
# `temperror` and `permerror` map to UNAVAILABLE, not FAIL, and that is a
# deliberate reading of the RFC: they mean the evaluation could not be
# completed (DNS timed out, the record was malformed), not that the sender
# failed it. Treating a DNS outage as an authentication failure would
# quarantine a vendor's invoices because someone else's nameserver was down.
_AR_RESULT_STATES = {
    "pass": PASS,
    "fail": FAIL,
    "softfail": FAIL,
    "policy": FAIL,
    "neutral": UNAVAILABLE,
    "none": UNAVAILABLE,
    "temperror": UNAVAILABLE,
    "permerror": UNAVAILABLE,
}

# Where a result came from. An auditor reading a stored record must be able
# to tell a relayed verdict from one this process computed itself, because
# they carry very different weight.
SOURCE_TRUSTED_AR = "trusted_authentication_results"
SOURCE_LOCAL_DKIM = "local_dkim_verification"
SOURCE_LOCAL_ALIGNMENT = "local_alignment_computation"
SOURCE_NONE = "not_evaluated"


def _result(mechanism, state, result=None, source=SOURCE_NONE, detail=None, properties=None):
    """One mechanism's outcome, in the shape everything downstream reads."""
    return {
        "mechanism": mechanism,
        "state": state,
        "result": result or state,
        "source": source,
        "detail": detail,
        "properties": properties or {},
    }


# --------------------------------------------------------------------------
# Public-key lookup: an interface, so the one part that needs the network is
# the only part that has to be swapped out.
#
# Same shape as documents.py's DocumentStore, and for the same reason: the
# rest of the module should not know or care where a key came from, and a
# deployment with no outbound DNS should degrade to `unavailable` rather than
# to a special code path.
# --------------------------------------------------------------------------
class DnsTxtResolver:
    """Returns the TXT strings published at a name, or [] if there are none."""

    def txt(self, name: str) -> list:
        raise NotImplementedError


class NullDnsTxtResolver(DnsTxtResolver):
    """Resolves nothing. The default.

    This is not a stub standing in for missing work -- it is the correct
    behaviour for a deployment that does no outbound DNS. Every lookup
    returns nothing, so DKIM reports `unavailable` (never `fail`, because a
    signature we could not fetch a key for has not failed anything) and DMARC
    policy reports `unavailable`.
    """

    def txt(self, name: str) -> list:
        return []


class StaticDnsTxtResolver(DnsTxtResolver):
    """Answers from a fixed dict of name -> TXT strings.

    Two real uses, not just tests: pinning a known vendor's DKIM public key
    the way `config.FX_RATES` pins an exchange rate -- reproducible, and
    auditable a year later in a way a live lookup never is -- and driving the
    verification tests with a real generated keypair and no network.
    """

    def __init__(self, records: dict = None):
        self.records = {k.lower().rstrip("."): v for k, v in (records or {}).items()}

    def txt(self, name: str) -> list:
        value = self.records.get((name or "").lower().rstrip("."))
        if value is None:
            return []
        return [value] if isinstance(value, str) else list(value)


class DnspythonTxtResolver(DnsTxtResolver):
    """Live DNS, for a deployment that wants it.

    `dnspython` is imported lazily inside the constructor and is NOT in
    requirements.txt -- exactly the arrangement S3DocumentStore uses for
    boto3, so an install that never sets EMAIL_DNS_RESOLVER=dnspython never
    needs the package, and one that does gets a clear error at construction
    rather than an ImportError at some later, less obvious moment.
    """

    def __init__(self, timeout: float = 5.0):
        try:
            import dns.resolver  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "EMAIL_DNS_RESOLVER=dnspython needs the 'dnspython' package "
                "installed (pip install dnspython)") from exc
        import dns.resolver
        self._resolver = dns.resolver.Resolver()
        self._resolver.lifetime = timeout
        self._dns = dns

    def txt(self, name: str) -> list:
        try:
            answers = self._resolver.resolve(name, "TXT")
        except Exception:
            # Any lookup problem -- NXDOMAIN, timeout, SERVFAIL -- is
            # "no key available", which the callers turn into `unavailable`.
            # None of them is evidence that the sender did anything wrong.
            return []
        out = []
        for rdata in answers:
            # A TXT record over 255 bytes arrives as several strings that the
            # publisher intended to be concatenated with nothing between them
            # (RFC 6376 §3.6.2.2 says so explicitly for DKIM keys).
            out.append(b"".join(rdata.strings).decode("utf-8", "replace"))
        return out


def resolver_from_config() -> DnsTxtResolver:
    return DnspythonTxtResolver() if config.email_dns_resolver() == "dnspython" else NullDnsTxtResolver()


# --------------------------------------------------------------------------
# Raw message handling
#
# The stdlib `email` package is used for MIME structure (parts, attachments,
# decoded filenames) because it is careful, tested, and handles encodings
# this module has no business reimplementing. It is NOT used for DKIM, which
# needs the header bytes exactly as they arrived -- any reparse-and-regenerate
# step would alter folding or whitespace and invalidate a valid signature.
# --------------------------------------------------------------------------
_MAX_HEADER_BYTES = 512 * 1024      # a header block larger than this is not a real message
_MAX_MIME_PARTS = 200               # MIME-bomb guard: stop walking, do not recurse forever
_MAX_MIME_DEPTH = 20


def normalise_eol(raw: bytes) -> bytes:
    """Bare LF -> CRLF, leaving existing CRLF alone.

    DKIM is defined over a CRLF-terminated message, but a message that has
    been through a file, a git checkout on Windows, or a naive test fixture
    may carry bare LFs. Normalising here means a signature that was valid on
    the wire is still valid after storage, and doing it in ONE place means
    the header split, the canonicalisation and the body hash cannot disagree
    about where the lines are.
    """
    return re.sub(rb"\r?\n", b"\r\n", raw or b"")


def split_raw(raw: bytes):
    """(header_block, body) split at the first empty line.

    Done on bytes, by hand, rather than via the email package -- see the
    section comment above.
    """
    raw = normalise_eol(raw)
    idx = raw.find(b"\r\n\r\n")
    if idx == -1:
        # A message with no body at all is legal; a message with no blank
        # line is malformed, but treating the whole thing as headers is the
        # reading that lets the classifier report on it rather than crash.
        return raw, b""
    return raw[:idx + 2], raw[idx + 4:]


def raw_header_fields(header_block: bytes):
    """[(lowercased-name, full-field-bytes-including-folding-and-CRLF)], in order.

    Continuation lines (those starting with space or tab) belong to the field
    above them and are kept attached, because DKIM's relaxed canonicalisation
    has to unfold them itself and simple canonicalisation has to hash them
    exactly as they are.
    """
    fields, current = [], []
    for line in header_block.split(b"\r\n"):
        if not line:
            continue
        if line[:1] in (b" ", b"\t") and current:
            current.append(line)
            continue
        if current:
            fields.append(b"\r\n".join(current) + b"\r\n")
            current = []
        if b":" not in line:
            # A header line with no colon is malformed. Dropped rather than
            # guessed at: inventing a field name for it would put
            # attacker-chosen text into the header list DKIM signs over.
            continue
        current = [line]
    if current:
        fields.append(b"\r\n".join(current) + b"\r\n")
    out = []
    for f in fields:
        name = f.split(b":", 1)[0].strip().lower()
        try:
            out.append((name.decode("ascii"), f))
        except UnicodeDecodeError:
            # A non-ASCII header NAME is a protocol violation and is skipped
            # for the same reason as a missing colon.
            continue
    return out


# --------------------------------------------------------------------------
# DKIM (RFC 6376) -- the one mechanism this process verifies for itself
# --------------------------------------------------------------------------
def _parse_tag_list(value: str) -> dict:
    """A DKIM/DMARC tag-value list: `v=1; a=rsa-sha256; d=example.com`."""
    tags = {}
    for part in (value or "").split(";"):
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        k = k.strip()
        if k:
            tags[k] = v.strip()
    return tags


def _canon_body_simple(body: bytes) -> bytes:
    """RFC 6376 §3.4.3."""
    body = body.rstrip(b"\r\n")
    return body + b"\r\n" if body else b"\r\n"


def _canon_body_relaxed(body: bytes) -> bytes:
    """RFC 6376 §3.4.4: squeeze runs of WSP, strip trailing WSP per line,
    drop trailing empty lines. An empty body canonicalises to nothing at all,
    which is why this cannot share `_canon_body_simple`'s tail."""
    lines = body.split(b"\r\n")
    out = []
    for line in lines:
        line = re.sub(rb"[ \t]+", b" ", line)
        out.append(line.rstrip(b" \t"))
    while out and out[-1] == b"":
        out.pop()
    if not out:
        return b""
    return b"\r\n".join(out) + b"\r\n"


def _canon_header_simple(field: bytes) -> bytes:
    """RFC 6376 §3.4.1: byte for byte, exactly as it arrived."""
    return field


def _canon_header_relaxed(field: bytes) -> bytes:
    """RFC 6376 §3.4.2: lowercase the name, unfold, squeeze WSP, strip the
    WSP around the colon and at the end of the value."""
    name, _, value = field.partition(b":")
    value = re.sub(rb"\r\n[ \t]+", b" ", value)      # unfold
    value = value.replace(b"\r\n", b"")
    value = re.sub(rb"[ \t]+", b" ", value)
    return name.strip().lower() + b":" + value.strip(b" \t") + b"\r\n"


def _selected_headers(fields, header_names, canon):
    """The signed header set, chosen the way RFC 6376 §5.4.2 requires.

    `h=` may name the same header twice, and a message may legitimately carry
    several copies of one header. The rule is bottom-up: the first mention of
    a name takes the LAST instance in the message, the second mention takes
    the one above it, and so on. Getting this backwards is the classic reason
    a hand-rolled verifier rejects valid mail, and it is also what makes a
    header ADDED above a signed one unable to smuggle itself into the
    signature.
    """
    remaining = {}
    for name, field in fields:
        remaining.setdefault(name, []).append(field)
    for name in remaining:
        remaining[name].reverse()
    chunks = []
    for name in header_names:
        name = name.strip().lower()
        pool = remaining.get(name)
        if not pool:
            # A header named in h= but absent from the message contributes
            # nothing (RFC 6376 §5.4). This is also how `h=` can oversign a
            # header to prove it was absent when signed.
            continue
        chunks.append(canon(pool.pop(0)))
    return b"".join(chunks)


def _dkim_public_key(tags: dict, resolver: DnsTxtResolver):
    """(key_object, error_string). Either the key, or why there isn't one."""
    domain, selector = tags.get("d", ""), tags.get("s", "")
    if not domain or not selector:
        return None, "signature is missing d= or s="
    name = f"{selector}._domainkey{'.' if domain else ''}{domain}"
    records = resolver.txt(name)
    if not records:
        return None, f"no DKIM public key available at {name}"
    for record in records:
        key_tags = _parse_tag_list(record)
        p = (key_tags.get("p") or "").replace(" ", "")
        if not p:
            # p= present but empty means the key was REVOKED (RFC 6376
            # §3.6.1). That is a real negative, unlike a missing record.
            if "p" in key_tags:
                return None, "revoked"
            continue
        try:
            der = base64.b64decode(p + "=" * (-len(p) % 4))
        except (binascii.Error, ValueError):
            continue
        key_type = (key_tags.get("k") or "rsa").lower()
        try:
            if key_type == "ed25519":
                from cryptography.hazmat.primitives.asymmetric import ed25519
                return ed25519.Ed25519PublicKey.from_public_bytes(der), None
            from cryptography.hazmat.primitives.serialization import load_der_public_key
            return load_der_public_key(der), None
        except Exception:
            continue
    return None, f"no usable DKIM public key at {name}"


def verify_dkim(raw: bytes, resolver: DnsTxtResolver = None):
    """Verify every DKIM-Signature on the message. Returns a list of results.

    This is real verification: the body is canonicalised and hashed, the hash
    is compared to `bh=`, the signed header set is rebuilt in the order the
    signature demands, and `b=` is checked against it with the domain's
    published public key. Nothing here is inferred from a header that merely
    claims a result.

    A signature we cannot fetch a key for comes back `unavailable`, never
    `fail` -- the signature has not failed, we simply could not check it, and
    the difference decides whether an honest vendor's invoice gets
    quarantined as hostile or held as unverified.
    """
    resolver = resolver or NullDnsTxtResolver()
    header_block, body = split_raw(raw)
    if len(header_block) > _MAX_HEADER_BYTES:
        return [_result("dkim", UNAVAILABLE, "permerror", SOURCE_LOCAL_DKIM,
                        "header block is implausibly large; not evaluated")]
    fields = raw_header_fields(header_block)
    sigs = [(n, f) for n, f in fields if n == "dkim-signature"]
    if not sigs:
        return []

    results = []
    for _, sig_field in sigs:
        _, _, sig_value = sig_field.partition(b":")
        tags = _parse_tag_list(sig_value.decode("utf-8", "replace").replace("\r\n", ""))
        props = {"d": tags.get("d"), "s": tags.get("s"), "a": tags.get("a"),
                 "i": tags.get("i")}

        if tags.get("v", "1") != "1":
            results.append(_result("dkim", UNAVAILABLE, "permerror", SOURCE_LOCAL_DKIM,
                                   "unsupported DKIM version", props))
            continue

        algo = (tags.get("a") or "rsa-sha256").lower()
        if algo not in ("rsa-sha256", "ed25519-sha256"):
            # rsa-sha1 is deliberately not supported. RFC 8301 forbids it,
            # and accepting it would let a weak signature read as a pass.
            results.append(_result("dkim", UNAVAILABLE, "permerror", SOURCE_LOCAL_DKIM,
                                   f"unsupported algorithm {algo}", props))
            continue

        header_canon, _, body_canon = (tags.get("c") or "simple/simple").partition("/")
        body_canon = body_canon or "simple"
        if header_canon not in ("simple", "relaxed") or body_canon not in ("simple", "relaxed"):
            results.append(_result("dkim", UNAVAILABLE, "permerror", SOURCE_LOCAL_DKIM,
                                   "unsupported canonicalisation", props))
            continue

        # x= is the signer's OWN expiry on its OWN signature (RFC 6376 §3.5).
        # Honoured, and treated as a failure rather than as `unavailable`,
        # because unlike a key we could not fetch this is not a gap in what we
        # could check: we checked, and the domain itself said this signature
        # stopped being valid at that time. Ignoring it would let a signature
        # captured before a deliberate key rotation keep verifying forever,
        # which is the exact replay x= exists to bound.
        expiry = tags.get("x")
        if expiry:
            try:
                expires_at = int(float(expiry))
            except ValueError:
                results.append(_result("dkim", UNAVAILABLE, "permerror", SOURCE_LOCAL_DKIM,
                                       "malformed x= expiry tag", props))
                continue
            props["x"] = expires_at
            issued = tags.get("t")
            if issued:
                try:
                    if int(float(issued)) >= expires_at:
                        # A signature that expires no later than it was made is
                        # malformed, not merely stale (RFC 6376 §3.5, x=).
                        results.append(_result(
                            "dkim", FAIL, "fail", SOURCE_LOCAL_DKIM,
                            "the signature expires no later than it was created", props))
                        continue
                except ValueError:
                    pass
            if time.time() > expires_at:
                results.append(_result(
                    "dkim", FAIL, "fail", SOURCE_LOCAL_DKIM,
                    "the signature has passed the expiry its own signer set (x=)", props))
                continue

        canon_body = (_canon_body_relaxed if body_canon == "relaxed"
                      else _canon_body_simple)(body)
        if tags.get("l"):
            # l= says only the first N bytes were signed, so anything appended
            # after them is unsigned. Honoured because the RFC defines it, but
            # recorded in `properties` -- a partial body signature is worth
            # far less than a whole one and an auditor should be able to see
            # that it was used.
            try:
                canon_body = canon_body[:int(tags["l"])]
                props["l"] = tags["l"]
            except ValueError:
                results.append(_result("dkim", UNAVAILABLE, "permerror", SOURCE_LOCAL_DKIM,
                                       "malformed l= tag", props))
                continue

        computed_bh = base64.b64encode(hashlib.sha256(canon_body).digest()).decode()
        if computed_bh != (tags.get("bh") or "").replace(" ", ""):
            # The body hash is checked BEFORE the signature: a mismatch means
            # the body changed after signing, which is a genuine failure and
            # needs no key to establish.
            results.append(_result("dkim", FAIL, "fail", SOURCE_LOCAL_DKIM,
                                   "body hash does not match the signature", props))
            continue

        key, key_error = _dkim_public_key(tags, resolver)
        if key is None:
            if key_error == "revoked":
                results.append(_result("dkim", FAIL, "fail", SOURCE_LOCAL_DKIM,
                                       "the signing key has been revoked", props))
            else:
                results.append(_result("dkim", UNAVAILABLE, "temperror", SOURCE_LOCAL_DKIM,
                                       key_error, props))
            continue

        canon_header = _canon_header_relaxed if header_canon == "relaxed" else _canon_header_simple
        signed = _selected_headers(fields, (tags.get("h") or "").split(":"), canon_header)
        # The DKIM-Signature header signs itself, with b= emptied and its
        # trailing CRLF removed (RFC 6376 §3.7).
        stripped = re.sub(rb"([;\s]b\s*=)[^;]*", rb"\1", sig_field, count=1)
        signed += canon_header(stripped).rstrip(b"\r\n")

        try:
            signature = base64.b64decode(re.sub(r"\s+", "", tags.get("b") or "") + "==")
        except (binascii.Error, ValueError):
            results.append(_result("dkim", FAIL, "permerror", SOURCE_LOCAL_DKIM,
                                   "malformed b= signature value", props))
            continue

        try:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding
            if algo == "ed25519-sha256":
                # RFC 8463: Ed25519-SHA256 signs the SHA-256 HASH of the
                # canonicalised headers, not the headers themselves.
                key.verify(signature, hashlib.sha256(signed).digest())
            else:
                key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
        except Exception:
            results.append(_result("dkim", FAIL, "fail", SOURCE_LOCAL_DKIM,
                                   "signature did not verify against the published key", props))
            continue

        results.append(_result("dkim", PASS, "pass", SOURCE_LOCAL_DKIM,
                               "signature verified", props))
    return results


# --------------------------------------------------------------------------
# Authentication-Results (RFC 8601), read only from a boundary we named
# --------------------------------------------------------------------------
_AR_METHOD_RE = re.compile(r"^\s*([a-z][a-z0-9-]*)\s*=\s*([a-z]+)", re.I)
_AR_PROPERTY_RE = re.compile(r"([a-z]+\.[a-z-]+)\s*=\s*([^\s;]+)", re.I)


def parse_authentication_results(value: str) -> dict:
    """One Authentication-Results header -> {authserv_id, methods{...}}."""
    value = re.sub(r"\r?\n[ \t]+", " ", value or "").strip()
    head, _, rest = value.partition(";")
    # The authserv-id may be followed by an optional version number.
    authserv_id = (head.strip().split() or [""])[0].strip().lower().rstrip(";")
    methods = {}
    for chunk in rest.split(";"):
        m = _AR_METHOD_RE.match(chunk)
        if not m:
            continue
        method, result = m.group(1).lower(), m.group(2).lower()
        props = {k.lower(): v.strip('"') for k, v in _AR_PROPERTY_RE.findall(chunk)}
        # A repeated method is kept only once; the first is the topmost and,
        # by RFC 8601's ordering, the most recent evaluation.
        methods.setdefault(method, {"result": result, "properties": props})
    return {"authserv_id": authserv_id, "methods": methods}


def collect_authentication_results(fields, trusted_ids):
    """(trusted[], discarded[]) -- split by whether we named the stamper.

    The discarded list is kept and stored. A message carrying a forged
    `Authentication-Results: ... dmarc=pass` is not just "not trusted", it is
    *interesting*, and an auditor looking at a quarantined message should be
    able to see that someone tried it.
    """
    trusted, discarded = [], []
    for name, field in fields:
        if name != "authentication-results":
            continue
        _, _, value = field.partition(b":")
        parsed = parse_authentication_results(value.decode("utf-8", "replace"))
        parsed["raw"] = value.decode("utf-8", "replace").strip()[:2000]
        (trusted if parsed["authserv_id"] in trusted_ids else discarded).append(parsed)
    return trusted, discarded


# --------------------------------------------------------------------------
# Domains and DMARC alignment
# --------------------------------------------------------------------------

# Multi-label public suffixes common enough to matter here. This is a
# HEURISTIC, not the Public Suffix List -- see `organizational_domain`.
_MULTI_LABEL_SUFFIXES = frozenset("""
co.uk org.uk me.uk ltd.uk plc.uk net.uk sch.uk ac.uk gov.uk nhs.uk
com.au net.au org.au edu.au gov.au id.au
co.nz net.nz org.nz govt.nz ac.nz
co.za org.za net.za web.za
co.jp or.jp ne.jp ac.jp go.jp
co.in net.in org.in gen.in firm.in ind.in
com.br net.br org.br gov.br
com.sg net.sg org.sg edu.sg gov.sg
com.mx org.mx gob.mx
com.cn net.cn org.cn gov.cn edu.cn
com.hk org.hk net.hk
com.tr net.tr org.tr gov.tr
co.kr or.kr ne.kr
com.ar com.co com.pe com.ph com.my com.tw com.vn com.pk com.eg com.sa
""".split())


def domain_of(address: str) -> str:
    """The domain part of an address, lowercased, or "" if there isn't one."""
    addr = (address or "").strip().strip("<>")
    if "@" not in addr:
        return ""
    return addr.rsplit("@", 1)[1].strip().strip(">").lower().rstrip(".")


def organizational_domain(domain: str) -> str:
    """The registrable domain, by heuristic.

    HONEST LIMITATION, stated here because relaxed DMARC alignment depends on
    it: this is a short list of common multi-label suffixes, not the Public
    Suffix List. For a suffix it does not know (`something.pvt.ltd.xx`) it
    will take one label too few, which would make two unrelated domains under
    that suffix look organizationally aligned.

    That is why the classifier never treats relaxed alignment as sufficient
    on its own for anything beyond what DMARC itself specifies, records both
    the strict and relaxed answers separately in the audit record, and why
    STRICT alignment -- an exact match, no heuristic involved -- is what a
    deployment should require if it cares. Swapping this for a real PSL
    lookup is a self-contained change to this one function.
    """
    domain = (domain or "").lower().strip().rstrip(".")
    labels = [l for l in domain.split(".") if l]
    if len(labels) < 2:
        return domain
    if ".".join(labels[-2:]) in _MULTI_LABEL_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def alignment(from_domain: str, auth_domain: str) -> dict:
    """Strict and relaxed DMARC alignment between two domains."""
    if not from_domain or not auth_domain:
        return {"strict": False, "relaxed": False, "auth_domain": auth_domain or None}
    from_domain, auth_domain = from_domain.lower(), auth_domain.lower()
    return {
        "strict": from_domain == auth_domain,
        "relaxed": organizational_domain(from_domain) == organizational_domain(auth_domain),
        "auth_domain": auth_domain,
    }


def lookup_dmarc_policy(from_domain: str, resolver: DnsTxtResolver):
    """The published DMARC record for a domain, or None when unavailable.

    Needs DNS, so with the default null resolver this is always None -- which
    the classifier reports as an unavailable policy, never as an absent one.
    "The domain publishes no DMARC record" and "we could not look" are, once
    again, different facts.
    """
    org = organizational_domain(from_domain)
    for candidate in [d for d in (from_domain, org) if d]:
        for record in resolver.txt(f"_dmarc.{candidate}"):
            tags = _parse_tag_list(record)
            if (tags.get("v") or "").upper() == "DMARC1":
                return {"domain": candidate, "policy": (tags.get("p") or "none").lower(),
                        "adkim": (tags.get("adkim") or "r").lower(),
                        "aspf": (tags.get("aspf") or "r").lower(),
                        "record": record[:500]}
    return None


# --------------------------------------------------------------------------
# Parsing a message into the facts the classifier needs
# --------------------------------------------------------------------------
def _decoded_header(msg, name):
    try:
        value = msg.get(name)
        return str(value) if value is not None else None
    except Exception:
        # A header with a broken RFC 2047 encoded-word can raise during
        # decoding. A malformed header must never take the whole evaluation
        # down -- it is a reason to be suspicious of the message, not to
        # return a 500 to the caller.
        return None


def _attachments(msg):
    """Attachment METADATA only. No bytes are returned, kept or stored.

    Phase F never opens an attachment for content -- extracting the invoice
    from it is Phase G's job. What is needed here is only enough to describe
    the message in the security record: what came with it, how big, and a
    hash so the same attachment can be recognised later.
    """
    out, seen = [], 0
    for part in msg.walk():
        if seen >= _MAX_MIME_PARTS:
            break
        if part.is_multipart():
            continue
        seen += 1
        disposition = (part.get_content_disposition() or "")
        filename = part.get_filename()
        if disposition != "attachment" and not filename:
            continue
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:
            payload = b""
        out.append({
            "filename": _safe_attachment_name(filename),
            "content_type": (part.get_content_type() or "").lower(),
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest() if payload else None,
        })
    return out


def _safe_attachment_name(name):
    """The same treatment main.py gives an uploaded filename: this string is
    attacker-controlled and ends up in the database and on screen."""
    if not name:
        return None
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable() and ch not in r'\/:*?"<>|')
    return (name.strip(". ") or None) if name else None


def parse_message(raw: bytes) -> dict:
    """Everything the classifier reads, extracted once.

    `findings` collects structural problems as they are noticed. Multiple
    `From:` headers is the one that matters most: it is legal in no reading
    of RFC 5322, different clients disagree about which one to display, and
    that disagreement is precisely the trick -- one From for the human, a
    different one for the authentication check.
    """
    raw = normalise_eol(raw)
    header_block, body = split_raw(raw)
    fields = raw_header_fields(header_block)
    findings = []

    try:
        msg = email.message_from_bytes(raw, policy=email.policy.default)
    except Exception:
        msg = None
    if msg is None:
        try:
            msg = email.message_from_bytes(raw)          # permissive fallback
        except Exception:
            msg = None
    if msg is None:
        findings.append("the message could not be parsed as an email at all")

    from_fields = [f for n, f in fields if n == "from"]
    if len(from_fields) > 1:
        findings.append(
            f"the message carries {len(from_fields)} From headers; RFC 5322 permits one, "
            "and clients disagree about which is displayed")
    if not from_fields:
        findings.append("the message has no From header")

    from_raw = _decoded_header(msg, "From") if msg is not None else None
    if from_raw is None and from_fields:
        from_raw = from_fields[0].partition(b":")[2].decode("utf-8", "replace").strip()
    display_name, from_address = ("", "")
    if from_raw:
        try:
            display_name, from_address = email.utils.parseaddr(from_raw)
        except Exception:
            findings.append("the From header could not be parsed as an address")
    # A From value with more than one address is the same trick as more than
    # one From header, one level down.
    if from_raw and len(email.utils.getaddresses([from_raw])) > 1:
        findings.append("the From header lists more than one address")

    from_domain = domain_of(from_address)
    if from_fields and not from_domain:
        findings.append("the From header carries no usable domain")

    return_path = _decoded_header(msg, "Return-Path") if msg is not None else None
    envelope_from = email.utils.parseaddr(return_path)[1] if return_path else ""

    subject = _decoded_header(msg, "Subject") if msg is not None else None
    if subject and ("\n" in subject or "\r" in subject):
        findings.append("the Subject header contains embedded newlines")
        subject = subject.replace("\r", " ").replace("\n", " ")

    message_id = _decoded_header(msg, "Message-ID") if msg is not None else None

    # Reply-To and the recipient list are carried for Phase G's triage and for
    # the ingestion record. They are DESCRIPTIVE only and contribute nothing to
    # the classification below -- a Reply-To pointing elsewhere is normal for a
    # mailing list and is also how a reply gets redirected to an attacker, so
    # it is a signal for a human, never a verdict.
    reply_to_raw = _decoded_header(msg, "Reply-To") if msg is not None else None
    reply_to = ""
    if reply_to_raw:
        try:
            reply_to = email.utils.parseaddr(reply_to_raw)[1]
        except Exception:
            reply_to = ""
    recipients = []
    for header in ("To", "Cc"):
        value = _decoded_header(msg, header) if msg is not None else None
        if not value:
            continue
        try:
            recipients.extend(a for _, a in email.utils.getaddresses([value]) if a)
        except Exception:
            continue

    attachments = _attachments(msg) if msg is not None else []

    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "message_id": (message_id or "").strip()[:400] or None,
        "date": (_decoded_header(msg, "Date") if msg is not None else None),
        "from_display_name": (display_name or "").strip()[:200] or None,
        "from_address": (from_address or "").strip().lower()[:320] or None,
        "from_domain": from_domain or None,
        "from_header_count": len(from_fields),
        "envelope_from": (envelope_from or "").strip().lower()[:320] or None,
        "envelope_from_domain": domain_of(envelope_from) or None,
        "reply_to": (reply_to or "").strip().lower()[:320] or None,
        "recipients": [r.strip().lower()[:320] for r in recipients][:50],
        "subject": (subject or "").strip()[:500] or None,
        "attachments": attachments,
        "attachment_count": len(attachments),
        "has_pdf_attachment": any(
            (a["content_type"] == "application/pdf")
            or (a["filename"] or "").lower().endswith(".pdf") for a in attachments),
        "findings": findings,
        "_fields": fields,
        "_message": msg,
    }


# --------------------------------------------------------------------------
# Classification -- deterministic, and built as it evaluates
#
# Same construction as rules.decide(): the audit record is assembled next to
# the branch that sets the verdict, not by a second pass afterwards that
# could disagree with it. No model is involved and none ever should be.
# --------------------------------------------------------------------------
def _trusted_sender_match(parsed, trusted_senders):
    """Is this sender one the business expects invoices from?

    Exact address first, then the domain, then the organizational domain.
    Matching the org domain is deliberately last and recorded distinctly:
    `invoices.acme.example` matching an allowlisted `acme.example` is usually
    right, and occasionally is a subdomain someone else controls.
    """
    address = (parsed.get("from_address") or "").lower()
    domain = (parsed.get("from_domain") or "").lower()
    for entry in trusted_senders or []:
        sender = (entry.get("sender") or "").lower().strip()
        if not sender or (entry.get("status") or "trusted").lower() != "trusted":
            continue
        kind = (entry.get("kind") or ("address" if "@" in sender else "domain")).lower()
        if kind == "address" and address and address == sender:
            return {"matched": True, "on": "address", "entry": entry}
        if kind == "domain" and domain:
            if domain == sender:
                return {"matched": True, "on": "domain", "entry": entry}
            if organizational_domain(domain) == organizational_domain(sender):
                return {"matched": True, "on": "organizational_domain", "entry": entry}
    return {"matched": False, "on": None, "entry": None}


def classify(raw: bytes, trusted_senders=None, resolver: DnsTxtResolver = None,
             trusted_authserv_ids=None, signature_verifier=None) -> dict:
    """Evaluate one message. Returns the full security record.

    The verdict is one of config.EMAIL_CLASSIFICATIONS:

      VERIFIED    -- an aligned, passing authentication result was obtained
                     from evidence this process is entitled to believe, and
                     the sender is one the business expects invoices from.
      FAILED      -- something was checked and did not pass: a signature that
                     did not verify, a trusted boundary reporting a failure,
                     a From header that is structurally a spoof.
      SUSPICIOUS  -- signals that do not agree with each other, or an
                     authenticated sender nobody put on the allowlist.
      UNVERIFIED  -- nothing could be checked. NOT an accusation. This is the
                     default state of a deployment with no trusted boundary
                     configured and no DNS resolver, and it means exactly
                     "held pending a human", never "hostile".
    """
    from email_signature import get_verifier      # local: avoids a circular import

    resolver = resolver if resolver is not None else resolver_from_config()
    trusted_ids = tuple(trusted_authserv_ids if trusted_authserv_ids is not None
                        else config.email_trusted_authserv_ids())
    verifier = signature_verifier or get_verifier()

    parsed = parse_message(raw)
    fields = parsed.pop("_fields")
    msg = parsed.pop("_message")
    findings = list(parsed["findings"])
    from_domain = parsed.get("from_domain") or ""

    trusted_ar, discarded_ar = collect_authentication_results(fields, trusted_ids)
    if discarded_ar:
        findings.append(
            f"{len(discarded_ar)} Authentication-Results header(s) came from an "
            "authentication server this deployment does not trust, and were ignored")
    # Never believed, at all -- it carries no authserv-id, so there is no way
    # to attribute it to our boundary rather than to the sender. Recorded as
    # evidence only.
    received_spf = [f.partition(b":")[2].decode("utf-8", "replace").strip()[:500]
                    for n, f in fields if n == "received-spf"]

    # ---- SPF: only ever relayed, never computed here (see module docstring)
    spf = _result("spf", UNAVAILABLE, "none", SOURCE_NONE,
                  "SPF authorises the connecting IP address, which a stored message "
                  "cannot establish; no trusted Authentication-Results header supplied one")
    for ar in trusted_ar:
        m = ar["methods"].get("spf")
        if m:
            spf = _result("spf", _AR_RESULT_STATES.get(m["result"], UNAVAILABLE),
                          m["result"], SOURCE_TRUSTED_AR,
                          f"reported by {ar['authserv_id']}", m["properties"])
            break

    # ---- DKIM: verified here when possible, relayed otherwise
    local_dkim = verify_dkim(raw, resolver)
    relayed_dkim = None
    for ar in trusted_ar:
        m = ar["methods"].get("dkim")
        if m:
            relayed_dkim = _result("dkim", _AR_RESULT_STATES.get(m["result"], UNAVAILABLE),
                                   m["result"], SOURCE_TRUSTED_AR,
                                   f"reported by {ar['authserv_id']}", m["properties"])
            break

    # Our own cryptography outranks a relayed claim about it -- a boundary can
    # be wrong or out of date, and we did the arithmetic ourselves.
    passing_local = [d for d in local_dkim if d["state"] == PASS]
    failing_local = [d for d in local_dkim if d["state"] == FAIL]
    if passing_local:
        dkim = passing_local[0]
    elif failing_local:
        dkim = failing_local[0]
    elif relayed_dkim is not None:
        dkim = relayed_dkim
    elif local_dkim:
        dkim = local_dkim[0]
    else:
        dkim = _result("dkim", UNAVAILABLE, "none", SOURCE_NONE,
                       "the message carries no DKIM signature")

    # A trusted boundary saying dkim=pass while our own verification says the
    # signature does NOT verify is exactly the kind of disagreement that must
    # never be resolved by picking the friendlier answer.
    conflicts = []
    if relayed_dkim is not None and passing_local and relayed_dkim["state"] == FAIL:
        conflicts.append(
            f"{relayed_dkim['detail']} reports dkim={relayed_dkim['result']}, but the "
            "signature verifies cryptographically here")
    if relayed_dkim is not None and failing_local and not passing_local \
            and relayed_dkim["state"] == PASS:
        conflicts.append(
            f"{relayed_dkim['detail']} reports dkim={relayed_dkim['result']}, but no "
            "signature on this message verifies cryptographically here")
    if len(trusted_ar) > 1:
        verdicts = {ar["methods"].get("dmarc", {}).get("result")
                    for ar in trusted_ar if ar["methods"].get("dmarc")}
        if len(verdicts - {None}) > 1:
            conflicts.append(
                "trusted Authentication-Results headers disagree about DMARC: "
                + ", ".join(sorted(v for v in verdicts if v)))

    # ---- DMARC: alignment computed here, policy looked up if we can
    dkim_domain = (dkim.get("properties") or {}).get("d") \
        or (dkim.get("properties") or {}).get("header.d")
    spf_domain = (spf.get("properties") or {}).get("smtp.mailfrom") \
        or parsed.get("envelope_from_domain")
    dkim_alignment = alignment(from_domain, domain_of(dkim_domain) or dkim_domain or "")
    spf_alignment = alignment(from_domain, domain_of(spf_domain) or spf_domain or "")

    policy = lookup_dmarc_policy(from_domain, resolver) if from_domain else None
    strict_dkim = policy and policy.get("adkim") == "s"
    strict_spf = policy and policy.get("aspf") == "s"
    dkim_aligned = dkim_alignment["strict"] if strict_dkim else dkim_alignment["relaxed"]
    spf_aligned = spf_alignment["strict"] if strict_spf else spf_alignment["relaxed"]

    dkim_ok = dkim["state"] == PASS and dkim_aligned
    spf_ok = spf["state"] == PASS and spf_aligned

    relayed_dmarc = None
    for ar in trusted_ar:
        m = ar["methods"].get("dmarc")
        if m:
            relayed_dmarc = _result("dmarc", _AR_RESULT_STATES.get(m["result"], UNAVAILABLE),
                                    m["result"], SOURCE_TRUSTED_AR,
                                    f"reported by {ar['authserv_id']}", m["properties"])
            break

    if dkim_ok or spf_ok:
        which = "DKIM" if dkim_ok else "SPF"
        dmarc = _result("dmarc", PASS, "pass", SOURCE_LOCAL_ALIGNMENT,
                        f"{which} passed and is aligned with the From domain "
                        f"({from_domain})",
                        {"dkim_alignment": dkim_alignment, "spf_alignment": spf_alignment})
    elif dkim["state"] == PASS or spf["state"] == PASS:
        # THE CASE DMARC EXISTS FOR. A mechanism passed, for a domain that is
        # not the one the reader sees in From. A real signature from
        # somewhere else does not authenticate this From.
        passer = "DKIM" if dkim["state"] == PASS else "SPF"
        other = (dkim_alignment if dkim["state"] == PASS else spf_alignment)["auth_domain"]
        dmarc = _result("dmarc", FAIL, "fail", SOURCE_LOCAL_ALIGNMENT,
                        f"{passer} passed for {other or 'an unstated domain'}, which is not "
                        f"aligned with the From domain ({from_domain or 'unknown'})",
                        {"dkim_alignment": dkim_alignment, "spf_alignment": spf_alignment})
    elif dkim["state"] == FAIL or spf["state"] == FAIL:
        dmarc = _result("dmarc", FAIL, "fail", SOURCE_LOCAL_ALIGNMENT,
                        "no mechanism passed, and at least one failed",
                        {"dkim_alignment": dkim_alignment, "spf_alignment": spf_alignment})
    else:
        dmarc = _result("dmarc", UNAVAILABLE, "none", SOURCE_NONE,
                        "neither SPF nor DKIM could be evaluated, so alignment says nothing",
                        {"dkim_alignment": dkim_alignment, "spf_alignment": spf_alignment})

    if relayed_dmarc is not None and relayed_dmarc["state"] != dmarc["state"] \
            and dmarc["state"] != UNAVAILABLE:
        conflicts.append(
            f"{relayed_dmarc['detail']} reports dmarc={relayed_dmarc['result']}, but "
            f"alignment computed here says {dmarc['state']}")
    if relayed_dmarc is not None and dmarc["state"] == UNAVAILABLE:
        # Nothing to compute against locally, so the relayed verdict is all
        # there is -- and it came from a boundary we named, so it counts.
        dmarc = relayed_dmarc

    # ---- user-level digital signature: a different question entirely
    signature = verifier.verify(msg)

    trusted_sender = _trusted_sender_match(parsed, trusted_senders)
    allowlist_configured = bool(trusted_senders)

    # ---- the verdict
    reasons = []
    structural_spoof = [f for f in findings if "From header" in f]
    if structural_spoof:
        classification = "FAILED"
        reasons.extend(structural_spoof)
    elif dmarc["state"] == FAIL:
        classification = "FAILED"
        reasons.append(dmarc["detail"])
    elif dkim["state"] == FAIL:
        classification = "FAILED"
        reasons.append(f"DKIM: {dkim['detail']}")
    elif spf["state"] == FAIL:
        classification = "FAILED"
        reasons.append(f"SPF reported {spf['result']} by a trusted boundary")
    elif conflicts:
        classification = "SUSPICIOUS"
        reasons.extend(conflicts)
    elif dmarc["state"] == PASS:
        if allowlist_configured and not trusted_sender["matched"]:
            classification = "SUSPICIOUS"
            reasons.append(
                f"the sender ({parsed.get('from_address') or 'unknown'}) is authenticated "
                "but is not on the trusted-sender list")
        else:
            classification = "VERIFIED"
            reasons.append(dmarc["detail"])
            if trusted_sender["matched"]:
                reasons.append(
                    f"the sender is on the trusted-sender list (matched on "
                    f"{trusted_sender['on']})")
    else:
        classification = "UNVERIFIED"
        reasons.append(
            "no authentication result could be obtained for this message; this is a "
            "gap in what could be checked, not a failed check")

    if conflicts and classification == "FAILED":
        reasons.extend(conflicts)

    # A quarantined message is HELD, exactly as a NEEDS_REVIEW invoice is
    # held: a person decides, and until they do it goes no further.
    status = "ADMITTED" if classification == "VERIFIED" else "QUARANTINED"

    audit = {
        "classification": classification,
        "status": status,
        "reasons": reasons,
        "evaluated_mechanisms": {"spf": spf, "dkim": dkim, "dmarc": dmarc},
        "dkim_signatures": local_dkim,
        "dmarc": {
            "policy": policy,
            "policy_source": "dns" if policy else "unavailable",
            "dkim_alignment": dkim_alignment,
            "spf_alignment": spf_alignment,
            "alignment_mode": {
                "dkim": "strict" if strict_dkim else "relaxed",
                "spf": "strict" if strict_spf else "relaxed",
            },
        },
        "digital_signature": signature,
        "trusted_sender": {
            "matched": trusted_sender["matched"],
            "matched_on": trusted_sender["on"],
            "vendor_name": (trusted_sender["entry"] or {}).get("vendor_name"),
            "allowlist_configured": allowlist_configured,
        },
        "evidence": {
            "trusted_authentication_results": trusted_ar,
            "discarded_authentication_results": discarded_ar,
            "trusted_authserv_ids": list(trusted_ids),
            "received_spf_headers_ignored": received_spf,
            "dns_resolver": resolver.__class__.__name__,
        },
        "findings": findings,
        "conflicts": conflicts,
        # Stated in the record itself, not just in documentation, so it
        # travels with the verdict wherever the verdict goes.
        "limitations": [
            "An authentication pass proves the message's claimed origin, not that the "
            "invoice inside it is legitimate. A compromised but authenticated mailbox "
            "passes every check here.",
            "SPF is never computed locally: it authorises a connecting IP address that a "
            "stored message cannot establish.",
            "Relaxed DMARC alignment uses a heuristic public-suffix list, not the full "
            "Public Suffix List.",
        ],
    }

    parsed.pop("findings", None)
    record = dict(parsed)
    record.update({
        "classification": classification,
        "status": status,
        "reasons": reasons,
        "spf_result": spf["state"],
        "spf_detail": spf["result"],
        "dkim_result": dkim["state"],
        "dkim_detail": dkim["result"],
        "dmarc_result": dmarc["state"],
        "dmarc_detail": dmarc["result"],
        "dmarc_aligned": bool(dkim_aligned or spf_aligned),
        "signature_kind": signature["kind"],
        "signature_result": signature["state"],
        "trusted_sender": trusted_sender["matched"],
        "audit": audit,
    })
    return record
