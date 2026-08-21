"""Phase J: the client portal.

THE CLAIM UNDER TEST

An external vendor can sign in, see their own invoices, purchase orders and
documents, and submit an invoice -- and cannot, by any route this application
exposes, reach another client's records or any internal function.

Driven over real HTTP through the real app wherever the claim is about
authorization, exactly as test_documents.py and test_security_hardening.py
are: calling portal.py's functions directly proves nothing about whether the
endpoint in front of them is guarded, and the guard is the entire feature.

Two things about the fixtures are load-bearing and worth reading before adding
a test here:

  * CLIENT ACCOUNTS COME FROM A REAL USER STORE ON DISK, pointed at by
    AUTH_USERS_FILE. The rest of this suite mints tokens directly through
    `auth.create_access_token` and never touches the store, which is right for
    tests about invoice behaviour -- but a client binding is READ FROM THE
    STORE ON EVERY REQUEST and deliberately does not live in the token, so a
    test that faked one would be testing nothing that exists.

  * A TOKEN IS MINTED WITH `token_for`, i.e. from roles alone, exactly as the
    real login endpoint mints one. So every "can this token reach that" test
    here is asking the same question the deployed system asks.
"""
import io
import json
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

import auth        # noqa: E402
import config      # noqa: E402
import main        # noqa: E402
import portal      # noqa: E402
import quota       # noqa: E402
import ratelimit   # noqa: E402
import storage     # noqa: E402
import pg_schema   # noqa: E402
from conftest import auth_headers, token_for   # noqa: E402

HAPPY_PDF = os.path.join(SAMPLES, "01_happy_path_acme.pdf")


def pdf_bytes(path=HAPPY_PDF):
    with open(path, "rb") as f:
        return f.read()


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

# Two clients, bound to two of the seeded approved vendors. ACME is the one
# most tests act as; GLOBEX exists so that "cannot see another client's data"
# is a statement about a real other client with real other data, rather than
# about an empty result nobody could tell from a bug.
ACME = {"username": "portal-acme", "roles": ["client"], "client_id": "C-ACME",
        "client_name": "Acme Office Supplies", "vendor_ids": ["V-001"]}
GLOBEX = {"username": "portal-globex", "roles": ["client"], "client_id": "C-GLOBEX",
          "client_name": "Globex Logistics", "vendor_ids": ["V-002"]}
READONLY = {"username": "portal-readonly", "roles": ["client_readonly"],
            "client_id": "C-ACME", "client_name": "Acme Office Supplies",
            "vendor_ids": ["V-001"]}


def write_users(path, records):
    """Write a user store to `path`. Does NOT touch the environment.

    Pointing AUTH_USERS_FILE at it is the caller's job, through
    `monkeypatch.setenv`, and that separation is deliberate. The first version
    of this helper set os.environ directly as a convenience -- which
    monkeypatch cannot undo, so after this file ran, every later test module
    in the same process had AUTH_USERS_FILE pointing at a deleted tmp
    directory. It broke two tests in test_production_safety.py and was
    invisible when either file ran alone.

    Tests that rewrite the store MID-test call this again with the same path;
    the env var set once at the start still points there, so the change is
    picked up on the next request without any further environment work. That
    is the property being tested in several places: the binding is read from
    the store on every request, not cached.
    """
    rows = [dict(r, password_hash=auth.hash_password("x")) for r in records]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f)
    return rows


@pytest.fixture
def db(tmp_path, monkeypatch):
    schema = pg_schema.fresh_schema(monkeypatch)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    ratelimit.limiter.reset()

    users = tmp_path / "users.json"
    write_users(users, [ACME, GLOBEX, READONLY])
    monkeypatch.setenv("AUTH_USERS_FILE", str(users))

    yield schema
    pg_schema.drop_schema(schema)


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        yield c


def headers(account=ACME):
    return {"Authorization": "Bearer " + token_for(account["roles"][0],
                                                   username=account["username"])}


def upload_internal(client, name="01_happy_path_acme.pdf", data=None):
    """Drive an invoice through the INTERNAL upload path, as an employee.

    This is how most of the fixture data in this file is created, and it is
    the case that matters most: an invoice that reached accounts payable some
    other way carries no client id at all, so the portal has to recognise it
    by vendor. If the portal only ever saw its own submissions, it would be
    useless to a supplier whose invoices arrive by email.
    """
    body = pdf_bytes() if data is None else data
    r = client.post("/api/runs/stream",
                    files={"file": (name, io.BytesIO(body), "application/pdf")},
                    headers=auth_headers("analyst", username="employee"))
    assert r.status_code == 200
    final = None
    for line in r.text.splitlines():
        if line.startswith("data: "):
            evt = json.loads(line[6:])
            if evt.get("type") == "final":
                final = evt["result"]
    assert final is not None
    return final


def make_run(vendor_name, status="NEEDS_REVIEW", invoice_number="INV-9001",
             total=100.0, client_id=None, audit=None, currency="USD",
             po_number=None):
    """A run written straight to the database, for the cases a real upload
    cannot produce on demand -- a specific vendor spelling, a specific status,
    a specific failing rule. Uses the same writer the pipeline uses."""
    extracted = {"vendor_name": vendor_name, "invoice_number": invoice_number,
                 "total": total, "currency": currency, "extraction_method": "regex"}
    po_match = {"po_number": po_number,
                "po_numbers": [po_number] if po_number else [],
                "allocations": []}
    run_id, final_status, _ = storage.save_run_checked(
        f"{invoice_number}.pdf", status, extracted, po_match, [], [],
        audit=audit, uploaded_by="employee", client_id=client_id)
    return run_id


def acme_ctx():
    return portal.context_for(auth.Principal({"sub": ACME["username"]}))


# ==========================================================================
# 1. authentication
# ==========================================================================

PORTAL_GETS = [
    "/api/portal/me",
    "/api/portal/invoices",
    "/api/portal/invoices/1",
    "/api/portal/invoices/1/document",
    "/api/portal/invoices/1/document/download",
    "/api/portal/purchase-orders",
]


@pytest.mark.parametrize("path", PORTAL_GETS)
def test_no_token_is_refused(client, path):
    assert client.get(path).status_code == 401


def test_submission_with_no_token_is_refused(client):
    r = client.post("/api/portal/invoices",
                    files={"file": ("a.pdf", io.BytesIO(pdf_bytes()), "application/pdf")})
    assert r.status_code == 401


@pytest.mark.parametrize("path", PORTAL_GETS)
def test_a_garbage_token_is_refused(client, path):
    r = client.get(path, headers={"Authorization": "Bearer not-a-token"})
    assert r.status_code == 401


def test_a_token_signed_with_another_key_is_refused(client, monkeypatch):
    import jwt
    import time
    forged = jwt.encode({"sub": ACME["username"], "roles": ["client"],
                         "scope": "portal:read portal:submit",
                         "iss": config.AUTH_ISSUER,
                         "iat": int(time.time()), "exp": int(time.time()) + 600},
                        "a-different-secret", algorithm="HS256")
    r = client.get("/api/portal/me", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401


def test_an_expired_token_is_refused(client, monkeypatch):
    monkeypatch.setattr(config, "AUTH_TOKEN_TTL_MINUTES", -1)
    tok = auth.create_access_token(dict(ACME))["access_token"]
    r = client.get("/api/portal/me", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 401


def test_a_client_can_sign_in_through_the_real_password_grant(client, tmp_path,
                                                              monkeypatch):
    """The portal is reached with an ordinary token from the ordinary token
    endpoint -- there is no separate client login, and no second issuer."""
    users = tmp_path / "u.json"
    rows = [dict(ACME, password_hash=auth.hash_password("s3cret"))]
    with open(users, "w", encoding="utf-8") as f:
        json.dump(rows, f)
    monkeypatch.setenv("AUTH_USERS_FILE", str(users))

    r = client.post("/api/auth/token",
                    data={"username": ACME["username"], "password": "s3cret"})
    assert r.status_code == 200
    body = r.json()
    assert "portal:read" in body["scope"]
    assert "invoice:read" not in body["scope"]

    me = client.get("/api/portal/me",
                    headers={"Authorization": "Bearer " + body["access_token"]})
    assert me.status_code == 200
    assert me.json()["client_id"] == "C-ACME"


# ==========================================================================
# 2. authorization -- the two directions of the scope boundary
# ==========================================================================

def test_a_client_role_carries_no_invoice_scope(client):
    """The structural property the whole phase rests on. If this ever becomes
    false, every internal endpoint opens to external callers at once."""
    for role in ("client", "client_readonly"):
        granted = set(auth.scopes_for_roles([role]))
        assert not any(s.startswith("invoice:") for s in granted), role


def test_no_internal_role_carries_a_portal_scope(client):
    for role in ("viewer", "analyst", "reviewer", "admin"):
        granted = set(auth.scopes_for_roles([role]))
        assert not any(s.startswith("portal:") for s in granted), role


@pytest.mark.parametrize("role", ["viewer", "analyst", "reviewer", "admin"])
@pytest.mark.parametrize("path", PORTAL_GETS)
def test_internal_roles_cannot_reach_the_portal(client, role, path):
    r = client.get(path, headers=auth_headers(role, username=f"staff-{role}"))
    assert r.status_code == 403


def test_admin_cannot_reach_the_portal_either(client):
    """Stated as its own test because it looks like an omission and is not.
    An administrator has no vendor binding, so there is nothing coherent for
    a per-client view to show them -- and everything it would show, they can
    already read in full through the internal API."""
    assert client.get("/api/portal/invoices",
                      headers=auth_headers("admin", username="root")).status_code == 403


def test_a_readonly_client_cannot_submit(client):
    r = client.post("/api/portal/invoices",
                    files={"file": ("a.pdf", io.BytesIO(pdf_bytes()), "application/pdf")},
                    headers=headers(READONLY))
    assert r.status_code == 403


def test_a_readonly_client_can_still_read(client):
    assert client.get("/api/portal/me", headers=headers(READONLY)).status_code == 200


# --------------------------------------------------------------------------
# The sweep. An attacker only needs the one endpoint somebody forgot, so this
# enumerates every route the application actually registers rather than a
# hand-written list that a later phase would silently outgrow.
# --------------------------------------------------------------------------

def _internal_api_routes():
    out = []
    for route in main.app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", None) or set()
        if not path.startswith("/api/") or path.startswith("/api/portal"):
            continue
        # Public by design: the liveness probe and the token endpoint.
        if path in ("/api/health", "/api/auth/token"):
            continue
        for method in sorted(methods):
            if method in ("GET", "POST"):
                out.append((method, path))
    return sorted(set(out))


@pytest.mark.parametrize("method,path", _internal_api_routes())
def test_a_client_token_reaches_no_internal_endpoint(client, method, path):
    """Every internal route, enumerated from the app itself, refused.

    `/api/auth/me` is the one internal route a client legitimately reaches:
    it reports the caller their own username and scopes and reads nothing
    about invoices at all. Everything else must refuse."""
    concrete = (path.replace("{run_id}", "1").replace("{email_id}", "1")
                    .replace("{name}", "x.pdf").replace("{stream}", "invoice")
                    .replace("{event_id}", "1").replace("{invoice_id}", "1"))
    call = client.get if method == "GET" else client.post
    kwargs = {"headers": headers(ACME)}
    if method == "POST":
        kwargs["json"] = {}
    r = call(concrete, **kwargs)

    if concrete == "/api/auth/me":
        assert r.status_code == 200
        assert r.json()["scopes"] == ["portal:read", "portal:submit"]
        return

    if concrete == "/api/email/oauth/gmail/callback":
        # Phase G2. This route CANNOT answer 401/403: Google redirects the
        # administrator's browser to it, and a top-level navigation carries no
        # Authorization header, so it does not authenticate at all -- it
        # ignores the token entirely and authorises on the single-use `state`
        # instead.
        #
        # So the property is asserted directly rather than through a status
        # code: a caller without a valid state is refused and NOTHING is
        # changed. Holding a client token buys exactly nothing here.
        #
        # Re-issued without following the redirect -- the shared call above
        # follows it to the app shell and reports that page's 200, which says
        # nothing about what this route did.
        redirect = client.get(concrete, headers=headers(ACME), follow_redirects=False)
        assert redirect.status_code == 303
        assert redirect.headers["location"] == "/?gmail=invalid_state"
        assert storage.get_oauth_connection("gmail") is None
        return

    assert r.status_code in (401, 403), f"{method} {concrete} -> {r.status_code}"


# ==========================================================================
# 3. a client sees their own data
# ==========================================================================

def test_a_client_sees_their_own_invoice(client):
    make_run("Acme Office Supplies", invoice_number="INV-ACME-1")
    r = client.get("/api/portal/invoices", headers=headers(ACME))
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["invoices"][0]["invoice_number"] == "INV-ACME-1"
    assert body["client"]["client_id"] == "C-ACME"
    assert body["client"]["vendors"] == ["Acme Office Supplies"]


def test_an_internally_uploaded_invoice_reaches_the_right_client(client):
    """The case that makes the portal worth having: an invoice an employee
    uploaded (or that arrived by email) carries no client id at all, and must
    still be recognised as the supplier's by vendor identity."""
    final = upload_internal(client)
    assert final["extracted"]["vendor_name"]

    r = client.get("/api/portal/invoices", headers=headers(ACME))
    ids = [i["invoice_id"] for i in r.json()["invoices"]]
    assert final["run_id"] in ids

    # And is not visible to the other client.
    other = client.get("/api/portal/invoices", headers=headers(GLOBEX))
    assert final["run_id"] not in [i["invoice_id"] for i in other.json()["invoices"]]


def test_vendor_name_spelling_variants_still_resolve(client):
    """Matched through `normalize_vendor_name`, the same comparison
    rules.vendor_check uses -- not on the raw string. A supplier whose name
    was read off the document as "ACME OFFICE SUPPLIES, INC" is the same
    supplier, and a portal that missed it would look like lost invoices."""
    make_run("acme  office   supplies", invoice_number="INV-SPELL-1")
    make_run("ACME OFFICE SUPPLIES", invoice_number="INV-SPELL-2")
    r = client.get("/api/portal/invoices", headers=headers(ACME))
    numbers = {i["invoice_number"] for i in r.json()["invoices"]}
    assert {"INV-SPELL-1", "INV-SPELL-2"} <= numbers


def test_a_different_vendor_is_not_a_spelling_variant(client):
    make_run("Acme Office Supplies Holdings", invoice_number="INV-NOTACME")
    r = client.get("/api/portal/invoices", headers=headers(ACME))
    assert "INV-NOTACME" not in {i["invoice_number"] for i in r.json()["invoices"]}


def test_one_invoice_detail_carries_a_client_timeline(client):
    run_id = make_run("Acme Office Supplies", invoice_number="INV-TL")
    r = client.get(f"/api/portal/invoices/{run_id}", headers=headers(ACME))
    assert r.status_code == 200
    body = r.json()
    assert body["invoice_id"] == run_id
    assert body["timeline"]
    assert all(set(e) == {"at", "event"} for e in body["timeline"])
    assert "Received and processed" in [e["event"] for e in body["timeline"]]


def test_the_client_sees_only_their_own_purchase_orders(client):
    r = client.get("/api/portal/purchase-orders", headers=headers(ACME))
    assert r.status_code == 200
    pos = r.json()["purchase_orders"]
    assert pos
    assert {p["vendor"] for p in pos} == {"Acme Office Supplies"}


def test_purchase_order_remaining_is_the_ledger_figure(client):
    """Not a per-client copy of a balance. A supplier and a buyer reading the
    same order must read the same number."""
    upload_internal(client)          # consumes budget on PO-1001 if approved
    r = client.get("/api/portal/purchase-orders", headers=headers(ACME))
    for po in r.json()["purchase_orders"]:
        expected = storage.remaining_for_po(po["po_number"])
        assert abs(po["remaining"] - expected) < 0.01


def test_paging_is_bounded(client):
    for i in range(5):
        make_run("Acme Office Supplies", invoice_number=f"INV-P{i}")
    r = client.get("/api/portal/invoices?limit=2&offset=0", headers=headers(ACME))
    body = r.json()
    assert len(body["invoices"]) == 2
    assert body["total"] == 5
    # Past the ceiling is refused rather than silently clamped, so a caller
    # cannot believe they have seen everything when they have seen a page.
    assert client.get(f"/api/portal/invoices?limit={portal.MAX_PAGE + 1}",
                      headers=headers(ACME)).status_code == 422


def test_state_filter_uses_the_client_vocabulary(client):
    make_run("Acme Office Supplies", status="APPROVED", invoice_number="INV-OK")
    make_run("Acme Office Supplies", status="NEEDS_REVIEW", invoice_number="INV-HOLD")
    r = client.get("/api/portal/invoices?state=IN_REVIEW", headers=headers(ACME))
    numbers = {i["invoice_number"] for i in r.json()["invoices"]}
    assert numbers == {"INV-HOLD"}

    # The INTERNAL word is not a valid filter here -- there is one vocabulary
    # on this surface, and it is the client's.
    assert client.get("/api/portal/invoices?state=NEEDS_REVIEW",
                      headers=headers(ACME)).status_code == 400


# ==========================================================================
# 4. a client cannot see another client's data -- IDOR, every route
# ==========================================================================

@pytest.fixture
def two_clients_with_invoices(client):
    acme_id = make_run("Acme Office Supplies", invoice_number="INV-A-SECRET")
    globex_id = make_run("Globex Logistics", invoice_number="INV-G-SECRET")
    return acme_id, globex_id


def test_the_list_never_carries_the_other_clients_invoice(client, two_clients_with_invoices):
    acme_id, globex_id = two_clients_with_invoices
    body = client.get("/api/portal/invoices", headers=headers(ACME)).json()
    ids = [i["invoice_id"] for i in body["invoices"]]
    assert acme_id in ids and globex_id not in ids
    assert "INV-G-SECRET" not in json.dumps(body)


@pytest.mark.parametrize("suffix", ["", "/document", "/document/download"])
def test_another_clients_invoice_id_is_404_not_403(client, two_clients_with_invoices,
                                                   suffix):
    """404, and identical to the 404 a nonexistent id gets.

    A 403 would confirm the id names a real invoice, which is a fact about
    another company's business and precisely what someone walking the id space
    is trying to learn."""
    _, globex_id = two_clients_with_invoices
    theirs = client.get(f"/api/portal/invoices/{globex_id}{suffix}", headers=headers(ACME))
    missing = client.get(f"/api/portal/invoices/999999{suffix}", headers=headers(ACME))
    assert theirs.status_code == 404
    assert theirs.status_code == missing.status_code
    assert theirs.json() == missing.json()


def test_the_other_client_can_see_their_own(client, two_clients_with_invoices):
    """The mirror of the test above. Without it, a portal that returned 404
    for everything would pass the isolation tests perfectly."""
    _, globex_id = two_clients_with_invoices
    r = client.get(f"/api/portal/invoices/{globex_id}", headers=headers(GLOBEX))
    assert r.status_code == 200
    assert r.json()["invoice_number"] == "INV-G-SECRET"


def test_query_parameters_cannot_widen_the_view(client, two_clients_with_invoices):
    """There is no client/vendor parameter, and inventing one changes nothing.
    A filter a caller supplies on the dimension deciding what they may see is
    not a filter -- it is an authorization check run by the person checked."""
    _, globex_id = two_clients_with_invoices
    for qs in ("client_id=C-GLOBEX", "client=C-GLOBEX", "vendor=Globex Logistics",
               "vendor_id=V-002", "client_id=", "client_id=%25"):
        body = client.get(f"/api/portal/invoices?{qs}", headers=headers(ACME)).json()
        assert globex_id not in [i["invoice_id"] for i in body["invoices"]], qs
        assert body["client"]["client_id"] == "C-ACME"


def forged_token(**extra_claims):
    """A VALIDLY SIGNED token carrying whatever claims a caller might wish for.

    Minted by hand rather than through `auth.create_access_token`, and that is
    the entire point of this helper. The first version of the test below used
    create_access_token and passed extra keys into it -- but that function
    copies only sub/roles/scope/iss/iat/exp into the payload, so the "forged"
    claims never reached the token and the test asserted nothing. It was
    caught by mutating the code to trust a token claim and finding the test
    still passed.

    Signed with the REAL secret, so it is a token this application accepts.
    The question being asked is not "is a bad signature refused" -- that is
    tested separately -- but "does a perfectly valid token get to say who its
    holder represents".
    """
    import jwt
    import time
    payload = {"sub": ACME["username"], "roles": ["client"],
               "scope": "portal:read portal:submit",
               "iss": config.AUTH_ISSUER,
               "iat": int(time.time()), "exp": int(time.time()) + 600}
    payload.update(extra_claims)
    return jwt.encode(payload, auth.signing_secret(), algorithm="HS256")


def test_a_forged_client_claim_in_the_token_is_ignored(client, two_clients_with_invoices):
    """The binding is read from the LIVE user store, never from the token.

    So a VALIDLY SIGNED token asserting a different client is authenticated as
    its own subject and shown its own records -- the claims below are not
    rejected, they are simply never consulted."""
    _, globex_id = two_clients_with_invoices
    tok = forged_token(client_id="C-GLOBEX", vendor_ids=["V-002"],
                       client_name="Globex Logistics")
    body = client.get("/api/portal/invoices",
                      headers={"Authorization": f"Bearer {tok}"}).json()
    assert body["client"]["client_id"] == "C-ACME"
    assert body["client"]["vendors"] == ["Acme Office Supplies"]
    assert globex_id not in [i["invoice_id"] for i in body["invoices"]]
    assert client.get(f"/api/portal/invoices/{globex_id}",
                      headers={"Authorization": f"Bearer {tok}"}).status_code == 404


@pytest.mark.parametrize("claims", [
    {"client_id": "C-GLOBEX"},
    {"vendor_ids": ["V-001", "V-002"]},
    {"client_id": "C-GLOBEX", "vendor_ids": ["V-002"]},
    {"vendor_ids": ["V-001", "V-002", "V-003", "V-004"]},
    {"client_id": None, "vendor_ids": None},
    {"sub": ACME["username"], "scope": "portal:read invoice:admin"},
])
def test_no_token_claim_can_widen_a_client_binding(client, claims,
                                                   two_clients_with_invoices):
    """Every shape of the same attack, including a token that simply awards
    itself `invoice:admin` -- Phase K's live re-check intersects the token's
    scopes with what the account's CURRENT roles grant, so a scope nobody
    granted is dropped rather than honoured."""
    _, globex_id = two_clients_with_invoices
    tok = {"Authorization": "Bearer " + forged_token(**claims)}
    body = client.get("/api/portal/invoices", headers=tok).json()
    assert body["client"]["client_id"] == "C-ACME"
    assert body["client"]["vendors"] == ["Acme Office Supplies"]
    assert globex_id not in [i["invoice_id"] for i in body["invoices"]]
    # and the self-awarded internal scope buys nothing either
    assert client.get("/api/runs", headers=tok).status_code == 403


def test_a_client_cannot_borrow_another_clients_purchase_orders(client):
    body = client.get("/api/portal/purchase-orders", headers=headers(GLOBEX)).json()
    assert {p["vendor"] for p in body["purchase_orders"]} == {"Globex Logistics"}
    assert "Acme" not in json.dumps(body)


def test_a_portal_submission_naming_another_vendor_stays_with_the_submitter(client):
    """The reason `runs.client_id` exists, and the reason the visibility rule
    has two clauses instead of one.

    An invoice submitted by ACME while naming GLOBEX on the document is
    ACME's problem to explain -- it must NOT appear in GLOBEX's portal, or a
    stranger could put a document in front of any company by uploading it in
    that company's name."""
    run_id = make_run("Globex Logistics", invoice_number="INV-IMPOSTOR",
                      client_id="C-ACME")

    mine = client.get("/api/portal/invoices", headers=headers(ACME)).json()
    assert run_id in [i["invoice_id"] for i in mine["invoices"]]

    theirs = client.get("/api/portal/invoices", headers=headers(GLOBEX)).json()
    assert run_id not in [i["invoice_id"] for i in theirs["invoices"]]
    assert client.get(f"/api/portal/invoices/{run_id}",
                      headers=headers(GLOBEX)).status_code == 404


def test_document_access_is_isolated(client):
    """Driven end to end through a real upload, so the document row, the
    stored bytes and the visibility check are all the real ones."""
    acme_run = upload_internal(client)["run_id"]
    globex_run = make_run("Globex Logistics", invoice_number="INV-G-DOC")

    ok = client.get(f"/api/portal/invoices/{acme_run}/document/download",
                    headers=headers(ACME))
    assert ok.status_code == 200
    assert ok.content.startswith(b"%PDF-")

    assert client.get(f"/api/portal/invoices/{acme_run}/document/download",
                      headers=headers(GLOBEX)).status_code == 404
    assert client.get(f"/api/portal/invoices/{globex_run}/document",
                      headers=headers(ACME)).status_code == 404


# ==========================================================================
# 5. misconfigured and deactivated accounts -- all fail CLOSED
# ==========================================================================

@pytest.mark.parametrize("record,why", [
    ({"username": "bad", "roles": ["client"], "vendor_ids": ["V-001"]},
     "no client_id"),
    ({"username": "bad", "roles": ["client"], "client_id": "C-X"},
     "no vendor_ids"),
    ({"username": "bad", "roles": ["client"], "client_id": "C-X", "vendor_ids": []},
     "empty vendor_ids"),
    ({"username": "bad", "roles": ["client"], "client_id": "", "vendor_ids": ["V-001"]},
     "blank client_id"),
    ({"username": "bad", "roles": ["client"], "client_id": "C-X",
      "vendor_ids": {"V-001": True}},
     "vendor_ids of the wrong type"),
    ({"username": "bad", "roles": ["client"], "client_id": "C-X",
      "vendor_ids": ["", "  "]},
     "vendor_ids that are all blank"),
])
def test_an_incomplete_client_account_sees_nothing_not_everything(client, tmp_path,
                                                                  monkeypatch, record, why):
    """There is no safe default for a missing binding.

    Defaulting the client id to the username would bind an account to a client
    that may not exist; defaulting the vendors to "all" would hand an outside
    party every supplier's invoices. So an incomplete record is refused."""
    users = tmp_path / "bad.json"
    write_users(users, [record])
    monkeypatch.setenv("AUTH_USERS_FILE", str(users))
    r = client.get("/api/portal/invoices",
                   headers={"Authorization": "Bearer " + token_for("client", "bad")})
    assert r.status_code == 403, why


def test_vendor_ids_may_be_a_bare_string(client, tmp_path, monkeypatch):
    """One vendor written without the list is honoured, because it is the
    obvious way to write it and an operator must not silently lose access for
    picking the shorter spelling."""
    users = tmp_path / "one.json"
    write_users(users, [dict(ACME, vendor_ids="V-001")])
    monkeypatch.setenv("AUTH_USERS_FILE", str(users))
    r = client.get("/api/portal/me", headers=headers(ACME))
    assert r.status_code == 200
    assert r.json()["vendors"] == ["Acme Office Supplies"]


def test_an_unknown_vendor_id_is_reported_not_ignored(client, tmp_path, monkeypatch):
    users = tmp_path / "unk.json"
    write_users(users, [dict(ACME, vendor_ids=["V-001", "V-DOES-NOT-EXIST"])])
    monkeypatch.setenv("AUTH_USERS_FILE", str(users))
    body = client.get("/api/portal/me", headers=headers(ACME)).json()
    assert body["vendors"] == ["Acme Office Supplies"]
    assert body["notices"], "a broken supplier link must be visible, not silent"


def test_a_client_account_with_only_an_unknown_vendor_sees_nothing(client, tmp_path,
                                                                   monkeypatch):
    users = tmp_path / "unk2.json"
    write_users(users, [dict(ACME, vendor_ids=["V-NOPE"])])
    monkeypatch.setenv("AUTH_USERS_FILE", str(users))
    make_run("Acme Office Supplies", invoice_number="INV-HIDDEN")
    body = client.get("/api/portal/invoices", headers=headers(ACME)).json()
    assert body["invoices"] == []
    assert body["client"]["notices"]


def test_a_colliding_vendor_name_hides_the_invoice_from_everyone(client, tmp_path,
                                                                 monkeypatch):
    """Vendor identity is a NORMALISED NAME, and two approved vendors can in
    principle normalise to the same form.

    `rules.vendor_check` already refuses to guess between them. This refuses
    harder: the vendor is dropped from the binding entirely, so the invoice is
    shown to NOBODY rather than to both -- which is the direction an isolation
    failure has to fail in."""
    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            # Same company name, different legal record. normalize_vendor_name
            # folds the punctuation, so these two collide.
            cur.execute("INSERT INTO vendors (vendor_name, vendor_id, status) "
                        "VALUES (%s,%s,%s)",
                        ("Acme Office Supplies.", "V-TWIN", "approved"))
        conn.commit()
    finally:
        conn.close()

    make_run("Acme Office Supplies", invoice_number="INV-COLLIDE")

    body = client.get("/api/portal/invoices", headers=headers(ACME)).json()
    assert body["invoices"] == []
    assert body["client"]["vendors"] == []
    assert body["client"]["notices"]

    # And the OTHER half of the collision cannot see it either.
    users = tmp_path / "twin.json"
    write_users(users, [dict(ACME, username="twin", client_id="C-TWIN",
                             vendor_ids=["V-TWIN"])])
    monkeypatch.setenv("AUTH_USERS_FILE", str(users))
    other = client.get("/api/portal/invoices",
                       headers={"Authorization": "Bearer " + token_for("client", "twin")})
    assert other.json()["invoices"] == []


@pytest.mark.parametrize("flag", [{"disabled": True}, {"active": False}])
def test_a_deactivated_client_is_cut_off_with_its_token_still_valid(client, tmp_path,
                                                                    monkeypatch, flag):
    """Phase K's guarantee, re-proved on the new surface. The token is minted
    while the account is live and is never re-issued; only the store changes."""
    users = tmp_path / "live.json"
    write_users(users, [ACME])
    monkeypatch.setenv("AUTH_USERS_FILE", str(users))
    tok = {"Authorization": "Bearer " + token_for("client", ACME["username"])}
    assert client.get("/api/portal/me", headers=tok).status_code == 200

    write_users(users, [dict(ACME, **flag)])
    assert client.get("/api/portal/me", headers=tok).status_code == 401
    assert client.get("/api/portal/invoices", headers=tok).status_code == 401


def test_demoting_a_client_to_readonly_takes_effect_immediately(client, tmp_path,
                                                                monkeypatch):
    users = tmp_path / "demote.json"
    write_users(users, [ACME])
    monkeypatch.setenv("AUTH_USERS_FILE", str(users))
    tok = {"Authorization": "Bearer " + token_for("client", ACME["username"])}

    write_users(users, [dict(ACME, roles=["client_readonly"])])
    r = client.post("/api/portal/invoices",
                    files={"file": ("a.pdf", io.BytesIO(pdf_bytes()), "application/pdf")},
                    headers=tok)
    assert r.status_code == 403
    assert client.get("/api/portal/me", headers=tok).status_code == 200


def test_removing_a_client_account_entirely_cuts_off_the_portal(client, tmp_path,
                                                                monkeypatch):
    """Phase K documents that a DELETED account's token stays valid until it
    expires, because an IdP-minted principal legitimately has no local record.
    The portal does not inherit that gap: a portal request needs a BINDING,
    and a deleted record has none, so the surface facing outside the company
    closes on deletion as well as on deactivation."""
    users = tmp_path / "gone.json"
    write_users(users, [ACME])
    monkeypatch.setenv("AUTH_USERS_FILE", str(users))
    tok = {"Authorization": "Bearer " + token_for("client", ACME["username"])}
    assert client.get("/api/portal/me", headers=tok).status_code == 200

    write_users(users, [])
    assert client.get("/api/portal/me", headers=tok).status_code == 403


# ==========================================================================
# 6. what the portal must never disclose
# ==========================================================================

INTERNAL_SECRETS = [
    "reviewer-bob",                 # who ruled on it
    "internal note about bob",      # what they wrote
    "gemini-vision",                # how the document was read
]


def _all_portal_bodies(client, run_id):
    return "\n".join([
        client.get("/api/portal/me", headers=headers(ACME)).text,
        client.get("/api/portal/invoices", headers=headers(ACME)).text,
        client.get(f"/api/portal/invoices/{run_id}", headers=headers(ACME)).text,
        client.get(f"/api/portal/invoices/{run_id}/document",
                   headers=headers(ACME)).text,
        client.get("/api/portal/purchase-orders", headers=headers(ACME)).text,
    ])


def test_no_internal_detail_reaches_a_client(client):
    run_id = upload_internal(client)["run_id"]
    storage.record_human_review(run_id, "ACCEPTED", reviewer="reviewer-bob",
                                note="internal note about bob")
    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE runs SET extracted_json = "
                        "jsonb_set(extracted_json::jsonb,'{extraction_method}',"
                        "'\"gemini-vision\"')::text WHERE id=%s", (run_id,))
        conn.commit()
    finally:
        conn.close()

    blob = _all_portal_bodies(client, run_id)
    for secret in INTERNAL_SECRETS:
        assert secret not in blob, secret


def test_no_document_location_reaches_a_client(client):
    run_id = upload_internal(client)["run_id"]
    doc = storage.get_document_for_run(run_id)
    blob = _all_portal_bodies(client, run_id)
    assert doc["storage_key"] not in blob
    assert "storage_key" not in blob
    assert "storage_backend" not in blob


def test_no_internal_structures_reach_a_client(client):
    run_id = upload_internal(client)["run_id"]
    blob = _all_portal_bodies(client, run_id)
    for key in ("audit", "stages", "provenance", "rules_failed", "rules_passed",
                "reviewed_by", "review_note", "human_decision", "final_decision",
                "automated_decision", "uploaded_by", "current_claim",
                "extraction_method", "confidence"):
        assert f'"{key}"' not in blob, key


def test_the_internal_reason_sentence_is_never_echoed(client):
    """Internal reason strings quote other runs' ids ("matches run #7"),
    reviewer names and purchase order balances. The client explanation is
    looked up by RULE NAME from a frozen table instead, so nothing from the
    database can travel out through the prose."""
    audit = {"rules_failed": ["Duplicate check"],
             "reason": "Invoice #INV-X matches run #4242 processed on 2026-01-01.",
             "rules": [{"name": "Duplicate check", "passed": False}]}
    run_id = make_run("Acme Office Supplies", status="REJECTED",
                      invoice_number="INV-DUP", audit=audit)
    body = client.get(f"/api/portal/invoices/{run_id}", headers=headers(ACME)).json()
    assert body["state"] == "DECLINED"
    assert body["state_detail"] == [portal.RULE_EXPLANATIONS["Duplicate check"]]
    assert "4242" not in json.dumps(body)


def test_an_unmapped_rule_falls_back_rather_than_leaking(client):
    """The important half of the frozen-table design: a rule added by a later
    phase, by someone who never read portal.py, produces a vague-but-true
    sentence rather than an internal one."""
    audit = {"rules_failed": ["Some Rule Invented In Phase Q"],
             "reason": "internal detail nobody vetted for a client"}
    run_id = make_run("Acme Office Supplies", audit=audit, invoice_number="INV-NEW")
    body = client.get(f"/api/portal/invoices/{run_id}", headers=headers(ACME)).json()
    assert body["state_detail"] == ["Being checked by our accounts payable team."]
    assert "internal detail" not in json.dumps(body)


def test_a_malformed_audit_blob_degrades_rather_than_failing(client):
    run_id = make_run("Acme Office Supplies", invoice_number="INV-BAD")
    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE runs SET audit_json='{not json' WHERE id=%s", (run_id,))
        conn.commit()
    finally:
        conn.close()
    r = client.get(f"/api/portal/invoices/{run_id}", headers=headers(ACME))
    assert r.status_code == 200
    assert r.json()["state_detail"] == ["Being checked by our accounts payable team."]


def test_the_timeline_hides_internal_events_and_every_actor(client):
    run_id = upload_internal(client)["run_id"]
    storage.claim_review(run_id, "reviewer-bob")
    storage.add_comment(run_id, "reviewer-bob", "checking with procurement")

    body = client.get(f"/api/portal/invoices/{run_id}", headers=headers(ACME)).json()
    labels = [e["event"] for e in body["timeline"]]
    assert "REVIEW_CLAIMED" not in json.dumps(body)
    assert "COMMENT_ADDED" not in json.dumps(body)
    assert "reviewer-bob" not in json.dumps(body)
    assert "procurement" not in json.dumps(body)
    assert labels                      # but the client still sees SOMETHING


def test_only_the_clients_own_purchase_orders_are_named_on_an_invoice(client):
    """A multi-PO invoice can name orders raised to more than one supplier.
    The numbers listed are intersected with this client's own orders."""
    ctx = acme_ctx()
    run = {"id": 1, "status": "APPROVED", "po_number": "PO-1002",
           "po_match_json": json.dumps({"po_numbers": ["PO-1001", "PO-1002"]}),
           "audit_json": None, "extracted_json": None, "client_id": None,
           "invoice_number": "INV-M", "vendor_name": "Acme Office Supplies",
           "total": 10.0, "created_at": "2026-01-01", "filename": "x.pdf",
           "has_document": False}
    summary = portal.invoice_summary(ctx, run)
    # PO-1001 is Acme's; PO-1002 is Globex's and must not be named.
    assert summary["purchase_orders"] == ["PO-1001"]


def test_the_portal_read_surface_refuses_a_post(client):
    """Read-only means read-only.

    `/api/portal/invoices` is deliberately excluded: it is the one write on
    this surface, and it is checked on its own further down. Every other
    portal route must have no POST at all -- not a POST that happens to be
    rejected by authorization, which would start passing the day somebody
    added a handler."""
    for path in PORTAL_GETS:
        if path == "/api/portal/invoices":
            continue
        r = client.post(path, headers=headers(ACME), json={})
        assert r.status_code in (404, 405), path


def test_submission_is_the_only_write_on_the_portal_surface(client):
    """Enumerated from the app rather than from a list here, so a write added
    to this surface by a later phase fails this test instead of going
    unnoticed."""
    writes = set()
    for route in main.app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/portal"):
            continue
        for method in (getattr(route, "methods", None) or set()):
            if method not in ("GET", "HEAD", "OPTIONS"):
                writes.add((method, path))
    assert writes == {("POST", "/api/portal/invoices")}


def test_the_portal_module_never_writes(client):
    """Asserted against the parsed source, the way test_chat.py asserts the
    assistant is read-only -- a claim about a module is worth checking against
    the module, not against the tests that happen to exercise it."""
    import ast
    src = open(os.path.join(BACKEND, "portal.py"), encoding="utf-8").read()
    tree = ast.parse(src)

    # Docstrings are prose and are excluded deliberately. The first version of
    # this test scanned every string constant, so the phrase "remembering to
    # drop them afterwards" in a comment about SELECT columns read as a DROP
    # statement. A test that fails on English rather than on SQL gets weakened
    # the first time it is inconvenient, which is worse than not having it.
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docstrings:
            upper = node.value.upper()
            for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP TABLE",
                         "ALTER TABLE", "CREATE TABLE", "FOR UPDATE"):
                assert verb not in upper, f"portal.py contains SQL: {verb}"
    # And no CALL into a storage writer. Matched against the parsed tree
    # rather than against the text, for the same reason the docstrings are
    # skipped above: this module's own docstring names `save_run_checked`
    # while explaining that the submission path -- which is in main.py, not
    # here -- goes through it, and a substring search cannot tell a sentence
    # about a function from a call to it.
    writers = {"save_run", "save_run_checked", "set_run_status", "log_activity",
               "record_human_review", "write_txn", "add_comment", "claim_review",
               "release_review_claim", "save_document", "clear_run_history",
               "init_db"}
    reached = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)                 and node.value.id == "storage" and node.attr in writers:
            reached.add(node.attr)
    assert not reached, f"portal.py reaches storage writers: {sorted(reached)}"


# ==========================================================================
# 7. submission
# ==========================================================================

def test_a_client_can_submit_an_invoice(client):
    r = client.post("/api/portal/invoices",
                    files={"file": ("mine.pdf", io.BytesIO(pdf_bytes()),
                                    "application/pdf")},
                    headers=headers(ACME))
    assert r.status_code == 200
    body = r.json()
    assert body["submitted"] is True
    invoice = body["invoice"]
    assert invoice["submitted_through_portal"] is True
    assert invoice["state"] in (portal.STATE_APPROVED, portal.STATE_IN_REVIEW,
                                portal.STATE_DECLINED, portal.STATE_RECEIVED)

    # And it is immediately visible in their own list.
    listed = client.get("/api/portal/invoices", headers=headers(ACME)).json()
    assert invoice["invoice_id"] in [i["invoice_id"] for i in listed["invoices"]]


def test_a_submission_is_attributed_to_the_submitting_client(client):
    body = client.post("/api/portal/invoices",
                       files={"file": ("m.pdf", io.BytesIO(pdf_bytes()),
                                       "application/pdf")},
                       headers=headers(ACME)).json()
    run_id = body["invoice"]["invoice_id"]
    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT client_id FROM runs WHERE id=%s", (run_id,))
            assert cur.fetchone()["client_id"] == "C-ACME"
    finally:
        conn.close()

    doc = storage.get_document_for_run(run_id)
    assert doc["source"] == "CLIENT_PORTAL"
    assert doc["source"] in config.DOCUMENT_SOURCES


def test_a_submission_runs_the_same_pipeline(client):
    """One pipeline, three doors. An externally submitted invoice is judged by
    exactly the process an internally uploaded one is -- same stages, same
    audit structure -- or the portal would be a second decision engine."""
    internal = upload_internal(client)
    external = client.post("/api/portal/invoices",
                           files={"file": ("01_happy_path_acme.pdf",
                                           io.BytesIO(pdf_bytes()), "application/pdf")},
                           headers=headers(ACME)).json()["invoice"]

    a = storage.get_run(internal["run_id"])
    b = storage.get_run(external["invoice_id"])
    assert [s["name"] for s in a["stages"]] == [s["name"] for s in b["stages"]]
    assert set(a["audit"]) == set(b["audit"])


def test_the_submission_response_names_no_internal_stage(client):
    """Deliberately not streamed. The SSE frames name internal stages and
    carry their detail lines -- extraction routes, vendor lookups, PO
    balances -- which is the running commentary this phase exists not to
    print."""
    r = client.post("/api/portal/invoices",
                    files={"file": ("m.pdf", io.BytesIO(pdf_bytes()),
                                    "application/pdf")},
                    headers=headers(ACME))
    for stage in ("INGEST", "EXTRACT_TEXT", "EXTRACT_FIELDS", "VENDOR_CHECK",
                  "PO_MATCH", "DUPLICATE_CHECK", "TOLERANCE_CHECK", "DECISION"):
        assert stage not in r.text, stage
    assert "data: " not in r.text


def test_a_non_pdf_is_refused(client):
    r = client.post("/api/portal/invoices",
                    files={"file": ("x.pdf", io.BytesIO(b"not a pdf at all"),
                                    "application/pdf")},
                    headers=headers(ACME))
    assert r.status_code == 415


def test_an_empty_file_is_refused(client):
    r = client.post("/api/portal/invoices",
                    files={"file": ("x.pdf", io.BytesIO(b""), "application/pdf")},
                    headers=headers(ACME))
    assert r.status_code == 400


def test_an_oversized_upload_is_refused(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 1024)
    r = client.post("/api/portal/invoices",
                    files={"file": ("x.pdf", io.BytesIO(b"%PDF-" + b"0" * 5000),
                                    "application/pdf")},
                    headers=headers(ACME))
    assert r.status_code == 413


def test_a_submitted_filename_cannot_escape_storage(client):
    r = client.post("/api/portal/invoices",
                    files={"file": ("../../../etc/evil.pdf", io.BytesIO(pdf_bytes()),
                                    "application/pdf")},
                    headers=headers(ACME))
    assert r.status_code == 200
    run_id = r.json()["invoice"]["invoice_id"]
    doc = storage.get_document_for_run(run_id)
    assert "/" not in doc["original_filename"] and "\\" not in doc["original_filename"]
    assert ".." not in doc["original_filename"]
    # The stored key is server-generated, never derived from what was sent.
    assert doc["storage_key"].endswith(".pdf") and len(doc["storage_key"]) == 36


# --------------------------------------------------------------------------
# The vendor-identity guard. This is the risk Phase J creates that did not
# exist while only employees could upload.
# --------------------------------------------------------------------------

def test_an_invoice_naming_another_vendor_never_auto_approves(client):
    """A client submitting an invoice that names a DIFFERENT supplier must
    never charge that supplier's purchase order, whatever the rules concluded
    about the document itself."""
    before = storage.remaining_for_po("PO-1001")

    extracted = {"vendor_name": "Globex Logistics", "invoice_number": "INV-X1",
                 "total": 100.0, "currency": "USD", "extraction_method": "regex"}
    po_match = {"po_number": "PO-1002", "po_numbers": ["PO-1002"],
                "allocations": [{"po_number": "PO-1002", "amount": 100.0}]}
    run_id, final_status, extra = storage.save_run_checked(
        "x.pdf", "APPROVED", extracted, po_match, [], [],
        audit={"rules": [], "rules_failed": [], "rules_passed": []},
        uploaded_by=ACME["username"], client_id="C-ACME",
        client_vendor_mismatch=True)

    assert final_status == "NEEDS_REVIEW"
    assert extra and "does not represent" in extra["text"]
    # Nothing was charged: consumption joins to status='APPROVED'.
    assert storage.consumed_amount_for_po("PO-1002") == 0
    assert storage.remaining_for_po("PO-1001") == before


def test_the_mismatch_hold_is_recorded_as_its_own_named_rule(client):
    """`rules_failed` is a fixed vocabulary that analytics groups by and that
    portal.py translates from, so a hold with no name would be one nothing
    downstream could account for."""
    extracted = {"vendor_name": "Globex Logistics", "invoice_number": "INV-X2",
                 "total": 50.0, "currency": "USD", "extraction_method": "regex"}
    run_id, _, _ = storage.save_run_checked(
        "x.pdf", "APPROVED", extracted, {"po_number": None, "po_numbers": [],
                                         "allocations": []}, [], [],
        audit={"rules": [], "rules_failed": [], "rules_passed": []},
        uploaded_by=ACME["username"], client_id="C-ACME", client_vendor_mismatch=True)

    audit = storage.get_run(run_id)["audit"]
    assert storage.PORTAL_VENDOR_IDENTITY_RULE in audit["rules_failed"]
    # The automated decision is the immutable record of what the RULES
    # concluded and must not be rewritten by a hold applied on top of it.
    assert storage.get_run(run_id)["automated_decision"] == "NEEDS_REVIEW"


def test_the_mismatch_hold_does_not_blame_the_po_balance_rule(client):
    """The audit fix-up rewrites the PO-balance rule for the OTHER downgrade
    this function performs. A vendor-identity hold must not be attributed to
    a check that passed."""
    extracted = {"vendor_name": "Globex Logistics", "invoice_number": "INV-X3",
                 "total": 50.0, "currency": "USD", "extraction_method": "regex"}
    audit = {"rules": [{"name": "PO remaining check", "passed": True,
                        "detail": "ok", "reason": None}],
             "rules_failed": [], "rules_passed": ["PO remaining check"]}
    run_id, _, _ = storage.save_run_checked(
        "x.pdf", "APPROVED", extracted, {"po_number": None, "po_numbers": [],
                                         "allocations": []}, [], [],
        audit=audit, uploaded_by=ACME["username"], client_id="C-ACME",
        client_vendor_mismatch=True)
    stored = storage.get_run(run_id)["audit"]
    assert "PO remaining check" in stored["rules_passed"]
    assert "PO remaining check" not in stored["rules_failed"]


def test_an_unreadable_vendor_name_counts_as_not_represented(client):
    """"We could not tell whose invoice this is" must not read as "it is
    yours" on the one path where the sender is an outside party."""
    ctx = acme_ctx()
    assert portal.represents_vendor(ctx, "Acme Office Supplies") is True
    for bad in (None, "", "   ", "Globex Logistics"):
        assert portal.represents_vendor(ctx, bad) is False


def test_an_internal_upload_is_never_treated_as_a_mismatch(client):
    """False for every non-portal run, because there is no client to
    disagree with. Proved through the real internal endpoint rather than by
    inspection."""
    final = upload_internal(client)
    audit = storage.get_run(final["run_id"])["audit"]
    assert storage.PORTAL_VENDOR_IDENTITY_RULE not in (audit.get("rules_failed") or [])


# --------------------------------------------------------------------------
# Budgets
# --------------------------------------------------------------------------

def test_submission_spends_a_per_client_daily_budget(client, monkeypatch):
    """The property the assistant's separate budget established, applied to
    the door that faces outside the company: a client can spend its own
    allowance, and cannot reach the pipeline's."""
    monkeypatch.setattr(config, "DAILY_QUOTA_PORTAL_SUBMISSIONS", 1)
    files = lambda: {"file": ("m.pdf", io.BytesIO(pdf_bytes()), "application/pdf")}

    assert client.post("/api/portal/invoices", files=files(),
                       headers=headers(ACME)).status_code == 200
    second = client.post("/api/portal/invoices", files=files(), headers=headers(ACME))
    assert second.status_code == 429

    assert quota.used(quota.portal_key("C-ACME")) == 1
    # And the pipeline's own budgets were not touched by any of it.
    assert quota.used(quota.TEXT) == 0
    assert quota.used(quota.CHAT) == 0


def test_one_clients_budget_is_not_another_clients(client, monkeypatch):
    monkeypatch.setattr(config, "DAILY_QUOTA_PORTAL_SUBMISSIONS", 1)
    files = lambda: {"file": ("m.pdf", io.BytesIO(pdf_bytes()), "application/pdf")}
    assert client.post("/api/portal/invoices", files=files(),
                       headers=headers(ACME)).status_code == 200
    assert client.post("/api/portal/invoices", files=files(),
                       headers=headers(ACME)).status_code == 429
    # GLOBEX has spent nothing and is unaffected.
    assert client.post("/api/portal/invoices", files=files(),
                       headers=headers(GLOBEX)).status_code == 200


def test_the_portal_quota_key_is_per_client(client):
    assert quota.portal_key("C-ACME") != quota.portal_key("C-GLOBEX")
    assert quota.limit_for(quota.portal_key("C-ACME")) == \
        config.DAILY_QUOTA_PORTAL_SUBMISSIONS
    # and it does not disturb the existing keys
    assert quota.limit_for(quota.VISION) == config.DAILY_QUOTA_VISION
    assert quota.limit_for(quota.TEXT) == config.DAILY_QUOTA_TEXT
    assert quota.limit_for(quota.CHAT) == config.DAILY_QUOTA_CHAT


def test_the_portal_read_surface_is_rate_limited(client, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_PORTAL_PER_MINUTE", 3)
    ratelimit.limiter.reset()
    codes = [client.get("/api/portal/me", headers=headers(ACME)).status_code
             for _ in range(6)]
    assert 429 in codes


def test_submission_is_rate_limited(client, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_PORTAL_SUBMIT_PER_MINUTE", 1)
    ratelimit.limiter.reset()
    files = lambda: {"file": ("m.pdf", io.BytesIO(pdf_bytes()), "application/pdf")}
    first = client.post("/api/portal/invoices", files=files(), headers=headers(ACME))
    second = client.post("/api/portal/invoices", files=files(), headers=headers(ACME))
    assert first.status_code == 200
    assert second.status_code == 429


# ==========================================================================
# 8. Phase K protections are preserved on the new surface
# ==========================================================================

@pytest.mark.parametrize("path", ["/api/portal/me", "/api/portal/invoices"])
def test_security_headers_are_present_on_portal_responses(client, path):
    r = client.get(path, headers=headers(ACME))
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "no-referrer"
    assert "Content-Security-Policy" in r.headers


def test_an_error_body_names_no_path_or_traceback(client):
    r = client.get("/api/portal/invoices/999999", headers=headers(ACME))
    body = r.text
    assert "Traceback" not in body
    assert "backend" not in body
    assert ROOT.replace("\\", "/") not in body.replace("\\", "/")


def test_no_password_hash_or_secret_reaches_the_portal(client):
    run_id = make_run("Acme Office Supplies", invoice_number="INV-S")
    blob = _all_portal_bodies(client, run_id)
    assert "pbkdf2_sha256" not in blob
    assert "password" not in blob.lower()
    assert auth.signing_secret() not in blob


@pytest.mark.parametrize("hostile", [
    "'; DROP TABLE runs; --",
    "1 OR 1=1",
    "%",
    "_",
    "../../etc/passwd",
    "C-GLOBEX",
])
def test_hostile_filter_values_do_not_widen_or_break_anything(client, hostile):
    globex_id = make_run("Globex Logistics", invoice_number="INV-G-H")
    make_run("Acme Office Supplies", invoice_number="INV-A-H")
    r = client.get("/api/portal/invoices", params={"state": hostile},
                   headers=headers(ACME))
    assert r.status_code in (200, 400)
    if r.status_code == 200:
        assert globex_id not in [i["invoice_id"] for i in r.json()["invoices"]]
    # the table is still there
    assert client.get("/api/portal/invoices",
                      headers=headers(ACME)).status_code == 200


def test_the_visibility_clause_refuses_an_unsafe_alias(client):
    """Every caller-supplied value is a bind parameter; the alias is the only
    interpolated fragment and is checked, so a future edit threading a request
    value through here fails loudly instead of becoming an injection point."""
    ctx = acme_ctx()
    for bad in ("runs; DROP TABLE runs", "runs WHERE 1=1", "", "a b"):
        with pytest.raises(ValueError):
            portal.visibility_clause(ctx, bad)


# ==========================================================================
# 9. the internal application is unchanged
# ==========================================================================

def test_internal_users_still_see_every_run(client):
    """The portal narrows what a CLIENT sees. It must not narrow anything for
    the AP team -- Phase D exists because several employees work one shared
    queue."""
    make_run("Acme Office Supplies", invoice_number="INV-I1")
    make_run("Globex Logistics", invoice_number="INV-I2")
    runs = client.get("/api/runs", headers=auth_headers("viewer")).json()
    numbers = {r["invoice_number"] for r in runs}
    assert {"INV-I1", "INV-I2"} <= numbers


def test_a_portal_submission_appears_in_the_internal_queue(client):
    """An invoice a supplier sent is an ordinary run for the AP team, in the
    ordinary place, with the ordinary controls."""
    body = client.post("/api/portal/invoices",
                       files={"file": ("m.pdf", io.BytesIO(pdf_bytes()),
                                       "application/pdf")},
                       headers=headers(ACME)).json()
    run_id = body["invoice"]["invoice_id"]
    r = client.get(f"/api/runs/{run_id}", headers=auth_headers("viewer"))
    assert r.status_code == 200
    assert r.json()["audit"] is not None
    assert client.get(f"/api/runs/{run_id}/document",
                      headers=auth_headers("viewer")).status_code == 200


def test_an_internal_upload_still_stores_no_client_id(client):
    final = upload_internal(client)
    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT client_id FROM runs WHERE id=%s", (final["run_id"],))
            assert cur.fetchone()["client_id"] is None
    finally:
        conn.close()


def test_the_new_column_and_index_exist_and_nothing_else_was_added(client):
    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema=%s AND table_name='runs'",
                        (storage.PG_SCHEMA,))
            assert "client_id" in {r["column_name"] for r in cur.fetchall()}

            cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname=%s",
                        (storage.PG_SCHEMA,))
            assert "idx_runs_client_id" in {r["indexname"] for r in cur.fetchall()}

            cur.execute("SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema=%s", (storage.PG_SCHEMA,))
            tables = {r["table_name"] for r in cur.fetchall()}
    finally:
        conn.close()

    # No clients table, no portal_sessions, no per-client cache of anything.
    # Client identity lives in the user store, for the same reason there is no
    # users table, and everything else is derived at read time.
    assert not [t for t in tables if "client" in t or "portal" in t]


def test_production_config_still_refuses_the_demo_client_accounts(client, monkeypatch):
    """The two demo client accounts shipped in data/users.json carry the same
    `demo` flag the internal four do, so the existing production gate refuses
    them without needing to learn what a client is."""
    monkeypatch.setenv("AUTH_USERS_FILE", os.path.join(ROOT, "data", "users.json"))
    monkeypatch.setattr(config, "app_env", lambda: "production")
    names = auth.demo_usernames()
    assert {"acme", "globex"} <= set(names)
    problems = "\n".join(auth.validate_production_config())
    assert "acme" in problems and "globex" in problems


def test_a_portal_document_view_is_attributed_to_the_supplier_account(client):
    """The activity history the AP team reads must say WHO looked.

    `actor` is NULL only for a system-generated event (an auto-approval
    cascade, an expired claim). A supplier opening their own invoice is
    neither the system nor anonymous, so it is recorded under the account that
    did it -- and the AP team sees, in the history they already read, that the
    vendor has seen it.
    """
    run_id = upload_internal(client)["run_id"]
    assert client.get(f"/api/portal/invoices/{run_id}/document",
                      headers=headers(ACME)).status_code == 200

    events = [e for e in storage.list_activity(run_id)
              if e["event_type"] == "DOCUMENT_VIEWED"]
    assert events, "the view was not recorded at all"
    assert events[-1]["actor"] == ACME["username"]
    assert events[-1]["metadata"]["client_id"] == "C-ACME"
    assert events[-1]["metadata"]["portal"] is True


def test_a_portal_read_does_not_issue_a_query_per_row(client):
    """The invoice list needs this client's purchase orders to decide which PO
    numbers may be named on a row. Resolved once per request and cached on the
    context, not fetched per row.

    Asserted by counting calls rather than by reading the code, because an
    N+1 reintroduced by a later edit looks completely correct at every
    individual call site."""
    # Each run NAMES a purchase order, which is what makes the per-row lookup
    # happen at all -- `_client_po_numbers` returns early for a run with no PO
    # numbers on it, so a fixture without them would pass this test while
    # exercising nothing.
    for i in range(6):
        make_run("Acme Office Supplies", invoice_number=f"INV-N{i}",
                 po_number="PO-1001")

    calls = {"pos": 0, "vendors": 0}
    real_pos, real_vendors = storage.list_purchase_orders, storage.list_vendors

    def counted_pos():
        calls["pos"] += 1
        return real_pos()

    def counted_vendors():
        calls["vendors"] += 1
        return real_vendors()

    storage.list_purchase_orders = counted_pos
    storage.list_vendors = counted_vendors
    try:
        r = client.get("/api/portal/invoices", headers=headers(ACME))
    finally:
        storage.list_purchase_orders = real_pos
        storage.list_vendors = real_vendors

    assert r.status_code == 200
    assert len(r.json()["invoices"]) == 6
    # One resolution of the binding, one of the orders -- not one per row.
    assert calls["vendors"] == 1, calls
    assert calls["pos"] == 1, calls


def test_the_context_cache_cannot_outlive_a_request(client, tmp_path, monkeypatch):
    """The memoisation is per CONTEXT, and a context is built fresh from the
    live user store on every request. So re-pointing an account at a different
    supplier takes effect on the next call rather than being served from a
    cached answer -- which is what a module-level cache would have done."""
    users = tmp_path / "repoint.json"
    write_users(users, [ACME])
    monkeypatch.setenv("AUTH_USERS_FILE", str(users))
    make_run("Acme Office Supplies", invoice_number="INV-BEFORE")
    make_run("Globex Logistics", invoice_number="INV-AFTER")

    first = client.get("/api/portal/invoices", headers=headers(ACME)).json()
    assert {i["invoice_number"] for i in first["invoices"]} == {"INV-BEFORE"}

    write_users(users, [dict(ACME, vendor_ids=["V-002"])])
    second = client.get("/api/portal/invoices", headers=headers(ACME)).json()
    assert {i["invoice_number"] for i in second["invoices"]} == {"INV-AFTER"}


def test_the_identity_hold_wins_when_the_balance_check_would_also_fire(client):
    """An invoice that trips both is described by the IDENTITY reason.

    "We are not sure who sent this" is a more serious thing to tell a reviewer
    than "it is slightly over budget", and the balance figure is meaningless
    until the first question is settled. The order of the two checks inside
    save_run_checked decides this, and running the balance re-check first
    would quietly win the tie -- it downgrades the very status the identity
    branch tests.
    """
    # PO-1001 is Acme's and is worth 1240. Bill far past it, as GLOBEX, from
    # a client that represents neither.
    extracted = {"vendor_name": "Globex Logistics", "invoice_number": "INV-BOTH",
                 "total": 99999.0, "currency": "USD", "extraction_method": "regex"}
    po_match = {"po_number": "PO-1001", "po_numbers": ["PO-1001"],
                "allocations": [{"po_number": "PO-1001", "amount": 99999.0}]}
    run_id, final_status, extra = storage.save_run_checked(
        "both.pdf", "APPROVED", extracted, po_match, [], [],
        tolerance_for=lambda x: 0.0,
        audit={"rules": [{"name": "PO remaining check", "passed": True,
                          "detail": "ok", "reason": None}],
               "rules_failed": [], "rules_passed": ["PO remaining check"]},
        uploaded_by=ACME["username"], client_id="C-ACME",
        client_vendor_mismatch=True)

    assert final_status == "NEEDS_REVIEW"
    assert "does not represent" in extra["text"]

    audit = storage.get_run(run_id)["audit"]
    assert storage.PORTAL_VENDOR_IDENTITY_RULE in audit["rules_failed"]
    assert "does not represent" in audit["reason"]
    # Nothing charged either way -- consumption joins to status='APPROVED'.
    assert storage.consumed_amount_for_po("PO-1001") == 0
