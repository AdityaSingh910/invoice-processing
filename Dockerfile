# Backend image for a split deployment: this container serves the API only.
#
# WHY A DOCKERFILE RATHER THAN AUTODETECTION. A buildpack would install
# requirements.txt and guess a start command. Two things here are not
# guessable: the app's package root is `backend/` (uvicorn needs --app-dir, and
# the modules import each other as top-level names, so the directory has to be
# ON the path rather than treated as a package), and a production deployment
# needs boto3, which requirements.txt deliberately leaves commented out so a
# local install never pays for a dependency it does not use.
#
# WHAT IS NOT IN THIS IMAGE, ON PURPOSE:
#   * frontend-next/  -- the UI is built and served by the static host. With no
#     frontend-next/out/ present, main.py serves the API alone (see
#     SERVE_FRONTEND there). Leaving it out also means no Node in this image.
#   * .env            -- excluded by .dockerignore. Configuration arrives as
#     real environment variables, which config.load_dotenv() already lets win.
#   * tests/, venv/, sample PDFs' build tooling -- nothing needed to serve.
FROM python:3.12-slim

# Fail fast and log immediately: an unflushed traceback in a container that is
# about to be restarted is a traceback nobody reads.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so an application edit does not re-resolve the whole
# dependency tree on every deploy.
COPY requirements.txt ./
# boto3 is the S3 document-store backend (backend/documents.py). It is
# commented out in requirements.txt because a local install defaults to the
# filesystem store and should not carry the dependency; a container has an
# EPHEMERAL filesystem, so object storage is the only durable option here and
# the package is always installed. Importing it is still lazy -- an image built
# with it and run with DOCUMENT_STORE_BACKEND=local never loads it.
RUN pip install --upgrade pip \
 && pip install -r requirements.txt \
 && pip install "boto3==1.35.0"

# The application, plus the seed/reference data init_db() reloads on every
# startup (purchase orders, vendors, trusted senders) and the locale catalogues
# i18n.py reads. data/documents/ is runtime state and is not copied.
COPY backend/ ./backend/
COPY data/ ./data/
COPY sample_invoices/ ./sample_invoices/
COPY scripts/ ./scripts/

# Documented rather than published: the platform routes to whatever $PORT says,
# and this is only the fallback for `docker run` with nothing set.
ENV PORT=8000
EXPOSE 8000

# The entrypoint writes the user store from AUTH_USERS_JSON and then execs
# uvicorn with ONE worker. One worker is deliberate: two things here are
# per-process -- the sliding-window rate limiters (Phase K) and the
# email-ingestion poller -- so every extra worker multiplies the effective rate
# limit and adds another poller. The poller stays correct either way
# (idempotency is a UNIQUE constraint in the database, not coordination between
# pollers); the rate limits do not. Raise it only alongside a shared limiter
# store. See scripts/start-backend.sh for the rest.
COPY scripts/start-backend.sh /usr/local/bin/start-backend.sh
RUN chmod +x /usr/local/bin/start-backend.sh
CMD ["/usr/local/bin/start-backend.sh"]
