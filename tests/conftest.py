"""Shared test helpers.

The API requires an OAuth 2.0 bearer token on every endpoint that touches
invoice data, so tests that drive it over HTTP have to authenticate like any
other client. Tokens are minted directly through `auth.create_access_token`
rather than by posting credentials, because most tests are about invoice
behaviour and should not also depend on what is in data/users.json. The login
flow itself is exercised for real in tests/test_api_security.py.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import auth   # noqa: E402


def token_for(role: str = "admin", username: str = None) -> str:
    """A signed access token carrying the scopes that role grants."""
    user = {"username": username or f"test-{role}", "roles": [role]}
    return auth.create_access_token(user)["access_token"]


def auth_headers(role: str = "admin", username: str = None) -> dict:
    return {"Authorization": "Bearer " + token_for(role, username)}
