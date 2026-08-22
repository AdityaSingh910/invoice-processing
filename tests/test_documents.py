"""Phase C: persistent invoice PDF storage.

THE CLAIM UNDER TEST

An uploaded invoice PDF survives the run that processed it -- it can be
viewed and downloaded afterwards -- without the database ever holding the
PDF bytes themselves (backend/documents.py's DocumentStore abstraction owns
the content; backend/storage.py's `documents` table owns only metadata), and
without an unauthenticated or unauthorized caller ever reaching either the
metadata or the bytes.

Driven over HTTP wherever the claim is about authorization, exactly like
test_api_security.py -- calling storage/documents functions directly proves
nothing about whether the endpoint in front of them is guarded. The
DocumentStore path-safety claims are unit-tested directly, since HTTP has no
way to submit a malformed storage key in the first place (it is always
server-generated) -- the direct tests are what prove that even a corrupted
database row could not be used to escape the storage directory.
"""
import hashlib
import io
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
SAMPLES = os.path.join(ROOT, "sample_invoices")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
TESTS = os.path.dirname(os.path.abspath(__file__))
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)

import config      # noqa: E402
import documents   # noqa: E402
import main        # noqa: E402
import ratelimit   # noqa: E402
import storage     # noqa: E402
import pg_schema   # noqa: E402
from conftest import auth_headers, token_for   # noqa: E402

HAPPY_PDF = os.path.join(SAMPLES, "01_happy_path_acme.pdf")


def pdf_bytes():
    with open(HAPPY_PDF, "rb") as f:
        return f.read()


@pytest.fixture
def db(monkeypatch):
    # Document storage isolation (DOCUMENT_STORAGE_DIR -> a per-test tmp_path)
    # is handled by the autouse `_isolate_document_storage` fixture in
    # conftest.py, for every test file, not just this one.
    schema = pg_schema.fresh_schema(monkeypatch)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    ratelimit.limiter.reset()
    yield schema
    pg_schema.drop_schema(schema)


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        yield c


def upload(client, headers=None, data=None, name="invoice.pdf", ctype="application/pdf"):
    body = pdf_bytes() if data is None else data
    return client.post("/api/runs/stream",
                       files={"file": (name, io.BytesIO(body), ctype)},
                       headers=headers or {})


def process_happy_path(client, headers=None):
    """Drive the real happy-path sample through the pipeline and return its
    run_id. No provider keys are set (see the `db` fixture), so this runs the
    deterministic regex route -- the point here is document persistence, not
    extraction quality."""
    r = upload(client, headers or auth_headers("analyst", username="alice"),
              name="01_happy_path_acme.pdf")
    assert r.status_code == 200

    import json
    final = None
    for line in r.text.splitlines():
        if line.startswith("data: "):
            evt = json.loads(line[6:])
            if evt.get("type") == "final":
                final = evt["result"]
    assert final is not None
    return final["run_id"], final["status"]


# --------------------------------------------------------------------------
# 1. the document survives the run, with correct metadata
# --------------------------------------------------------------------------

def test_document_is_persisted_and_readable_after_processing(client):
    run_id, run_status = process_happy_path(client)
    assert run_status in ("APPROVED", "NEEDS_REVIEW", "REJECTED")

    r = client.get(f"/api/runs/{run_id}/document", headers=auth_headers("viewer"))
    assert r.status_code == 200
    meta = r.json()
    assert meta["run_id"] == run_id
    assert meta["original_filename"] == "01_happy_path_acme.pdf"
    assert meta["mime_type"] == "application/pdf"
    assert meta["size_bytes"] == len(pdf_bytes())
    assert meta["sha256"] == hashlib.sha256(pdf_bytes()).hexdigest()
    assert meta["uploaded_by"] == "alice"
    assert meta["source"] == "MANUAL_UPLOAD"


def test_document_metadata_never_exposes_storage_internals(client):
    run_id, _ = process_happy_path(client)
    meta = client.get(f"/api/runs/{run_id}/document", headers=auth_headers("viewer")).json()
    assert "storage_key" not in meta
    assert "storage_backend" not in meta
    assert "path" not in meta


def test_downloaded_bytes_match_the_original_upload_exactly(client):
    run_id, _ = process_happy_path(client)
    r = client.get(f"/api/runs/{run_id}/document/download", headers=auth_headers("viewer"))
    assert r.status_code == 200
    assert r.content == pdf_bytes()
    assert r.headers["content-type"].startswith("application/pdf")


def test_download_defaults_to_attachment_disposition(client):
    run_id, _ = process_happy_path(client)
    r = client.get(f"/api/runs/{run_id}/document/download", headers=auth_headers("viewer"))
    assert "attachment" in r.headers["content-disposition"]
    assert "01_happy_path_acme.pdf" in r.headers["content-disposition"]


def test_inline_flag_switches_to_inline_disposition(client):
    run_id, _ = process_happy_path(client)
    r = client.get(f"/api/runs/{run_id}/document/download?inline=true",
                   headers=auth_headers("viewer"))
    assert "inline" in r.headers["content-disposition"]


def test_pipeline_still_reaches_a_verdict_with_document_persistence_wired_in(client):
    """A regression guard for the actual thing Phase C must not break: the
    decision engine's output is unaffected by whether the PDF was also
    stored."""
    run_id, run_status = process_happy_path(client)
    run = client.get(f"/api/runs/{run_id}", headers=auth_headers("viewer")).json()
    assert run["status"] == run_status
    assert run["extracted"]["vendor_name"]
    assert run["po_match"] is not None


# --------------------------------------------------------------------------
# 2. authorization is checked before any document access
# --------------------------------------------------------------------------

def test_unauthenticated_caller_cannot_read_metadata_or_download(client):
    run_id, _ = process_happy_path(client)
    assert client.get(f"/api/runs/{run_id}/document").status_code == 401
    assert client.get(f"/api/runs/{run_id}/document/download").status_code == 401


def test_a_token_with_no_read_scope_is_refused_before_the_document_is_touched(client, monkeypatch):
    """Every named role in this app happens to carry invoice:read, so a
    scope-less token is crafted directly -- exactly the pattern
    test_api_security.py uses to prove the 403 boundary itself, independent
    of which roles exist today."""
    import jwt
    import auth
    run_id, _ = process_happy_path(client)

    no_scope = jwt.encode(
        {"sub": "nobody", "scope": "", "iss": config.AUTH_ISSUER, "exp": 9999999999},
        auth.signing_secret(), algorithm="HS256")
    headers = {"Authorization": f"Bearer {no_scope}"}

    meta = client.get(f"/api/runs/{run_id}/document", headers=headers)
    assert meta.status_code == 403

    dl = client.get(f"/api/runs/{run_id}/document/download", headers=headers)
    assert dl.status_code == 403


def test_a_forged_token_cannot_reach_a_document(client):
    import jwt
    run_id, _ = process_happy_path(client)
    forged = jwt.encode(
        {"sub": "attacker", "scope": "invoice:read", "iss": config.AUTH_ISSUER,
         "exp": 9999999999},
        "not-the-real-secret", algorithm="HS256")
    r = client.get(f"/api/runs/{run_id}/document/download",
                   headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401


def test_any_role_with_invoice_read_can_view_and_download(client):
    run_id, _ = process_happy_path(client)
    for role in ("viewer", "analyst", "reviewer", "admin"):
        assert client.get(f"/api/runs/{run_id}/document",
                          headers=auth_headers(role)).status_code == 200
        assert client.get(f"/api/runs/{run_id}/document/download",
                          headers=auth_headers(role)).status_code == 200


# --------------------------------------------------------------------------
# 3. not-found paths say nothing about internal state
# --------------------------------------------------------------------------

def test_document_for_an_unknown_run_is_404(client):
    r = client.get("/api/runs/999999/document", headers=auth_headers("viewer"))
    assert r.status_code == 404


def test_a_run_with_no_stored_document_is_404_not_500(client):
    # Inserted directly, bypassing the pipeline -- exactly the shape a
    # pre-Phase-C run in an existing database would have: a real run, no
    # document row at all.
    run_id = storage.save_run(
        "legacy.pdf", "APPROVED",
        {"vendor_name": "Acme Office Supplies", "invoice_number": "INV-LEGACY",
         "total": 100.0, "currency": "USD"},
        {"po_number": "PO-1001"}, [], [])
    r = client.get(f"/api/runs/{run_id}/document", headers=auth_headers("viewer"))
    assert r.status_code == 404
    r2 = client.get(f"/api/runs/{run_id}/document/download", headers=auth_headers("viewer"))
    assert r2.status_code == 404


def test_missing_backing_file_is_404_and_leaks_no_path(client):
    """The metadata row survived; the bytes did not (disk cleared by hand,
    object expired). Must degrade to a clean 404, never a 500, and the error
    body must not name a filesystem path or bucket."""
    run_id, _ = process_happy_path(client)
    doc = storage.get_document_for_run(run_id)
    documents.get_store().delete(doc["storage_key"])

    r = client.get(f"/api/runs/{run_id}/document/download", headers=auth_headers("viewer"))
    assert r.status_code == 404
    body = r.text.lower()
    assert str(config.DOCUMENT_STORAGE_DIR).lower() not in body
    assert ":\\" not in body and "/users/" not in body


# --------------------------------------------------------------------------
# 4. upload validation is unchanged, and a rejected upload persists nothing
# --------------------------------------------------------------------------

def test_invalid_file_type_is_rejected_and_nothing_is_persisted(client):
    r = upload(client, auth_headers("analyst"), data=b"not a pdf at all", name="x.pdf")
    assert r.status_code == 415
    assert storage.list_runs() == []


def test_empty_upload_is_rejected_and_nothing_is_persisted(client):
    r = upload(client, auth_headers("analyst"), data=b"")
    assert r.status_code == 400
    assert storage.list_runs() == []


def test_oversized_upload_is_rejected_before_any_run_or_document_exists(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 1000)
    body = b"%PDF-1.4\n" + (b"0" * 5000)
    r = upload(client, auth_headers("analyst"), data=body)
    assert r.status_code == 413
    assert storage.list_runs() == []


def test_a_document_that_fails_to_persist_does_not_fail_the_run(client, monkeypatch):
    """Storage-layer trouble (a full disk, an unreachable bucket) must not
    turn a correctly-decided run into a failed pipeline -- the same
    fail-safe posture as the daily quota breaker."""
    def explode(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(documents, "get_store", explode)

    run_id, run_status = process_happy_path(client)
    assert run_status in ("APPROVED", "NEEDS_REVIEW", "REJECTED")
    run = client.get(f"/api/runs/{run_id}", headers=auth_headers("viewer"))
    assert run.status_code == 200

    meta = client.get(f"/api/runs/{run_id}/document", headers=auth_headers("viewer"))
    assert meta.status_code == 404


# --------------------------------------------------------------------------
# 5. path traversal / storage-key validation -- unit level
#
# HTTP cannot submit a malformed storage key (it is always server-generated
# via new_storage_key()); these tests prove the abstraction itself refuses
# one anyway, so a corrupted or hand-edited database row could never be used
# to read or write outside the storage directory.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad_key", [
    "../../../etc/passwd",
    "..\\..\\windows\\system32\\config",
    "../secret.pdf",
    "sub/dir/x.pdf",
    "no-extension",
    "has spaces.pdf",
    "UPPERCASE.pdf",
    "",
    "a" * 32 + ".exe",
    "/etc/passwd",
])
def test_local_store_refuses_every_non_conforming_key(tmp_path, bad_key):
    store = documents.LocalDocumentStore(root=str(tmp_path))
    with pytest.raises(ValueError):
        store.save(bad_key, b"whatever")
    with pytest.raises(ValueError):
        store.read(bad_key)
    with pytest.raises(ValueError):
        store.delete(bad_key)
    assert store.exists(bad_key) is False


def test_local_store_never_writes_outside_its_root(tmp_path):
    store = documents.LocalDocumentStore(root=str(tmp_path))
    with pytest.raises(ValueError):
        store.save("../outside.pdf", b"escape attempt")
    # Nothing was created one level up from the store root.
    assert not os.path.isfile(os.path.join(str(tmp_path), "..", "outside.pdf"))


def test_new_storage_key_always_matches_the_conforming_shape():
    for _ in range(20):
        key = documents.new_storage_key()
        assert documents._KEY_RE.match(key), key


def test_local_store_round_trips_a_real_key(tmp_path):
    store = documents.LocalDocumentStore(root=str(tmp_path))
    key = documents.new_storage_key()
    data = pdf_bytes()
    store.save(key, data)
    assert store.exists(key) is True
    assert store.read(key) == data
    store.delete(key)
    assert store.exists(key) is False


def test_local_store_refuses_content_over_the_upload_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 10)
    store = documents.LocalDocumentStore(root=str(tmp_path))
    with pytest.raises(ValueError):
        store.save(documents.new_storage_key(), b"0" * 100)


# --------------------------------------------------------------------------
# 5a. the database-backed store
#
# THE CLAIM UNDER TEST: an uploaded PDF stored with
# DOCUMENT_STORE_BACKEND=postgres survives anything that wipes the container
# filesystem, because it never touched the filesystem -- and it behaves
# identically to the local store at every point the rest of the application
# can observe, so choosing it is a configuration change and nothing more.
#
# That last part is why these tests mirror the local-store tests above case
# for case rather than inventing their own shapes: the interface is the
# contract, and a backend that honoured it "mostly" would fail in production
# on whichever call it got wrong.
# --------------------------------------------------------------------------

@pytest.fixture
def pg_store(db, monkeypatch):
    monkeypatch.setenv("DOCUMENT_STORE_BACKEND", "postgres")
    return documents.PostgresDocumentStore()


def test_the_backend_switch_selects_the_postgres_store(db, monkeypatch):
    monkeypatch.setenv("DOCUMENT_STORE_BACKEND", "postgres")
    assert config.document_store_backend() == "postgres"
    assert isinstance(documents.get_store(), documents.PostgresDocumentStore)


def test_an_unrecognised_backend_still_falls_back_to_local(monkeypatch):
    monkeypatch.setenv("DOCUMENT_STORE_BACKEND", "gcs")
    assert config.document_store_backend() == "local"
    assert isinstance(documents.get_store(), documents.LocalDocumentStore)


def test_postgres_store_round_trips_a_real_key(pg_store):
    key = documents.new_storage_key()
    data = pdf_bytes()
    pg_store.save(key, data)
    assert pg_store.exists(key) is True
    assert pg_store.read(key) == data
    pg_store.delete(key)
    assert pg_store.exists(key) is False


def test_postgres_store_preserves_bytes_exactly(pg_store):
    """Not a formality: bytea round trips through psycopg2's own encoding, and
    a PDF is full of NULs and high bytes that a text path would mangle."""
    key = documents.new_storage_key()
    data = bytes(range(256)) * 40 + bytes([0, 13, 10, 39, 34, 92]) * 5
    pg_store.save(key, data)
    assert pg_store.read(key) == data


@pytest.mark.parametrize("bad_key", [
    "../../../etc/passwd",
    "..\\..\\windows\\system32",
    "sub/dir/x.pdf",
    "no-extension",
    "",
    "a" * 32 + ".exe",
])
def test_postgres_store_refuses_every_non_conforming_key(pg_store, bad_key):
    with pytest.raises(ValueError):
        pg_store.save(bad_key, b"whatever")
    with pytest.raises(ValueError):
        pg_store.read(bad_key)
    with pytest.raises(ValueError):
        pg_store.delete(bad_key)
    assert pg_store.exists(bad_key) is False


def test_postgres_store_refuses_content_over_the_upload_limit(pg_store, monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 10)
    with pytest.raises(ValueError):
        pg_store.save(documents.new_storage_key(), b"0" * 100)


def test_postgres_store_reads_a_missing_key_as_not_found(pg_store):
    """The same exception the local store raises for a missing file, so the
    endpoint's existing 404 handling applies unchanged."""
    with pytest.raises(FileNotFoundError):
        pg_store.read(documents.new_storage_key())


def test_saving_the_same_key_twice_is_a_retry_not_a_crash(pg_store):
    key = documents.new_storage_key()
    pg_store.save(key, b"%PDF-first")
    pg_store.save(key, b"%PDF-second")
    assert pg_store.read(key) == b"%PDF-second"


def test_deleting_a_key_that_is_not_there_is_not_an_error(pg_store):
    pg_store.delete(documents.new_storage_key())


def test_a_document_stored_in_postgres_survives_the_filesystem_being_wiped(
        client, monkeypatch, tmp_path):
    """THE POINT OF THE WHOLE BACKEND, stated as a test.

    A container redeploy replaces the filesystem and keeps the database. This
    reproduces exactly that: process an invoice with the postgres backend
    selected, then repoint DOCUMENT_STORAGE_DIR at an empty directory -- the
    strongest available stand-in for "the disk this ran on is gone" -- and
    require the download to still return the original bytes.

    Run against the local backend, this same sequence 404s.
    """
    monkeypatch.setenv("DOCUMENT_STORE_BACKEND", "postgres")
    run_id, _ = process_happy_path(client)

    meta = client.get(f"/api/runs/{run_id}/document", headers=auth_headers("viewer"))
    assert meta.status_code == 200

    gone = tmp_path / "after-redeploy"
    gone.mkdir()
    monkeypatch.setattr(config, "DOCUMENT_STORAGE_DIR", str(gone))

    r = client.get(f"/api/runs/{run_id}/document/download", headers=auth_headers("viewer"))
    assert r.status_code == 200
    assert r.content == pdf_bytes()
    assert not any(gone.iterdir()), "the postgres store must not touch the filesystem"


def test_save_document_rejects_an_unrecognised_source(db):
    run_id = storage.save_run(
        "x.pdf", "APPROVED",
        {"vendor_name": "Acme Office Supplies", "invoice_number": "INV-X", "total": 1.0},
        {"po_number": "PO-1001"}, [], [])
    with pytest.raises(ValueError):
        storage.save_document(
            run_id=run_id, original_filename="x.pdf", mime_type="application/pdf",
            size_bytes=1, sha256_hex="a" * 64, uploaded_by="tester",
            source="NOT_A_REAL_SOURCE", storage_backend="local",
            storage_key=documents.new_storage_key())


# --------------------------------------------------------------------------
# 6. reset-demo clears documents along with the runs they belong to
# --------------------------------------------------------------------------

def test_reset_demo_clears_documents_with_their_runs(client):
    run_id, _ = process_happy_path(client)
    doc = storage.get_document_for_run(run_id)
    assert doc is not None
    store = documents.get_store()
    assert store.exists(doc["storage_key"])

    r = client.post("/api/admin/reset-demo", headers=auth_headers("admin"))
    assert r.status_code == 200

    assert storage.get_document_for_run(run_id) is None
    assert not store.exists(doc["storage_key"])
