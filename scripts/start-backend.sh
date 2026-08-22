#!/bin/sh
# Container entrypoint: materialise the user store, then hand over to uvicorn.
#
# Two lines of work, and both are deployment plumbing rather than application
# behaviour -- nothing here decides anything the app would otherwise decide.
#
#   1. AUTH_USERS_JSON -> a file, because auth.load_users() reads a PATH and a
#      container has nowhere durable to keep one. A no-op when the variable is
#      unset, which is what leaves local and CI runs on the demo store.
#
#   2. exec uvicorn, so uvicorn becomes PID 1 and receives SIGTERM directly.
#      Without `exec` the shell holds PID 1, swallows the signal, and the
#      platform eventually kills the container instead -- which skips FastAPI's
#      shutdown handler and the email poller it stops.
set -e

: "${AUTH_USERS_FILE:=/tmp/users.json}"
export AUTH_USERS_FILE

python scripts/make_user_store.py render

# ${PORT} is assigned by the platform. The fallback is only for `docker run`
# with nothing set.
exec uvicorn main:app \
    --app-dir backend \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers 1 \
    --proxy-headers \
    --forwarded-allow-ips='*'
