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

# Google Gemini via Google AI Studio. The extraction layer is the only place
# a model is used at all -- every decision downstream of it is plain Python.
API_KEY_ENV = "GEMINI_API_KEY"
EXTRACTION_MODEL = "gemini-2.0-flash"


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
    return bool(api_key())


def extraction_mode() -> str:
    return "llm" if has_api_key() else "regex"


def status() -> dict:
    return {
        "extraction_mode": extraction_mode(),
        "model": EXTRACTION_MODEL if has_api_key() else None,
        "vision_available": has_api_key(),
        "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        "env_file_present": os.path.isfile(ENV_PATH),
    }
