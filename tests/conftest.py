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

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import auth   # noqa: E402
import config  # noqa: E402


def token_for(role: str = "admin", username: str = None) -> str:
    """A signed access token carrying the scopes that role grants."""
    user = {"username": username or f"test-{role}", "roles": [role]}
    return auth.create_access_token(user)["access_token"]


def auth_headers(role: str = "admin", username: str = None) -> dict:
    return {"Authorization": "Bearer " + token_for(role, username)}


@pytest.fixture(autouse=True)
def _isolate_document_storage(tmp_path, monkeypatch):
    """Point every test at a throwaway local document-storage directory.

    Any test that drives a real upload through the pipeline (most of
    test_api_security.py, test_human_review.py, etc., not just
    test_documents.py) now also writes the PDF to disk via
    backend/documents.py's LocalDocumentStore. Those tests isolate their
    database rows in a fresh, dropped-on-teardown Postgres schema
    (tests/pg_schema.py) but have no reason to know that document content
    needs the same treatment -- so it is done here, once, for every test in
    the suite, rather than requiring every current and future test file that
    happens to call upload() to remember it. Without this, running the suite
    against the default DOCUMENT_STORAGE_DIR would leave real PDFs behind
    under data/documents/ with no database row surviving to reference them.
    """
    monkeypatch.setattr(config, "DOCUMENT_STORAGE_DIR", str(tmp_path / "documents"))
