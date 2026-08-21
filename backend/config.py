"""Runtime configuration.

The Gemini API key is read from the environment or a local .env file. It is
never accepted over HTTP and never returned to the browser -- the UI is only told
whether a key is present, not what it is.
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")
ENV_PATH = os.path.join(ROOT, ".env")

# Upload guardrails
MAX_UPLOAD_BYTES = 15 * 1024 * 1024   # 15 MB
MAX_PAGES_TEXT = 25                   # pages scanned for embedded text
MAX_PAGES_VISION = 3                  # pages sent to the vision model

# --------------------------------------------------------------------------
# PO tolerance policy
#
# How far OVER the remaining PO balance an invoice may go and still auto-approve.
# The allowance exists for tax and freight added after the PO was raised, which
# a buyer legitimately cannot predict to the cent.
#
# The effective tolerance is the LARGER of the two, so small POs get a usable
# floor and large POs scale. Under-billing is not bounded by this at all -- a
# partial invoice is normal and is handled separately (see matching.match_po).
#
# These are the first business rules to leave the code. Phase 3 moves the whole
# policy to a versioned rules.yaml; until then, changing a number here changes
# what the process approves, so treat edits as a policy change, not a tweak.
PO_TOLERANCE_PERCENT = 0.01   # 1% of the remaining balance
PO_TOLERANCE_DOLLARS = 50.0   # ...or this, whichever is larger

# --------------------------------------------------------------------------
# Invoice arithmetic tolerance
#
# How far `subtotal + tax` may sit from the printed `total` before the invoice
# is treated as internally inconsistent.
#
# Deliberately NOT the PO tolerance above. That one is a business allowance for
# charges a buyer could not predict when the PO was raised; this one is pure
# floating-point and cash-rounding slack. Reusing the $50 PO figure here would
# let a $40 arithmetic error through, which is exactly the kind of error this
# check exists to catch.
#
# 5 cents: currency arithmetic is exact to the cent, so the only legitimate
# drift is per-line rounding (a cent or two) or jurisdictions that round the
# cash total to the nearest 0.05. Anything larger is a real discrepancy.
ARITHMETIC_TOLERANCE_DOLLARS = 0.05

# --------------------------------------------------------------------------
# Extraction confidence gate
#
# Every extracted field carries provenance: a confidence score, where it came
# from, and a quoted snippet backing it (see ExtractedInvoice.provenance).
# LLM routes get the score from the MODEL ITSELF, asked to self-report per
# field alongside the value. Regex gets a deterministic heuristic instead (an
# explicit labelled match scores high; a positionally-guessed vendor name or a
# computed-not-printed total scores lower) -- see extraction.py.
#
# Honest limitation, stated here rather than presented as measured accuracy:
# model self-reported confidence is known to skew high and is not independently
# calibrated. It is still a genuine signal -- a model unsure about a field it
# read is meaningfully different from one that read it cleanly -- just not a
# guarantee.
#
# Only fields in CONFIDENCE_GATED_FIELDS can hold up a decision: the ones
# REQUIRED_FIELDS already treats as central. Gating on every field (line
# items, dates) would send almost any invoice to review regardless of whether
# anything is actually wrong with it. A field the extractor never found at all
# is validate_required_fields()'s business, not this gate's -- this only fires
# when a value IS present but the extractor itself is not confident in it,
# which is a different failure class (a reading-quality problem, not a missing-
# data problem) and is reported as its own finding.
#
# Like every other extraction-uncertainty signal in this pipeline (unreadable
# scan, injection guard), this only ever HOLDS for review. It never rejects --
# low confidence about a reading is not evidence the invoice itself is wrong.
CONFIDENCE_THRESHOLD = 0.65
CONFIDENCE_GATED_FIELDS = ["vendor_name", "invoice_number", "total"]

# --------------------------------------------------------------------------
# Foreign-exchange rates (USD per 1 unit of the currency)
#
# PINNED, not fetched live -- the same argument as pinning the extraction
# models below: an AP process must be able to say which rate approved an
# invoice months ago, and a rate fetched at run time is not reproducible by an
# auditor. Whenever this table changes, bump FX_RATES_VERSION; every converted
# figure in the audit trail is stamped with the version that produced it, so a
# later rate change cannot silently reinterpret an old decision.
#
# A currency with no entry here cannot be converted at all -- match_po() then
# holds the invoice for a human rather than guessing at a rate, exactly like a
# provider outage falls back to a safe path instead of fabricating a value.
# --------------------------------------------------------------------------
FX_RATES_VERSION = "2026-08-01"
FX_RATES = {
    "USD": 1.0,
    "EUR": 1.08,
    "GBP": 1.27,
    "INR": 0.012,
    "CAD": 0.73,
}

# --------------------------------------------------------------------------
# Extraction providers
#
# TWO providers, split by what the document physically is:
#
#   PDF with a usable text layer  ->  Groq            (most invoices)
#   image-only / scanned PDF      ->  Gemini Vision   (reads page images)
#
# This split is an economics decision, not an architectural one. Gemini's free
# tier allows 20 requests per DAY per model, which a single demo run of seven
# invoices very nearly exhausts; Groq is fast and far more generous on text. So
# the scarce resource is spent only where it is the only option -- reading a
# scan. Groq is text-only in this pipeline and never sees a page image.
#
# Nothing downstream knows or cares which provider ran: both return the same
# ExtractedInvoice, and every decision after extraction is plain Python.
# --------------------------------------------------------------------------

# Google Gemini via Google AI Studio -- the VISION route.
API_KEY_ENV = "GEMINI_API_KEY"
# Pinned to a specific version, not the "gemini-flash-latest" alias: an alias
# silently changes the model under a running system, and an AP process has to
# be able to say which model read an invoice that was approved months ago.
# gemini-2.0-flash was retired -- the API returns 404 for it.
EXTRACTION_MODEL = "gemini-3.7-flash"

# Groq -- the TEXT route.
GROQ_API_KEY_ENV = "GROQ_API_KEY"
GROQ_MODEL_ENV = "GROQ_MODEL"
# Same pinning argument as Gemini above: a named model, never a floating alias,
# so a run can always say which model read the invoice. Overridable through the
# environment so swapping it is a config change rather than a code change.
#
# Chosen by asking the API what it could actually reach and then measuring, not
# from memory -- `llama-3.3-70b-versatile` is NOT available on this account, the
# same trap `gemini-2.0-flash` sprang earlier. Against all six text samples
# gpt-oss-120b and gpt-oss-20b extracted every field identically and correctly;
# 120b was the faster of the two in aggregate (9.7s vs 15.0s) and is the larger
# model, so it wins on both robustness and demo speed.
GROQ_MODEL_DEFAULT = "openai/gpt-oss-120b"


# --------------------------------------------------------------------------
# API security
#
# Operational settings only, like everything else in this file -- no business
# rules. The signing secret itself is NEVER stored here; it comes from the
# environment, and auth.signing_secret() generates an ephemeral one rather than
# falling back to a value committed to the repository.
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Environment
#
# One switch separates "a case study running on a laptop" from "a deployment
# handling someone's payables". Everything that is convenient-but-unsafe --
# demo accounts, an ephemeral signing key -- is allowed in development and
# refused outright in production, at startup, before the app serves a request.
#
# Read at call time rather than bound at import, because load_dotenv() runs
# after this module is imported and a constant would miss a value set in .env.
# --------------------------------------------------------------------------
APP_ENV_VAR = "APP_ENV"
PRODUCTION_NAMES = ("production", "prod", "live")


def app_env() -> str:
    return os.environ.get(APP_ENV_VAR, "development").strip().lower()


def is_production() -> bool:
    return app_env() in PRODUCTION_NAMES


AUTH_SECRET_ENV = "AUTH_SECRET"
AUTH_ISSUER = os.environ.get("AUTH_ISSUER", "invoice-processing")
AUTH_TOKEN_TTL_MINUTES = int(os.environ.get("AUTH_TOKEN_TTL_MINUTES", "480") or 480)
USERS_SEED = os.path.join(ROOT, "data", "users.json")

# --------------------------------------------------------------------------
# Database
#
# PostgreSQL is the live application database, in both development and
# production -- there is no SQLite fallback. Read at call time, like the
# signing secret and API keys above, for the same reason: load_dotenv() runs
# after this module is imported, so a module-level constant would silently
# miss a value set in .env.
#
# No hardcoded host, user or password ever lives here, same principle as
# AUTH_SECRET: a default connection string committed to the repository is not
# a secret, and a deployment that forgot to set one should fail loudly rather
# than silently talk to some other database.
# --------------------------------------------------------------------------
DATABASE_URL_ENV = "DATABASE_URL"


def database_url() -> str:
    """The Postgres connection string, or raise if it is not configured.

    Raising here rather than falling back to a default is deliberate: a
    process that started against the wrong database (or none) should fail the
    first time it tries to use one, not proceed and produce confusing errors
    three calls later.
    """
    url = os.environ.get(DATABASE_URL_ENV, "").strip()
    if not url:
        raise RuntimeError(
            f"{DATABASE_URL_ENV} is not set. Point it at a PostgreSQL instance, e.g. "
            f"postgresql://user:password@localhost:5432/invoice_processing")
    return url

# CORS. Configured so a browser on another origin can be allowed deliberately,
# NOT as a security control: CORS is enforced by the browser, and a script or
# curl request ignores it entirely. Authentication is the boundary; this only
# decides which origins a browser is permitted to make credentialed calls from.
# Default is same-origin only, which is how the app is actually served.
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]

# --------------------------------------------------------------------------
# Rate limiting
#
# The resource being protected is extraction quota. Gemini's free tier is 20
# requests per DAY, so an unattended script hitting the processing endpoint is
# not a theoretical concern -- it would exhaust the scanned-invoice route in
# under a minute and leave the process unable to read a scan at all.
# --------------------------------------------------------------------------
RATE_LIMIT_ENABLED = os.environ.get("RATE_LIMIT_ENABLED", "1").strip() not in ("0", "false", "False", "")
# Per authenticated user, on the processing endpoint.
RATE_LIMIT_PROCESS_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PROCESS_PER_MINUTE", "20") or 20)
# Per client IP, as a second line for the unauthenticated surface (the token
# endpoint) and as a backstop when many users share one host.
RATE_LIMIT_IP_PER_MINUTE = int(os.environ.get("RATE_LIMIT_IP_PER_MINUTE", "60") or 60)
# Login attempts per IP per minute. Lower, because this one guards passwords.
RATE_LIMIT_LOGIN_PER_MINUTE = int(os.environ.get("RATE_LIMIT_LOGIN_PER_MINUTE", "10") or 10)

# Whether X-Forwarded-For may be believed when identifying a caller. Off by
# default: the header is client-controlled, so trusting it on a directly-exposed
# app lets anyone reset their own rate-limit counter by inventing one. Turn it on
# only when the app genuinely sits behind a proxy that overwrites it.
TRUST_PROXY_HEADERS = os.environ.get("TRUST_PROXY_HEADERS", "").strip() in ("1", "true", "True")

# --------------------------------------------------------------------------
# Daily extraction quota (circuit breaker)
#
# The per-minute rate limit stops a runaway script. It does NOT stop steady,
# reasonable-looking use from quietly exhausting a provider for the rest of the
# day -- and Gemini's free tier is 20 requests per DAY, on the only route that
# can read a scanned invoice. Twenty polite requests an hour apart never trip a
# per-minute limit and still leave the process unable to read a scan by lunch.
#
# So there is a second, slower guard: a daily budget per provider. When it is
# spent, extraction takes its existing safe fallback WITHOUT calling the
# provider -- text drops to regex, scans go to route "none" and therefore to a
# human. No new decision semantics; the same paths a provider outage takes.
#
# Defaults leave real headroom: the vision budget matches the free tier exactly
# so the breaker trips at the same moment the provider would start refusing,
# and the text budget is generous because Groq is not the scarce one.
# --------------------------------------------------------------------------
DAILY_QUOTA_ENABLED = os.environ.get("DAILY_QUOTA_ENABLED", "1").strip() not in ("0", "false", "False", "")
DAILY_QUOTA_VISION = int(os.environ.get("DAILY_QUOTA_VISION", "20") or 20)
DAILY_QUOTA_TEXT = int(os.environ.get("DAILY_QUOTA_TEXT", "500") or 500)

# --------------------------------------------------------------------------
# Document storage (Phase C)
#
# The uploaded PDF itself, kept after processing so a run can still be opened
# and its source document viewed or downloaded later -- the database only ever
# holds METADATA and an opaque storage key, never the PDF bytes (see
# backend/documents.py, backend/storage.py's `documents` table).
#
# Backend selection is a config switch, not a code fork: "local" writes files
# under DOCUMENT_STORAGE_DIR (the default, needs nothing installed or
# configured), "s3" writes to an S3-compatible bucket for a real deployment.
# Nothing outside documents.py knows which one is active.
# --------------------------------------------------------------------------
DOCUMENT_STORE_BACKEND_ENV = "DOCUMENT_STORE_BACKEND"
DOCUMENT_STORAGE_DIR = os.environ.get(
    "DOCUMENT_STORAGE_DIR", os.path.join(ROOT, "data", "documents"))

DOCUMENT_S3_BUCKET_ENV = "DOCUMENT_S3_BUCKET"
DOCUMENT_S3_PREFIX_ENV = "DOCUMENT_S3_PREFIX"
DOCUMENT_S3_REGION_ENV = "DOCUMENT_S3_REGION"
DOCUMENT_S3_ENDPOINT_ENV = "DOCUMENT_S3_ENDPOINT_URL"  # for S3-compatible, non-AWS hosts

# The only source this process can currently produce a document from is a
# browser upload. EMAIL is recognised here so the schema and the storage
# abstraction do not need to change when ingestion (Phase J) adds a second
# producer -- nothing in this phase writes a document with that source yet.
DOCUMENT_SOURCES = ("MANUAL_UPLOAD", "EMAIL")


def document_store_backend() -> str:
    """'local' or 's3'. Read at call time, like every other env-backed
    setting here, so a value set in .env after import is still honoured."""
    backend = os.environ.get(DOCUMENT_STORE_BACKEND_ENV, "local").strip().lower()
    return backend if backend in ("local", "s3") else "local"


def document_s3_bucket() -> str:
    return os.environ.get(DOCUMENT_S3_BUCKET_ENV, "").strip()


def document_s3_prefix() -> str:
    return os.environ.get(DOCUMENT_S3_PREFIX_ENV, "").strip()


def document_s3_region() -> str:
    return os.environ.get(DOCUMENT_S3_REGION_ENV, "").strip() or None


def document_s3_endpoint_url() -> str:
    return os.environ.get(DOCUMENT_S3_ENDPOINT_ENV, "").strip() or None


# --------------------------------------------------------------------------
# Review claims (Phase D)
#
# How long an employee's claim on a NEEDS_REVIEW invoice holds before it is
# eligible to be taken over. A lease, not a permanent lock -- a closed browser
# tab or a lost connection must not block an invoice forever, and there is no
# background sweep job; the next claim attempt after this window simply finds
# the old claim expired and takes over (storage.claim_review). Read at call
# time, like every other env-backed setting in this file.
# --------------------------------------------------------------------------
REVIEW_CLAIM_LEASE_MINUTES_ENV = "REVIEW_CLAIM_LEASE_MINUTES"


def review_claim_lease_minutes() -> int:
    return int(os.environ.get(REVIEW_CLAIM_LEASE_MINUTES_ENV, "15") or 15)


# --------------------------------------------------------------------------
# Email security & trusted-source verification (Phase F)
#
# Everything here answers one question: how much of an incoming message's
# claimed origin can this process actually PROVE. Nothing here decides whether
# an invoice is legitimate -- that stays with rules.decide(), exactly as it is
# for a manually uploaded PDF. An email authentication result is a security
# signal about the ENVELOPE, never a verdict about the CONTENT.
#
# WHY AN AUTHSERV-ID ALLOWLIST IS THE CENTRE OF THIS
#
# `Authentication-Results` (RFC 8601) is an ordinary header. Anyone can put
# one in a message they send, saying anything they like -- "dmarc=pass" costs
# a spoofer nothing. The header is only worth reading when it was stamped by a
# receiving boundary WE control and can name. That is what this list is: the
# authserv-ids whose verdicts this process is willing to believe. Empty (the
# default) means believe none of them, which makes every message read as
# UNVERIFIED rather than trusted -- the safe direction, and the reason there is
# no "trust whatever is in the header" fallback.
# --------------------------------------------------------------------------
EMAIL_TRUSTED_AUTHSERV_IDS_ENV = "EMAIL_TRUSTED_AUTHSERV_IDS"
EMAIL_DNS_RESOLVER_ENV = "EMAIL_DNS_RESOLVER"
EMAIL_SIGNATURE_VERIFIER_ENV = "EMAIL_SIGNATURE_VERIFIER"
EMAIL_MAX_MESSAGE_BYTES_ENV = "EMAIL_MAX_MESSAGE_BYTES"

# A submitted .eml carries the invoice PDF inline, base64-encoded, so it is
# necessarily larger than the PDF alone -- base64 costs ~33%, plus headers and
# any other parts. Sized off the PDF cap rather than picked independently, so
# raising one does not silently leave the other as the real limit.
EMAIL_MAX_MESSAGE_BYTES_DEFAULT = MAX_UPLOAD_BYTES * 2

# The seed list of senders this business actually expects invoices FROM.
# Reloaded from JSON on every startup, exactly like purchase_orders.json and
# approved_vendors.json -- it is reference data the business owns, not
# application state.
TRUSTED_SENDER_SEED = os.path.join(ROOT, "data", "trusted_email_senders.json")

# Classifications email_security.classify() can produce. Deliberately FOUR,
# not three: "we proved it failed" and "we could not check" are different
# facts about a message and must never collapse into one another.
EMAIL_CLASSIFICATIONS = ("VERIFIED", "SUSPICIOUS", "FAILED", "UNVERIFIED")

# What the message is allowed to do next. Separate from the classification for
# the same reason `runs.status` is separate from `runs.automated_decision`:
# the classification is an immutable finding, the status moves when a human
# rules on it.
EMAIL_STATUSES = ("ADMITTED", "QUARANTINED", "RELEASED", "DISCARDED")


def email_trusted_authserv_ids() -> tuple:
    """The authentication-results boundaries this process believes, lowercased.

    Empty by default and empty is meaningful: it is not "misconfigured", it is
    "this deployment has no trusted boundary yet", and the classifier reports
    UNVERIFIED accordingly instead of inventing trust.
    """
    raw = os.environ.get(EMAIL_TRUSTED_AUTHSERV_IDS_ENV, "")
    return tuple(x.strip().lower() for x in raw.split(",") if x.strip())


def email_dns_resolver() -> str:
    """'none' (default) or 'dnspython'.

    'none' is not a degraded mode to apologise for -- it is the only setting
    that keeps verification reproducible offline, and it makes DKIM report
    `unavailable` (never `fail`) because no public key could be fetched.
    """
    choice = os.environ.get(EMAIL_DNS_RESOLVER_ENV, "none").strip().lower()
    return choice if choice in ("none", "dnspython") else "none"


def email_signature_verifier() -> str:
    """Which user-level (S/MIME or PGP) signature verifier to use.

    Only 'none' is implemented. It DETECTS a signature and reports its status
    as unavailable; it cannot and does not report a pass. See
    backend/email_signature.py for why a real one needs a trust anchor this
    deployment does not have.
    """
    return os.environ.get(EMAIL_SIGNATURE_VERIFIER_ENV, "none").strip().lower() or "none"


def email_max_message_bytes() -> int:
    try:
        return int(os.environ.get(EMAIL_MAX_MESSAGE_BYTES_ENV, "")
                   or EMAIL_MAX_MESSAGE_BYTES_DEFAULT)
    except ValueError:
        return EMAIL_MAX_MESSAGE_BYTES_DEFAULT


def load_dotenv():
    """Minimal .env loader (KEY=VALUE per line). Real environment wins."""
    if not os.path.isfile(ENV_PATH):
        return
    try:
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception:
        pass


def api_key() -> str:
    """The raw key, or "" when unset. Never logged and never sent to the browser."""
    return os.environ.get(API_KEY_ENV, "").strip()


def has_api_key() -> bool:
    """True when the Gemini key is set. Gemini is the VISION route, so this is
    also the answer to "can this process read a scanned invoice at all?"."""
    return bool(api_key())


def groq_api_key() -> str:
    """The raw Groq key, or "" when unset. Never logged, never sent to the browser."""
    return os.environ.get(GROQ_API_KEY_ENV, "").strip()


def has_groq_key() -> bool:
    """True when the Groq key is set -- i.e. the LLM text route is available."""
    return bool(groq_api_key())


def groq_model() -> str:
    """The Groq model to extract with.

    Read from the environment at call time rather than at import, because
    load_dotenv() runs after this module is imported -- a module-level constant
    would silently ignore a GROQ_MODEL set in .env.
    """
    return os.environ.get(GROQ_MODEL_ENV, "").strip() or GROQ_MODEL_DEFAULT


def extraction_mode() -> str:
    return "llm" if (has_groq_key() or has_api_key()) else "regex"


def status() -> dict:
    return {
        "extraction_mode": extraction_mode(),
        "text_model": groq_model() if has_groq_key() else None,
        "vision_model": EXTRACTION_MODEL if has_api_key() else None,
        "text_llm_available": has_groq_key(),
        "vision_available": has_api_key(),
        "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        "env_file_present": os.path.isfile(ENV_PATH),
    }
