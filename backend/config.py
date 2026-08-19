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
