"""The demo-reset endpoint: who may call it, and exactly what it removes.

The sample invoices are history-dependent by design, so a second pass through
them turns the happy path into a duplicate of itself. Clearing run history is
what makes them repeatable — but it is a destructive endpoint, so the tests that
matter are the ones proving it is locked down and narrow.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from fastapi.testclient import TestClient   # noqa: E402

import main       # noqa: E402
import storage    # noqa: E402
from conftest import auth_headers   # noqa: E402


def _seed_run(filename="seed.pdf", status="APPROVED", invoice_number="INV-SEED"):
    """Insert a run directly, so these tests do not depend on the pipeline."""
    return storage.save_run(
        filename,
        status,
        {"vendor_name": "Acme Office Supplies", "invoice_number": invoice_number,
         "total": 100.0, "currency": "USD"},
        {"po_number": "PO-1001"},
        [],
        [],
    )


@pytest.fixture
def client():
    with TestClient(main.app, headers=auth_headers("admin")) as c:
        yield c


# --------------------------------------------------------------------------
# authorisation — the important half
# --------------------------------------------------------------------------

def test_anonymous_callers_are_refused():
    with TestClient(main.app) as c:
        assert c.post("/api/admin/reset-demo").status_code == 401


@pytest.mark.parametrize("role", ["viewer", "analyst", "reviewer"])
def test_non_admin_roles_are_refused(role):
    """Reviewing an invoice and wiping the ledger's history are different
    authorities. Only invoice:admin carries the second."""
    with TestClient(main.app, headers=auth_headers(role)) as c:
        res = c.post("/api/admin/reset-demo")
        assert res.status_code == 403, f"{role} must not be able to reset"


def test_refusal_leaves_history_intact():
    with TestClient(main.app, headers=auth_headers("analyst")) as c:
        _seed_run()
        before = len(storage.list_runs())
        assert c.post("/api/admin/reset-demo").status_code == 403
        assert len(storage.list_runs()) == before


# --------------------------------------------------------------------------
# behaviour
# --------------------------------------------------------------------------

def test_admin_can_clear_run_history(client):
    _seed_run()
    _seed_run("second.pdf", invoice_number="INV-SEED-2")
    assert len(storage.list_runs()) >= 2

    res = client.post("/api/admin/reset-demo")
    assert res.status_code == 200

    body = res.json()
    assert body["ok"] is True
    assert body["deleted"] >= 2
    assert storage.list_runs() == []


def test_the_caller_is_recorded_from_the_token(client):
    """Who wiped the history is taken from the token, never from the body."""
    _seed_run()
    assert client.post("/api/admin/reset-demo").json()["by"] == "test-admin"


def test_reference_data_survives(client):
    """The whole safety argument: this removes only what re-running an invoice
    can rebuild. Purchase orders and vendors are seed data and must remain."""
    pos_before = storage.list_purchase_orders()
    vendors_before = storage.list_vendors()
    _seed_run()

    client.post("/api/admin/reset-demo")

    assert storage.list_purchase_orders() == pos_before
    assert storage.list_vendors() == vendors_before


def test_clearing_frees_po_budget(client):
    """Consumption is derived from APPROVED runs, so removing them must return
    the PO to its full authorised balance — no separate counter to correct."""
    po = storage.get_po("PO-1001")
    full = po["amount"]

    _seed_run(status="APPROVED")
    assert storage.remaining_for_po("PO-1001") < full

    client.post("/api/admin/reset-demo")
    assert storage.remaining_for_po("PO-1001") == full


def test_an_invoice_can_be_reprocessed_after_a_reset(client):
    """The point of the endpoint: the same invoice stops being a duplicate."""
    _seed_run(filename="01_happy_path_acme.pdf", invoice_number="INV-2201")
    assert storage.find_duplicate("Acme Office Supplies", "INV-2201", 100.0) is not None

    client.post("/api/admin/reset-demo")
    assert storage.find_duplicate("Acme Office Supplies", "INV-2201", 100.0) is None


def test_resetting_an_empty_history_is_harmless(client):
    """Idempotent: a second click must not error."""
    client.post("/api/admin/reset-demo")
    res = client.post("/api/admin/reset-demo")
    assert res.status_code == 200
    assert res.json()["deleted"] == 0
