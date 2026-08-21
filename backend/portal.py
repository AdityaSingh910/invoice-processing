"""The client portal (Phase J) -- what one external vendor may see of this system.

WHAT THIS MODULE IS

Everything up to Phase I was built for people inside the company, and the
authorization model says so plainly: there is no per-user invoice ownership,
because this is a SHARED accounts-payable queue and the whole point of Phase D
is that several employees work the same invoices. `invoice:read` reads every
run, every document and every activity row, and that is the product rather
than an oversight.

Phase J adds the first caller for whom that is completely wrong. A vendor
signing in to ask "where is my invoice" must see their own records and
absolutely nothing else -- not another vendor's invoice, not another vendor's
purchase order, not the name of the employee reviewing theirs, not the reason
sentence that happens to quote a different run's id.

So this module is a SECOND, much narrower view over the same rows: a
visibility predicate, and a set of projections that hand-list every field that
leaves. It is not a filter bolted onto the internal API -- see the note on
scopes below for why that distinction is the whole security argument.

THE FOUR PROPERTIES THIS MODULE IS BUILT TO HAVE

1.  THE CALLER CANNOT INFLUENCE WHAT IS VISIBLE TO THEM. The client id and the
    vendor binding are resolved from the LIVE user store on every request
    (auth.client_binding), never from the token and never from anything in the
    URL, the query string or the body. A run id in a path is only ever an
    ADDITIONAL narrowing on top of the predicate, so someone else's run id
    returns exactly what a nonexistent one returns.

2.  FILTERING HAPPENS IN SQL, BEFORE ANYTHING IS READ. There is no
    fetch-then-filter path in this file. A projection function is never handed
    a row the predicate did not already select, so forgetting to check inside
    one cannot leak anything.

3.  NOTHING LEAVES THAT WAS NOT NAMED. Every response is assembled field by
    field, the way chat.py's retrievers are. `audit_json`, `stages_json`,
    `extracted_json`, provenance and confidence, the extraction route and
    provider, `reviewed_by`, `review_note`, `human_decision`, `final_decision`,
    `uploaded_by`, `current_claim`, `storage_key` and `storage_backend` are
    none of them reachable from here.

4.  THE PROSE IS FROZEN, NOT FORWARDED. A client is never shown a sentence
    this application wrote about its own internals. Internal reason strings
    embed other runs' ids ("matches run #7"), reviewer usernames, purchase
    order balances and extraction routes, so none of them is echoed. The
    explanation a client reads is looked up from RULE_EXPLANATIONS below,
    keyed by RULE NAME -- `audit_json.rules_failed` is a fixed, hand-written
    vocabulary (that is why analytics can group by it), which makes it the one
    part of that structure safe to translate from.

WHY THE PORTAL DOES NOT REUSE invoice:read

Because then isolation would be a property of forty-odd separate endpoints
instead of a property of the token. A client role carrying NO invoice:* scope
is refused by every existing internal route without one of them changing, and
without depending on anyone remembering to add a filter to the next one. See
auth.SCOPES for the longer version.

READ-ONLY, WITH ONE EXCEPTION THAT IS NOT HERE

Nothing in this module writes. Portal submission -- the one path that creates
anything -- lives in main.py, drives the existing `run_pipeline` unchanged and
commits through `storage.save_run_checked`, so it goes through exactly the
same stages, the same rules, the same allocation ledger and the same review
routing an internal upload does. There is no second pipeline and no second
decision engine for external invoices.
"""
import json
import re

import auth
import storage

# --------------------------------------------------------------------------
# What a client is told, in a vocabulary of this application's own choosing
#
# Deliberately NOT the internal status words. `NEEDS_REVIEW` is accurate and
# means nothing to a supplier; worse, `REJECTED` reads as an accusation when
# the cause is usually a duplicate submission or a purchase order that has
# already been billed in full.
# --------------------------------------------------------------------------
STATE_RECEIVED = "RECEIVED"
STATE_IN_REVIEW = "IN_REVIEW"
STATE_APPROVED = "APPROVED"
STATE_DECLINED = "DECLINED"

_STATE_FOR_STATUS = {
    "APPROVED": (STATE_APPROVED, "Approved for payment."),
    "NEEDS_REVIEW": (STATE_IN_REVIEW,
                     "Received and being checked by our accounts payable team."),
    "REJECTED": (STATE_DECLINED, "Not accepted for payment."),
}

# A status this module has never heard of. Reported as RECEIVED rather than
# guessed at or reported as an error: the invoice IS on file, which is the one
# thing that can honestly be said about it.
_STATE_UNKNOWN = (STATE_RECEIVED, "Received and on file.")

# --------------------------------------------------------------------------
# The explanations a client is allowed to read
#
# Keyed by the rule names rules.py writes into `audit_json.rules_failed` (plus
# the portal's own identity check). Each sentence is written for the SUPPLIER:
# it says what is holding their invoice and, where there is one, what they can
# do about it -- without naming another run, another vendor, an employee, a
# purchase order balance, or anything about how this system reads a document.
#
# A rule with no entry here falls through to _GENERIC_HOLD. That is the
# important half of the design: a rule added later, by someone who has never
# read this file, produces a vague-but-true sentence rather than leaking an
# internal one, because nothing here forwards a string it was not given.
# --------------------------------------------------------------------------
RULE_EXPLANATIONS = {
    "Duplicate check":
        "This invoice appears to duplicate one already submitted. No action is "
        "needed unless you believe it was sent in error.",
    "Vendor approved":
        "We could not match this invoice to your supplier record. Our team is "
        "confirming it.",
    "Document is an invoice":
        "The document submitted did not read as an invoice. Please check that "
        "the correct file was sent.",
    "Required fields present":
        "Some details could not be read from the document -- typically the "
        "invoice number, date or total. Our team is checking it.",
    "Document readable":
        "The document could not be read automatically, so it is being checked "
        "by hand. A text-based PDF processes faster than a scan or photograph.",
    "Extraction confidence":
        "Some details could not be read with confidence, so the invoice is "
        "being checked by hand.",
    "Invoice amount valid":
        "The invoice total could not be accepted as stated. Our team is "
        "checking it.",
    "Invoice arithmetic":
        "The stated subtotal, tax and total do not add up. Please check the "
        "figures on the document.",
    "PO matched":
        "This invoice could not be matched to a purchase order. Quoting the "
        "purchase order number on the document helps it clear automatically.",
    "Invoice-to-PO split stated":
        "This invoice covers more than one purchase order and does not state "
        "how the total is divided between them, so it is being confirmed by "
        "hand.",
    "PO remaining check":
        "The amount billed is more than the purchase order has remaining. Our "
        "team is confirming it.",
    "Currency match":
        "The invoice currency differs from the purchase order currency, so it "
        "is being confirmed by hand.",
    "Currency/amount not reused across currencies":
        "The invoice currency does not appear to match the amount as stated. "
        "Please check the currency on the document.",
    "Security screen":
        "This document is being checked by hand before any action is taken.",
    storage.PORTAL_VENDOR_IDENTITY_RULE:
        "The supplier named on this document is not one your account "
        "represents. Our team is confirming who it is from.",
}

_GENERIC_HOLD = "Being checked by our accounts payable team."
_GENERIC_DECLINED = "Not accepted for payment. Please contact your accounts payable contact."

# --------------------------------------------------------------------------
# The timeline a client is allowed to see
#
# An allowlist, not a denylist, and the difference is the point: an event type
# added to invoice_activity by a later phase does not appear on a vendor's
# screen until somebody decides what a vendor should be told about it.
#
# Note what is absent. REVIEW_CLAIMED, REVIEW_RELEASED, COMMENT_ADDED,
# DOCUMENT_VIEWED and DOCUMENT_DOWNLOADED are all real events about real
# employees doing their jobs, and "Bob opened your invoice at 15:04, then put
# it back" is internal. The actor is stripped from every event that IS shown,
# for the same reason.
# --------------------------------------------------------------------------
CLIENT_VISIBLE_EVENTS = {
    "PROCESSING_COMPLETED": "Received and processed",
    "REVIEW_REQUIRED": "Referred for checking",
    "ACCEPTED": "Approved by our team",
    "REJECTED": "Declined by our team",
    "AUTO_APPROVED": "Approved",
    "STATUS_OVERRIDDEN": "Decision updated",
}

# The most invoices one portal response will return. A ceiling rather than a
# page size a caller may raise, because there is no legitimate portal use for
# an unbounded scan and every reason not to offer one to an external caller.
MAX_PAGE = 100
DEFAULT_PAGE = 25

class PortalError(Exception):
    """A client account that cannot be served. Carries the client-facing text.

    Raised (rather than returning an empty result) only for a MISCONFIGURED
    account -- one whose record names no client, no vendors, or vendors that
    do not exist. An empty result would read to the vendor as "you have no
    invoices", which is a false statement about their business; this reads as
    "your account is not set up", which is true and actionable.
    """


class ClientContext:
    """One authenticated external client, resolved against live reference data.

    Built fresh per request from the live user store and the live `vendors`
    table. Nothing here is cached between requests and nothing here came from
    the caller, so re-pointing an account at a different vendor, or disabling
    it, takes effect on the very next call.
    """

    def __init__(self, client_id, client_name, vendor_ids, username=None):
        self.client_id = client_id
        self.client_name = client_name
        # The authenticated login behind this request. Carried so an activity
        # row a portal action writes can name a real principal: §6.1's rule is
        # that `actor` is the authenticated username or NULL for a
        # system-generated event, and NEVER a name invented for either case. A
        # supplier viewing their own document is neither the system nor
        # anonymous, so it is recorded under the account that did it.
        self.username = username
        self.requested_vendor_ids = list(vendor_ids)
        # Approved-vendor rows this client represents and that this module is
        # prepared to act on.
        self.vendors = []
        # Vendor ids in the account record that resolve to no vendor row at
        # all. Reported so a misconfiguration is visible rather than silently
        # narrowing what the client sees.
        self.unknown_vendor_ids = []
        # Vendor ids DELIBERATELY dropped because their name collides with
        # another approved vendor under this codebase's own name
        # normalisation. See resolve_client().
        self.ambiguous_vendor_ids = []
        # Per-REQUEST memoisation, and the scope is the point: a context is
        # built fresh from the live user store on every request and thrown
        # away with it, so nothing here can go stale between calls or outlive
        # a change to the account behind it.
        #
        # Without this, one invoice list costs a purchase-order query PER ROW
        # (`_client_po_numbers` needs this client's orders to decide which PO
        # numbers may be named) plus a distinct-vendor scan per visibility
        # clause -- an N+1 hidden behind two layers of helper. Caching in the
        # context rather than in a module-level dict keeps it impossible for
        # one client's answer to be served to another.
        self._cache = {}

    def cached(self, key, produce):
        if key not in self._cache:
            self._cache[key] = produce()
        return self._cache[key]

    @property
    def vendor_names(self):
        return [v["vendor_name"] for v in self.vendors]

    @property
    def normalised_vendor_names(self):
        return {storage.normalize_vendor_name(v["vendor_name"]) for v in self.vendors}


def resolve_client(binding: dict, username: str = None) -> ClientContext:
    """Turn a user-store binding into the vendors it may actually act for.

    THE AMBIGUITY RULE, WHICH IS THE ONE PART OF THIS WORTH READING TWICE.

    Vendor identity in this codebase is a NORMALISED NAME, not a key on the
    run: `runs.vendor_name` is whatever the extractor read off the document,
    and `storage.normalize_vendor_name()` is the only comparison anything here
    uses to decide whether two spellings are the same company.

    That function can, in principle, map two DIFFERENT approved vendors onto
    the same normalised form. `rules.vendor_check` already meets this case and
    already refuses to guess: more than one match is ambiguity, and it holds
    the invoice for a person rather than picking one. This module inherits
    that decision and takes it further, because the stakes here are higher --
    guessing wrong internally means one invoice is reviewed by hand, whereas
    guessing wrong here means showing one company another company's invoices.

    So a colliding vendor is dropped from the client's binding entirely, and
    recorded in `ambiguous_vendor_ids` so the condition is visible rather than
    presenting as an unexplained absence. The invoice is then shown to NOBODY
    rather than to both, which is the direction an isolation failure should
    fail in.
    """
    ctx = ClientContext(binding["client_id"], binding["client_name"],
                        binding["vendor_ids"], username=username)

    rows = storage.list_vendors()
    by_id = {}
    for row in rows:
        vid = str(row.get("vendor_id") or "").strip()
        if vid:
            by_id.setdefault(vid, []).append(row)

    # How many approved vendors share each normalised name. Computed over ALL
    # vendors, not just this client's, because a collision is a property of
    # the pair -- the other half of it belongs to somebody else.
    norm_counts = {}
    for row in rows:
        norm = storage.normalize_vendor_name(row.get("vendor_name"))
        if norm:
            norm_counts[norm] = norm_counts.get(norm, 0) + 1

    for vid in ctx.requested_vendor_ids:
        matches = by_id.get(vid) or []
        if not matches:
            ctx.unknown_vendor_ids.append(vid)
            continue
        # One vendor_id appearing on two vendor rows is itself ambiguous --
        # vendor_id is not a primary key on that table (vendor_name is), so
        # this is representable and must not be resolved by taking the first.
        if len(matches) > 1:
            ctx.ambiguous_vendor_ids.append(vid)
            continue
        row = matches[0]
        norm = storage.normalize_vendor_name(row.get("vendor_name"))
        if not norm or norm_counts.get(norm, 0) > 1:
            ctx.ambiguous_vendor_ids.append(vid)
            continue
        ctx.vendors.append(row)

    if not ctx.vendors and not ctx.unknown_vendor_ids and not ctx.ambiguous_vendor_ids:
        # The binding named vendors, none resolved, and none was recorded as a
        # problem -- which should be unreachable. Refused rather than served
        # as an unrestricted context.
        raise PortalError("This account is not linked to a supplier record. "
                          "Please contact your accounts payable contact.")
    return ctx


def context_for(principal) -> ClientContext:
    """The ClientContext for an authenticated principal, or raise PortalError.

    The single place a portal request turns an identity into a set of records.
    Reads `auth.client_binding`, which reads the LIVE user store -- so this
    reflects the account as it stands right now, not as it stood when the
    token was minted.
    """
    username = getattr(principal, "username", None)
    binding = auth.client_binding(username)
    if not binding:
        raise PortalError("This account is not set up for the supplier portal. "
                          "Please contact your accounts payable contact.")
    return resolve_client(binding, username=username)


# --------------------------------------------------------------------------
# The visibility predicate
# --------------------------------------------------------------------------

def visible_run_vendor_names(ctx: ClientContext):
    """The raw `runs.vendor_name` strings that belong to this client.

    WHY THIS IS RESOLVED IN PYTHON RATHER THAN IN SQL.

    `normalize_vendor_name()` folds case, punctuation, "&"/"and" and legal-form
    synonyms ("Corp." and "Corporation" are one company; "ABC Supplies" and
    "XYZ Supplies" are not). It has no SQL equivalent and cannot be inverted
    into a LIKE pattern, so the choice is between reimplementing it in SQL --
    two definitions of vendor identity, drifting apart, one of them deciding
    who sees whose invoices -- and resolving the small set of distinct names
    here and binding the answer as parameters. The second is obviously right.

    THE COST, STATED RATHER THAN HIDDEN: this reads the distinct vendor names
    on `runs`, which is bounded by the number of real suppliers plus however
    many ways their names have been misspelled on documents, and is served by
    the existing idx_runs_vendor_name. It is not bounded by the number of
    runs. At a volume where that stops being true, the answer is a normalised
    vendor column written at insert time, and it is a self-contained change to
    this one function.
    """
    wanted = ctx.normalised_vendor_names
    if not wanted:
        return []

    def _resolve():
        conn = storage.get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT vendor_name FROM runs "
                            "WHERE vendor_name IS NOT NULL AND vendor_name <> ''")
                names = [r["vendor_name"] for r in cur.fetchall()]
        finally:
            conn.close()
        return [n for n in names if storage.normalize_vendor_name(n) in wanted]

    return ctx.cached("run_vendor_names", _resolve)


def visibility_clause(ctx: ClientContext, alias: str = "runs"):
    """(sql, params) selecting exactly the runs this client may see.

    THE RULE, AND WHY IT IS TWO CLAUSES AND NOT ONE:

        runs.client_id = <this client>
          OR (runs.client_id IS NULL AND runs.vendor_name = ANY(<their names>))

    The first clause owns everything submitted through the portal. The second
    owns everything that reached accounts payable another way -- an employee's
    upload, or Phase G's email ingestion -- which is most of what a supplier
    actually wants to look at, and which carries no client id because nobody
    was authenticated as that supplier when it arrived.

    The `client_id IS NULL` guard on the second clause is not redundant, and
    removing it is the interesting bug. Without it, an invoice submitted by
    client A while naming vendor B on the document would match B's vendor list
    and appear in B's portal -- so a stranger could put a document in front of
    a company by uploading it in that company's name. With it, such a run is
    pinned to whoever was authenticated when it arrived and is visible to that
    account alone.

    Every value is a bind parameter; `alias` is the only interpolated fragment
    and is checked against a fixed shape, so a future edit that threads a
    request value through here fails loudly instead of becoming an injection
    point -- the same guard analytics.py and logs.py apply to their column
    names.
    """
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,30}$", alias or ""):
        raise ValueError(f"unsafe table alias: {alias!r}")
    names = visible_run_vendor_names(ctx)
    sql = (f"({alias}.client_id = %s OR "
           f"({alias}.client_id IS NULL AND {alias}.vendor_name = ANY(%s)))")
    return sql, [ctx.client_id, names]


# --------------------------------------------------------------------------
# Projections -- every field that leaves is named here, one at a time
# --------------------------------------------------------------------------

def _loads(raw):
    """Parse a JSON column, tolerating a malformed one.

    Same guarded parse analytics.py and logs.py use, for the same reason: the
    JSON columns are TEXT, not JSONB, so one bad blob would otherwise take a
    whole page down. Here it degrades to the generic explanation, which is
    true and says nothing it should not.
    """
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _failed_rules(audit):
    if not isinstance(audit, dict):
        return []
    failed = audit.get("rules_failed")
    if isinstance(failed, list):
        return [r for r in failed if isinstance(r, str)]
    return []


def client_state(status, audit):
    """(state, headline, detail_lines) -- everything a client reads about a decision.

    `detail_lines` comes only from RULE_EXPLANATIONS, keyed by rule name. No
    branch of this function can return a string that came out of the database:
    the internal `reason` and `reasons` fields are never consulted, because
    they embed other runs' ids, reviewer names and purchase order balances.
    """
    state, headline = _STATE_FOR_STATUS.get(status, _STATE_UNKNOWN)

    if state == STATE_APPROVED:
        return state, headline, []

    lines, seen = [], set()
    for rule in _failed_rules(audit):
        text = RULE_EXPLANATIONS.get(rule)
        if text and text not in seen:
            seen.add(text)
            lines.append(text)

    if not lines:
        lines = [_GENERIC_DECLINED if state == STATE_DECLINED else _GENERIC_HOLD]
    return state, headline, lines


def _client_po_numbers(ctx: ClientContext, run, po_match):
    """The purchase orders on this run that belong to THIS client.

    A purchase order number is somebody's internal reference, and a run can
    name several. Listing them all would tell a supplier which orders another
    supplier is being billed against in the rare multi-PO case, so the list is
    intersected with the orders raised to this client's own vendors.
    """
    numbers = []
    if isinstance(po_match, dict):
        for n in (po_match.get("po_numbers") or []):
            if isinstance(n, str) and n not in numbers:
                numbers.append(n)
    primary = run.get("po_number")
    if primary and primary not in numbers:
        numbers.append(primary)
    if not numbers:
        return []

    own = ctx.cached("own_po_numbers",
                     lambda: {str(p["po_number"]).upper()
                              for p in client_purchase_order_rows(ctx)})
    return [n for n in numbers if str(n).upper() in own]


def invoice_summary(ctx: ClientContext, run: dict) -> dict:
    """One row of the client's invoice list.

    Every key is written out below. Nothing is spread in from the run row, so
    a column added to `runs` by a later phase cannot appear here by accident.
    """
    audit = _loads(run.get("audit_json"))
    state, headline, detail = client_state(run.get("status"), audit)
    return {
        # The run id. Named `invoice_id` because that is what it identifies
        # from the supplier's side; it is the same integer the internal API
        # calls run_id, and it is theirs to quote back to us.
        "invoice_id": run.get("id"),
        "invoice_number": run.get("invoice_number"),
        "vendor_name": run.get("vendor_name"),
        "total": run.get("total"),
        "currency": (_loads(run.get("extracted_json")) or {}).get("currency"),
        "received_at": run.get("created_at"),
        "filename": run.get("filename"),
        "state": state,
        "state_headline": headline,
        "state_detail": detail,
        "purchase_orders": _client_po_numbers(ctx, run, _loads(run.get("po_match_json"))),
        # Whether the PDF we hold can be handed back. Not a storage key, not a
        # backend name, not a path -- just yes or no.
        "has_document": bool(run.get("has_document")),
        "submitted_through_portal": bool(run.get("client_id")),
    }


def invoice_timeline(run_id: int):
    """The client-visible history of one invoice.

    Rows come from `invoice_activity` -- the same append-only table Phase D
    writes and the internal UI reads -- passed through CLIENT_VISIBLE_EVENTS.
    The ACTOR IS NEVER INCLUDED, and neither is the note: both describe our
    people doing our work.
    """
    out = []
    for event in storage.list_activity(run_id):
        label = CLIENT_VISIBLE_EVENTS.get(event.get("event_type"))
        if not label:
            continue
        out.append({"at": event.get("created_at"), "event": label})
    return out


# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------

_RUN_COLUMNS = ("id, filename, status, created_at, vendor_name, invoice_number, "
                "total, po_number, extracted_json, po_match_json, audit_json, client_id")


def _select_runs(ctx: ClientContext, extra_sql: str = "", extra_params=None,
                 limit: int = None, offset: int = 0):
    """Runs visible to this client, newest first.

    Named columns rather than SELECT *: `runs` carries `stages_json`,
    `reasons_json`, `reviewed_by`, `review_note` and the human-decision
    columns, none of which may leave, and not fetching them is a stronger
    guarantee than remembering to drop them afterwards.

    `has_document` is answered with an EXISTS on the `documents` table so the
    list can say whether a PDF can be fetched without a query per row, and
    without ever reading the storage key that would say where it is.
    """
    where, params = visibility_clause(ctx)
    if extra_sql:
        where += " AND " + extra_sql
        params = params + list(extra_params or [])

    sql = (f"SELECT {_RUN_COLUMNS}, "
           f"EXISTS (SELECT 1 FROM documents d WHERE d.run_id = runs.id) AS has_document "
           f"FROM runs WHERE {where} ORDER BY id DESC")
    if limit is not None:
        sql += " LIMIT %s OFFSET %s"
        params = params + [int(limit), int(offset)]

    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def list_invoices(ctx: ClientContext, limit: int = DEFAULT_PAGE, offset: int = 0,
                  state: str = None) -> dict:
    """This client's invoices, newest first, with a total count.

    `state` filters on the CLIENT vocabulary, not the internal one, so the
    filter a supplier sees on screen is the filter that reaches the database
    -- there is no second mapping for them to disagree about.
    """
    limit = max(1, min(int(limit or DEFAULT_PAGE), MAX_PAGE))
    offset = max(0, int(offset or 0))

    extra_sql, extra_params = "", []
    if state:
        internal = [k for k, v in _STATE_FOR_STATUS.items() if v[0] == state]
        if not internal:
            raise PortalError(f"Unknown status filter: {state}")
        extra_sql, extra_params = "runs.status = ANY(%s)", [internal]

    rows = _select_runs(ctx, extra_sql, extra_params, limit=limit, offset=offset)

    where, params = visibility_clause(ctx)
    if extra_sql:
        where += " AND " + extra_sql
        params = params + extra_params
    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM runs WHERE {where}", params)
            total = cur.fetchone()["n"]
    finally:
        conn.close()

    return {
        "client": client_identity(ctx),
        "invoices": [invoice_summary(ctx, r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def get_invoice(ctx: ClientContext, run_id: int):
    """One invoice, or None if it is not this client's.

    None covers "no such invoice" and "somebody else's invoice" together, and
    the endpoint turns both into the same 404 -- deliberately. A 403 on
    another client's run would confirm that the run exists, which is a fact
    about another company's business.
    """
    rows = _select_runs(ctx, "runs.id = %s", [int(run_id)], limit=1)
    if not rows:
        return None
    run = rows[0]
    detail = invoice_summary(ctx, run)
    detail["timeline"] = invoice_timeline(run["id"])
    return detail


def invoice_document_row(ctx: ClientContext, run_id: int):
    """The `documents` row for one of THIS CLIENT'S invoices, or None.

    Resolves visibility first and reuses `storage.get_document_for_run`
    unchanged -- the portal has no document storage of its own, and Phase C's
    store is reached with the same server-generated key the internal endpoint
    uses. The row is returned whole because the CALLER projects it; nothing
    here hands it to a client.
    """
    if not _select_runs(ctx, "runs.id = %s", [int(run_id)], limit=1):
        return None
    return storage.get_document_for_run(int(run_id))


def client_purchase_order_rows(ctx: ClientContext):
    """Raw `purchase_orders` rows raised to this client's vendors.

    Matched through `normalize_vendor_name` -- the same comparison the rest of
    this module and `rules.vendor_check` use -- rather than on the raw string,
    so a purchase order recorded as "Acme Office Supplies Inc." belongs to the
    same supplier as one recorded as "Acme Office Supplies, Inc".
    """
    wanted = ctx.normalised_vendor_names
    if not wanted:
        return []
    return ctx.cached("purchase_orders", lambda: [
        po for po in storage.list_purchase_orders()
        if storage.normalize_vendor_name(po.get("vendor")) in wanted])


def purchase_orders(ctx: ClientContext) -> dict:
    """This client's purchase orders and what is left on each.

    `remaining` is the LEDGER's own figure -- summed from `run_allocations`
    joined to APPROVED runs, exactly as the internal reference screen reports
    it -- so a supplier and a buyer reading the same order see the same
    number. It is derived here as everywhere else; there is no per-client copy
    of a balance.

    Deliberately NOT windowed by date. A balance "as of the last 30 days" is
    meaningless to somebody deciding what they may still invoice against an
    order.
    """
    consumed = storage.consumed_amounts_by_po()
    out = []
    for po in client_purchase_order_rows(ctx):
        amount = po.get("amount")
        spent = round(float(consumed.get(po["po_number"], 0.0) or 0.0), 2)
        out.append({
            "po_number": po.get("po_number"),
            "vendor": po.get("vendor"),
            "description": po.get("description"),
            "issued_date": po.get("issued_date"),
            "status": po.get("status"),
            "currency": po.get("currency"),
            "amount": amount,
            "billed": spent,
            "remaining": round(float(amount or 0.0) - spent, 2),
        })
        # `source_file` and `source_row` are on the row and stay there: they
        # name a file on our side of the boundary.
    out.sort(key=lambda p: str(p["po_number"]))
    return {"client": client_identity(ctx), "purchase_orders": out}


def client_identity(ctx: ClientContext) -> dict:
    """Who the portal thinks the caller is, and which suppliers they cover.

    `notices` is how a misconfigured binding becomes visible instead of
    presenting as missing invoices. A vendor id that names nothing, or one
    dropped for ambiguity, is reported in plain language -- it is the
    difference between a supplier ringing up to say "your portal is broken"
    and one quietly assuming we lost their invoices.

    The vendor IDS are deliberately not returned: they are our procurement
    reference, and the supplier's own name is the part that means anything to
    them.
    """
    notices = []
    if ctx.unknown_vendor_ids:
        notices.append("Part of this account's supplier link is not recognised. "
                       "Some invoices may not be listed. Please contact your "
                       "accounts payable contact.")
    if ctx.ambiguous_vendor_ids:
        notices.append("Part of this account's supplier link matches more than one "
                       "supplier record, so it has been left out rather than "
                       "guessed at. Please contact your accounts payable contact.")
    return {
        "client_id": ctx.client_id,
        "client_name": ctx.client_name,
        "vendors": ctx.vendor_names,
        "notices": notices,
    }


def represents_vendor(ctx: ClientContext, vendor_name: str) -> bool:
    """Whether a vendor name as read off a document is one this client covers.

    Used by the submission path to decide whether an invoice needs a person to
    confirm who it is from. An UNREADABLE vendor name (None, empty) counts as
    NOT represented, which is the safe direction: an invoice whose supplier
    could not be read is already held for review by the rules, and treating
    "we could not tell" as "it is yours" would be exactly the wrong default on
    the one path where the sender is an outside party.
    """
    norm = storage.normalize_vendor_name(vendor_name)
    return bool(norm) and norm in ctx.normalised_vendor_names
