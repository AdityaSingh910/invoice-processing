"""Phase I: logs, filtering, grouping and exports.

WHAT THIS IS

Phase D gave every invoice an append-only history (`invoice_activity`) and
Phase F gave every incoming message one (`email_activity`). Both have been
readable ONE ENTITY AT A TIME ever since -- `GET /api/runs/{id}/activity` and
`GET /api/email/messages/{id}` -- which answers "what happened to this
invoice" and cannot answer "what happened yesterday", "what has this vendor
been doing", or "show me every rejection this month".

This module is that second question. It is a QUERY LAYER over the rows those
phases already write.


WHAT IT IS NOT, AND THIS IS THE WHOLE DESIGN

**Nothing in this file writes.** There is no `logs` table, no `log_entries`,
no denormalised search index, no event mirror. Not one INSERT, not one UPDATE.
A second log would be a second truth: the moment one code path forgot to
mirror an event, the log and the history it claims to show would disagree, and
nobody would find out until an auditor asked. `invoice_activity` and
`email_activity` ARE the log; this reads them.

That is the same call this project has now made four times -- PO balances
(§3), the review-claim holder (§6.2), every KPI (§7c.1), and now this -- and
for the same reason each time.

The only schema change Phase I makes is ONE index (`email_activity(created_at)`,
in storage.init_db) -- the sanctioned way to make a derived read cheap. It
exists because every query here filters on that column and nothing indexed it:
`email_activity` had only `(email_id)`, which serves "one message's history"
and nothing else. `invoice_activity(created_at)` and `(actor)` already exist
from Phases D and H and are reused as they are.


TWO STREAMS, ONE SHAPE, NOT ONE TABLE

`invoice_activity.run_id` is NOT NULL and foreign-keyed to `runs`;
`email_activity.email_id` is NOT NULL and foreign-keyed to `email_messages`. A
quarantined message has no run and may never have one, which is exactly why
Phase F did not put its events in `invoice_activity` (§7a.7). Phase I does not
relitigate that: it UNIONs the two at read time into one row shape, keeping
`stream` on every row so a reader always knows which table an event came from
and the detail endpoint can go back to the right one.

Rows are joined to their subject (`runs`, `email_messages`) for context --
vendor, invoice number, decision, status -- because a log line reading
"REJECTED by ada" with no indication of which invoice is not a log, it is a
riddle.


AND A THIRD HISTORY, WHICH IS NOT A STREAM

The per-run stage log (`runs.stages_json`) is the other record this phase
makes queryable, and it is deliberately NOT in that union: it is a JSON array
on the run, not rows, so it can only join by being flattened into rows the
database does not hold. It gets its own view over the same filters --
`stage_rows()`, one row per stage instead of one row per event -- and the
section above it explains what that costs. `GET /api/runs/{id}` could always
show ONE run's stages; this answers "which invoices failed at VENDOR_CHECK
last week".


TIME

`analytics.resolve_window()` does all date handling. Deliberately not a second
parser: it is already validated, already half-open `[start, end)`, already
UTC, and already tested against midnight boundaries. A filter panel that
parsed dates its own way would disagree with the dashboard beside it, and the
disagreement would be silent.

Logs window on WHEN THE EVENT HAPPENED (`*_activity.created_at`), which is not
what the analytics endpoints window on -- they use `runs.created_at`, because
they ask about a cohort of invoices ("of the work that arrived last week...").
A log asks what happened in a period. An event yesterday about an invoice from
last month belongs in yesterday's log, and this is the same distinction
`analytics.users()` already draws for reviewer workload (§7c.9).


SAFETY

Every caller-supplied value is a bind parameter. The only interpolated
fragments are column names and pre-built SQL from this module's own frozen
tables (`GROUPINGS`, `SEARCH_COLUMNS`, `Window.clause`), never a request
value. `search` is escaped for LIKE metacharacters so `%` typed by a user
matches a literal percent sign rather than everything.

Exports are generated here, streamed in chunks, and go through the SAME query
function the list endpoint uses -- an export that built its own WHERE clause
would be one edit away from being the way around the filters.
"""
import csv
import io
import json
import re
from datetime import datetime, timezone

import analytics
import storage
from analytics import AnalyticsError, Window


class LogError(ValueError):
    """A caller-supplied parameter was invalid. main.py maps this to a 400.

    Deliberately its own type rather than reusing AnalyticsError: the two
    modules validate different parameters, and a caller should not see a log
    filter rejected in the vocabulary of the analytics window.
    """


# --------------------------------------------------------------------------
# limits
#
# Every one of these exists because logs GROW. An unbounded response is a
# denial-of-service against the browser that asked for it, and eventually
# against the process that built it.
# --------------------------------------------------------------------------
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

# How far the exact total is counted before it is reported as "at least".
# Counting 4 million rows to render "4,000,000 results" is work nobody reads;
# the page controls only need to know whether there is a next page.
COUNT_CEILING = 10_000

# The hard stop on an export. A CSV is streamed, so memory is not the limit --
# the limit is that a file this size is a database extract, not a report, and
# the caller should narrow the filters or be told plainly that they did not
# get everything.
MAX_EXPORT_ROWS = 50_000

# Rows fetched per round trip while streaming an export.
EXPORT_CHUNK = 1_000

# Distinct values offered as filter options. A dropdown longer than this is
# not a dropdown.
MAX_FACET_VALUES = 200

# Grouped rows returned. Same reasoning as analytics.MAX_GROUP_LIMIT, and the
# same number, so the two agree.
DEFAULT_GROUP_LIMIT = 25
MAX_GROUP_LIMIT = analytics.MAX_GROUP_LIMIT


STREAMS = ("all", "invoice", "email")

# Which axes a caller may group on, and the SQL each one groups by.
#
# A FROZEN TABLE, and that is the security property: `group_by` names a key in
# this dict and nothing else ever reaches SQL. A caller cannot group by an
# expression they supply, because there is no path for one.
GROUPINGS = {
    "event":    ("event_type", "Event type"),
    "actor":    ("actor", "Person"),
    "vendor":   ("vendor_name", "Vendor"),
    "day":      ("substring(created_at from 1 for 10)", "UTC day"),
    "decision": ("automated_decision", "Automated decision"),
    "status":   ("status", "Ledger status"),
    "source":   ("source", "Source"),
    "stream":   ("stream", "Stream"),
    "run":      ("run_id", "Invoice run"),
}

# What free-text search looks at.
#
# Also frozen, for the same reason. Note what is NOT here: `note` is included
# (a reviewer's own comment is the most useful thing to search) but nothing
# reads `metadata_json`, `audit_json`, `extracted_json`, or any email header.
SEARCH_COLUMNS = ("event_type", "actor", "vendor_name", "invoice_number",
                  "po_number", "filename", "note")

# The reserved `actor` value meaning "the system acted, not a person".
#
# `actor` is NULL for a system-generated event -- an auto-approval cascade, a
# claim that expired unattended (§6.1) -- and NULL cannot be expressed in a
# query string. This token can be, and its shape makes a collision with a real
# username unlikely rather than impossible; see KNOWN LIMITATIONS in CLAUDE.md.
SYSTEM_ACTOR = "__system__"

_ID_SHAPE = re.compile(r"^[A-Za-z0-9_.:@ /+-]{1,120}$")
_EVENT_SHAPE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# --------------------------------------------------------------------------
# filters
# --------------------------------------------------------------------------

class LogFilters:
    """Everything a caller may narrow the log by, validated once.

    Built once per request and handed to whichever of `search()`, `group()`,
    `count()` or `export_rows()` runs -- so the list a user sees, the counts
    they group by, and the CSV they download are narrowed by the identical
    object. An export that re-parsed the query string would be one typo away
    from exporting rows the list never showed.
    """

    __slots__ = ("window", "stream", "actor", "event", "vendor", "run_id",
                 "invoice_number", "po_number", "decision", "status", "source",
                 "email_status", "rule_failed", "search", "order")

    def __init__(self, window: Window, stream="all", actor=None, event=None,
                 vendor=None, run_id=None, invoice_number=None, po_number=None,
                 decision=None, status=None, source=None, email_status=None,
                 rule_failed=None, search=None, order="desc"):
        self.window = window
        self.stream = _one_of("stream", stream or "all", STREAMS)
        self.actor = _text("actor", actor)
        self.event = _shaped("event", event, _EVENT_SHAPE,
                             "letters, digits, underscore or hyphen")
        self.vendor = _text("vendor", vendor, limit=200)
        self.run_id = _int("run_id", run_id)
        self.invoice_number = _shaped("invoice_number", invoice_number, _ID_SHAPE,
                                      "a plain invoice reference")
        self.po_number = _shaped("po_number", po_number, _ID_SHAPE,
                                 "a plain PO reference")
        self.decision = _upper_one_of("decision", decision, analytics.DECISIONS)
        self.status = _upper_one_of("status", status, analytics.DECISIONS)
        self.source = _upper_one_of("source", source, ("MANUAL_UPLOAD", "EMAIL"))
        self.email_status = _upper_one_of("email_status", email_status,
                                          ("ADMITTED", "QUARANTINED", "RELEASED",
                                           "DISCARDED"))
        self.rule_failed = _text("rule_failed", rule_failed, limit=200)
        self.search = _text("search", search, limit=200)
        self.order = _one_of("order", (order or "desc").lower(), ("desc", "asc"))

    # -- what each stream can honestly answer ------------------------------
    #
    # An email event has no vendor, no PO and no ledger status; an invoice
    # event has no email classification. Rather than silently returning
    # nothing when someone combines `stream=email` with `vendor=`, each
    # stream declares which filters exclude it entirely, and the query drops
    # that stream from the UNION. The result is the same rows either way --
    # this just avoids scanning a table that cannot match.

    def wants_invoice(self) -> bool:
        return self.stream in ("all", "invoice") and not self.email_status

    def wants_email(self) -> bool:
        if self.stream not in ("all", "email"):
            return False
        return not any((self.vendor, self.po_number, self.invoice_number,
                        self.decision, self.status, self.source,
                        self.rule_failed, self.run_id))

    def stage_conflicts(self) -> tuple:
        """The filters in force that the per-stage view cannot honestly apply.

        A stage is something the PIPELINE did, so it has no actor, no event
        type and no message status. The other two streams answer such a
        combination with no rows, which costs nothing because no row could
        have matched anyway (`wants_email`). Here it would be a lie of a
        different kind: the runs exist and their stage logs exist, so an empty
        page would read as "this vendor's runs have no stages" rather than as
        "you asked a question about people of a record that has none". The
        conflicting filters are named instead, and the caller is told.
        """
        clashes = []
        if self.stream == "email":
            clashes.append("stream=email")
        for name in ("actor", "event", "email_status"):
            if getattr(self, name) is not None:
                clashes.append(name)
        return tuple(clashes)

    def describe(self) -> dict:
        """The filters actually in force, echoed back.

        A caller (and an export's own header row) can then state what a
        result set was narrowed by, rather than trusting that the client
        still holds the same filter state it sent.
        """
        out = {"range": self.window.as_dict(), "stream": self.stream}
        for name in ("actor", "event", "vendor", "run_id", "invoice_number",
                     "po_number", "decision", "status", "source", "email_status",
                     "rule_failed", "search"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        out["order"] = self.order
        return out


def _text(field, value, limit=120):
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    if len(value) > limit:
        raise LogError(f"'{field}' is too long (limit {limit} characters)")
    return value


def _shaped(field, value, pattern, expected):
    value = _text(field, value)
    if value is None:
        return None
    if not pattern.fullmatch(value):
        raise LogError(f"'{field}' must be {expected}")
    return value


def _one_of(field, value, allowed):
    if value not in allowed:
        raise LogError(f"'{field}' must be one of: {', '.join(allowed)}")
    return value


def _upper_one_of(field, value, allowed):
    value = _text(field, value)
    if value is None:
        return None
    return _one_of(field, value.upper(), allowed)


def _int(field, value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise LogError(f"'{field}' must be a whole number (got {value!r})")


def resolve_page(page, page_size):
    """Validate paging, refusing rather than clamping.

    A page size of 5,000 silently served as 200 tells the caller they have
    everything when they have 4% of it. Refusing says so.
    """
    page = _int("page", page)
    page_size = _int("page_size", page_size)
    page = 1 if page is None else page
    page_size = DEFAULT_PAGE_SIZE if page_size is None else page_size
    if page < 1:
        raise LogError("'page' starts at 1")
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise LogError(f"'page_size' must be between 1 and {MAX_PAGE_SIZE}")
    return page, page_size


def resolve_group_limit(limit):
    if limit is None:
        return DEFAULT_GROUP_LIMIT
    value = _int("limit", limit)
    if value < 1 or value > MAX_GROUP_LIMIT:
        raise LogError(f"'limit' must be between 1 and {MAX_GROUP_LIMIT}")
    return value


# --------------------------------------------------------------------------
# LIKE escaping
# --------------------------------------------------------------------------

def escape_like(term: str) -> str:
    """Make every character in a search term literal.

    Without this, `%` typed into a search box matches everything and `_`
    matches any character -- so searching for `PO_1001` would quietly also
    return `PO-1001`, and a lone `%` would return the whole database while
    looking like it had filtered something.

    The backslash goes first: escaping it after `%` and `_` would double-escape
    the backslashes this function just added.
    """
    return (term.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_"))


# --------------------------------------------------------------------------
# the two stream queries
#
# Each SELECT below produces the SAME columns in the SAME order, so they can
# be UNIONed. Columns a stream genuinely has no value for are explicit NULLs
# rather than empty strings -- "this kind of event has no vendor" and "this
# event's vendor is blank" are different facts, and the second one never
# occurs here.
# --------------------------------------------------------------------------

# `source` is read from `documents`, which is where it lives -- `runs` has no
# source column (Phase C put it on the document, since it describes how the
# FILE arrived). A scalar subquery rather than a join: a run could in principle
# hold more than one document row, and a join would then silently duplicate
# every activity row for that run.
_RUN_SOURCE = ("(SELECT d.source FROM documents d WHERE d.run_id = runs.id "
               " ORDER BY d.id ASC LIMIT 1)")

_INVOICE_SELECT = f"""
    SELECT
        'invoice'                  AS stream,
        a.id                       AS event_id,
        a.created_at               AS created_at,
        a.event_type               AS event_type,
        a.actor                    AS actor,
        a.note                     AS note,
        a.run_id                   AS run_id,
        NULL::INTEGER              AS email_id,
        runs.filename              AS filename,
        runs.vendor_name           AS vendor_name,
        runs.invoice_number        AS invoice_number,
        runs.po_number             AS po_number,
        runs.total                 AS total,
        {analytics.AUTOMATED}      AS automated_decision,
        runs.status                AS status,
        runs.final_decision        AS final_decision,
        {_RUN_SOURCE}              AS source,
        NULL::TEXT                 AS email_status,
        NULL::TEXT                 AS classification
    FROM invoice_activity a
    JOIN runs ON runs.id = a.run_id
"""

_EMAIL_SELECT = """
    SELECT
        'email'                    AS stream,
        e.id                       AS event_id,
        e.created_at               AS created_at,
        e.event_type               AS event_type,
        e.actor                    AS actor,
        e.note                     AS note,
        m.run_id                   AS run_id,
        e.email_id                 AS email_id,
        NULL::TEXT                 AS filename,
        NULL::TEXT                 AS vendor_name,
        NULL::TEXT                 AS invoice_number,
        NULL::TEXT                 AS po_number,
        NULL::REAL                 AS total,
        NULL::TEXT                 AS automated_decision,
        NULL::TEXT                 AS status,
        NULL::TEXT                 AS final_decision,
        'EMAIL'                    AS source,
        m.status                   AS email_status,
        m.classification           AS classification
    FROM email_activity e
    JOIN email_messages m ON m.id = e.email_id
"""


def _invoice_where(f: LogFilters, params: list) -> str:
    """WHERE fragment for the invoice stream. Bounds and values are always
    bind parameters; the only interpolation is this module's own column
    names."""
    # Resolved FIRST, and in Python rather than SQL -- see `runs_failing_rule`.
    # A rule nothing failed means this stream contributes nothing, and that has
    # to be settled before any parameter is appended: returning `1=0` after
    # binding the window bounds would leave `params` holding values the SQL no
    # longer has placeholders for.
    matching_runs = None
    if f.rule_failed is not None:
        matching_runs = runs_failing_rule(f.window, f.rule_failed)
        if not matching_runs:
            return " WHERE 1=0"

    sql = " WHERE 1=1"
    sql += f.window.clause("a.created_at", params)

    if f.actor is not None:
        if f.actor == SYSTEM_ACTOR:
            sql += " AND a.actor IS NULL"
        else:
            sql += " AND a.actor = %s"
            params.append(f.actor)
    if f.event is not None:
        sql += " AND a.event_type = %s"
        params.append(f.event)
    if f.vendor is not None:
        sql += " AND runs.vendor_name = %s"
        params.append(f.vendor)
    if f.run_id is not None:
        sql += " AND a.run_id = %s"
        params.append(f.run_id)
    if f.invoice_number is not None:
        sql += " AND runs.invoice_number = %s"
        params.append(f.invoice_number)
    if f.po_number is not None:
        # A multi-PO invoice names one PO in `runs.po_number` and all of them
        # in `run_allocations` (§3). EXISTS rather than a join, so an invoice
        # bound to three POs still contributes each activity row ONCE.
        sql += (" AND (runs.po_number = %s OR EXISTS ("
                "SELECT 1 FROM run_allocations ra "
                "WHERE ra.run_id = runs.id AND ra.po_number = %s))")
        params.extend([f.po_number, f.po_number])
    if f.decision is not None:
        sql += f" AND {analytics.AUTOMATED} = %s"
        params.append(f.decision)
    if f.status is not None:
        sql += " AND runs.status = %s"
        params.append(f.status)
    if f.source is not None:
        sql += f" AND {_RUN_SOURCE} = %s"
        params.append(f.source)
    if matching_runs is not None:
        sql += " AND a.run_id = ANY(%s)"
        params.append(matching_runs)
    if f.search is not None:
        sql += _search_clause(("a.event_type", "a.actor", "runs.vendor_name",
                               "runs.invoice_number", "runs.po_number",
                               "runs.filename", "a.note"), f.search, params)
    return sql


def _email_where(f: LogFilters, params: list) -> str:
    sql = " WHERE 1=1"
    sql += f.window.clause("e.created_at", params)

    if f.actor is not None:
        if f.actor == SYSTEM_ACTOR:
            sql += " AND e.actor IS NULL"
        else:
            sql += " AND e.actor = %s"
            params.append(f.actor)
    if f.event is not None:
        sql += " AND e.event_type = %s"
        params.append(f.event)
    if f.email_status is not None:
        sql += " AND m.status = %s"
        params.append(f.email_status)
    if f.search is not None:
        # The email stream has no vendor/invoice/PO/filename columns, so it
        # searches the fields it actually has. Searching a NULL column would
        # never match and would only slow the scan.
        sql += _search_clause(("e.event_type", "e.actor", "e.note"),
                              f.search, params)
    return sql


def _search_clause(columns, term, params) -> str:
    """`AND (col ILIKE %s ESCAPE '\\' OR ...)`.

    ILIKE for case-insensitivity, because nobody types a vendor name the way
    it was extracted. The TERM is a bind parameter with its metacharacters
    already escaped; the COLUMNS come from this module's own frozen tuples.
    """
    pattern = f"%{escape_like(term)}%"
    parts = []
    for col in columns:
        if not analytics._SAFE_COLUMN.fullmatch(col):
            raise LogError(f"unsafe column reference {col!r}")
        parts.append(f"{col} ILIKE %s ESCAPE '\\'")
        params.append(pattern)
    return " AND (" + " OR ".join(parts) + ")"


def _union(f: LogFilters):
    """The filtered UNION of whichever streams can match, plus its params.

    Returns (sql, params) or (None, None) when no stream can contribute --
    e.g. `stream=email` combined with `vendor=`, which is a coherent request
    with a genuinely empty answer, not an error.
    """
    parts, params = [], []
    if f.wants_invoice():
        p = []
        parts.append(_INVOICE_SELECT + _invoice_where(f, p))
        params.extend(p)
    if f.wants_email():
        p = []
        parts.append(_EMAIL_SELECT + _email_where(f, p))
        params.extend(p)
    if not parts:
        return None, None
    # UNION ALL, never UNION: the two streams cannot produce a duplicate row
    # (different tables, and `stream` differs on every row), so de-duplicating
    # would be a sort over the whole result set to remove nothing.
    return "(" + " UNION ALL ".join(parts) + ")", params


# --------------------------------------------------------------------------
# ordering
#
# STABILITY IS THE REQUIREMENT, not just "newest first". Several events land
# in the same millisecond routinely -- save_run_checked writes
# PROCESSING_COMPLETED and REVIEW_REQUIRED inside one transaction, with the
# same `datetime.now()` string. Ordering on `created_at` alone would let those
# two swap places between page 1 and page 2, which either duplicates a row
# across pages or drops one entirely.
#
# `event_id` breaks the tie inside a stream (SERIAL, so it is also insertion
# order), and `stream` breaks it between them. The triple is unique, so the
# sort is total and OFFSET paging is repeatable.
# --------------------------------------------------------------------------

def _order_by(f: LogFilters) -> str:
    d = "ASC" if f.order == "asc" else "DESC"
    return f" ORDER BY created_at {d}, stream ASC, event_id {d}"


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

def _row(r: dict) -> dict:
    """One log row as the API returns it.

    Note what is absent, and that it is absent DELIBERATELY:

      * no `metadata_json` -- the list is a table, and the structured detail
        belongs on the one event a reader opened (see `detail`);
      * no `audit_json`, `extracted_json` or `raw_text` -- invoice contents
        are not log data;
      * no `storage_key`/`storage_backend` -- the same restriction the Phase C
        document endpoints observe;
      * no email address, domain or subject, anywhere in this module. Phase F
        owns that record and exposes it at `/api/email/messages/{id}`; a log
        line does not need it, and a CSV of it would carry message content out
        of the application.
    """
    return {
        # Stable across pages and unique across both streams -- a React key,
        # and how a client refers to one event without holding two fields.
        "id": f"{r['stream']}:{r['event_id']}",
        "stream": r["stream"],
        "event_id": r["event_id"],
        "timestamp": r["created_at"],
        "event": r["event_type"],
        # NULL actor means the system acted on its own (§6.1). Reported as
        # null rather than as an invented name like "system", which would be
        # indistinguishable from a real user called that.
        "actor": r["actor"],
        "summary": r["note"],
        "run_id": r["run_id"],
        "email_id": r["email_id"],
        "filename": r["filename"],
        "vendor": r["vendor_name"],
        "invoice_number": r["invoice_number"],
        "po_number": r["po_number"],
        "total": r["total"],
        "decision": r["automated_decision"],
        "status": r["status"],
        "final_decision": r["final_decision"],
        "source": r["source"],
        "email_status": r["email_status"],
        "classification": r["classification"],
    }


def search(f: LogFilters, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE) -> dict:
    """One page of log rows, plus enough about the total to draw page controls.

    TWO queries, never one per row: the page itself, and a bounded count. The
    context on each row (vendor, decision, status) comes from the join in the
    SELECT, so a page of 50 rows costs 50 rows of work, not 50 follow-up
    lookups.
    """
    union, params = _union(f)
    if union is None:
        return {"rows": [], "page": page, "page_size": page_size,
                "total": 0, "total_is_exact": True, "has_more": False,
                "filters": f.describe()}

    sql = f"SELECT * FROM {union} AS log{_order_by(f)} LIMIT %s OFFSET %s"
    args = list(params) + [page_size, (page - 1) * page_size]

    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            rows = [_row(dict(r)) for r in cur.fetchall()]
            total, exact = _count(cur, union, params)
    finally:
        conn.close()

    return {
        "rows": rows,
        "page": page,
        "page_size": page_size,
        "total": total,
        # Says whether `total` is the real number or the ceiling was hit, so a
        # client renders "10,000+" instead of claiming a precise figure it was
        # never given.
        "total_is_exact": exact,
        "has_more": (page - 1) * page_size + len(rows) < total,
        "filters": f.describe(),
    }


def _count(cur, union: str, params: list):
    """How many rows match, counted no further than COUNT_CEILING.

    The subquery is LIMITed before the count, so a filter matching millions
    stops after ten thousand instead of scanning to the end to produce a
    number that would only be rendered as "lots".
    """
    cur.execute(
        f"SELECT COUNT(*) AS n FROM (SELECT 1 FROM {union} AS log LIMIT %s) AS capped",
        list(params) + [COUNT_CEILING])
    n = cur.fetchone()["n"]
    return n, n < COUNT_CEILING


def count(f: LogFilters) -> int:
    """The bounded match count on its own. Used by the export endpoint to
    decide whether the caller is about to hit the export cap."""
    union, params = _union(f)
    if union is None:
        return 0
    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            return _count(cur, union, params)[0]
    finally:
        conn.close()


# --------------------------------------------------------------------------
# grouping
# --------------------------------------------------------------------------

def group(f: LogFilters, group_by: str, limit: int = DEFAULT_GROUP_LIMIT,
          viewer: str = None, see_everyone: bool = True) -> dict:
    """The same filtered rows, counted per key instead of listed.

    This is the drill-down Phase H deliberately did not build (§7c.15): the
    analytics screen gives the figure, this gives the rows behind it, and
    grouping sits between the two.

    AUTHORIZATION. Grouping by `actor` is the one axis here that produces a
    PER-PERSON REPORT rather than a view of invoice history, so it follows the
    rule `/api/analytics/users` already set (§7c.5): your own row, unless you
    hold `invoice:admin`. The restriction is applied HERE, from the
    authenticated principal -- `see_everyone` is passed by main.py from
    `principal.has("invoice:admin")` and is never read from a query parameter.
    An `actor=` filter the caller supplies cannot widen it: for a non-admin the
    key is overwritten with the viewer's own name, so asking about somebody
    else returns your own row, not theirs.

    The individual event ROWS are not restricted, and that is not an
    oversight: `GET /api/runs/{id}/activity` has returned every actor's events
    for a run at `invoice:read` since Phase D, so a cross-run list of the same
    rows exposes nothing new. What `invoice:admin` gates is the ranking.
    """
    if group_by not in GROUPINGS:
        raise LogError(
            f"'group_by' must be one of: {', '.join(sorted(GROUPINGS))}")

    scope = "all"
    if group_by == "actor" and not see_everyone:
        scope = "self"
        # Not "and also filter by the viewer" -- REPLACED. Whatever actor the
        # caller asked about, they get their own.
        f = _with_actor(f, viewer)

    expr, label = GROUPINGS[group_by]
    union, params = _union(f)
    if union is None:
        return {"group_by": group_by, "label": label, "groups": [],
                "distinct_keys": 0, "scope": scope, "filters": f.describe()}

    sql = (f"SELECT {expr} AS k, COUNT(*) AS n, "
           f"       MIN(created_at) AS first_at, MAX(created_at) AS last_at "
           f"FROM {union} AS log GROUP BY {expr} "
           # Count first, then the key, so equal counts come back in a stable
           # order instead of whatever the hash aggregate happened to produce.
           f"ORDER BY n DESC, k ASC NULLS LAST LIMIT %s")

    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, list(params) + [limit])
            rows = [dict(r) for r in cur.fetchall()]
            cur.execute(
                f"SELECT COUNT(*) AS n FROM (SELECT DISTINCT {expr} AS k "
                f"FROM {union} AS log LIMIT %s) AS d",
                list(params) + [MAX_GROUP_LIMIT + 1])
            distinct = cur.fetchone()["n"]
    finally:
        conn.close()

    return {
        "group_by": group_by,
        "label": label,
        # `key` is null for the group of events with no value on that axis --
        # system events under `actor`, email events under `vendor`. Reported
        # as null so a client can label it honestly; collapsing it into a
        # bucket called "unknown" would assert something the rows do not say.
        "groups": [{"key": r["k"] if r["k"] is None else str(r["k"]),
                    "count": r["n"], "first_at": r["first_at"],
                    "last_at": r["last_at"]} for r in rows],
        "distinct_keys": distinct,
        "truncated": distinct > len(rows),
        "scope": scope,
        "filters": f.describe(),
    }


def _with_actor(f: LogFilters, actor) -> LogFilters:
    """A copy of these filters pinned to one actor.

    A copy rather than a mutation: the caller's object is also what
    `describe()` reports and what an export re-reads, and quietly rewriting it
    in place would make the echoed filters disagree with what was asked for.
    """
    clone = LogFilters.__new__(LogFilters)
    for name in LogFilters.__slots__:
        setattr(clone, name, getattr(f, name))
    # A viewer with no username can match nothing rather than everything.
    clone.actor = actor or SYSTEM_ACTOR
    return clone


# --------------------------------------------------------------------------
# rule-failed filtering -- the one thing SQL cannot do here
# --------------------------------------------------------------------------

def runs_failing_rule(window: Window, rule: str) -> list:
    """Run ids whose audit trail records `rule` among its FAILED rules.

    WHY THIS IS PYTHON AND NOT SQL. `audit_json` is TEXT, not JSONB, and
    Postgres has no total cast: one malformed blob in the window would abort
    the whole query and take the log page down with it. That is the same
    reasoning `analytics._scan_run_json` records (§7c.2), and this reuses
    `analytics._loads` -- the guarded parse -- rather than adding a second one.

    WHY NOT A LIKE ON THE TEXT. `audit_json` lists every rule that was
    evaluated, passed ones included, so `audit_json LIKE '%PO remaining
    check%'` matches runs where that rule PASSED. It would look like it worked
    and be wrong in the direction that matters.

    The scan is bounded by the window's RUN count (not its event count) and
    only happens when this filter is used.
    """
    conn = storage.get_conn()
    try:
        params = []
        sql = "SELECT id, audit_json FROM runs WHERE 1=1" + \
            window.clause("runs.created_at", params)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()

    wanted = rule.strip().lower()
    out = []
    for r in rows:
        audit = analytics._loads(r["audit_json"], dict)
        if not audit:
            continue
        failed = audit.get("rules_failed")
        if not isinstance(failed, list):
            continue
        if any(isinstance(name, str) and name.strip().lower() == wanted
               for name in failed):
            out.append(r["id"])
    return out


def rule_vocabulary(window: Window, limit: int = MAX_FACET_VALUES) -> list:
    """Every rule name that FAILED at least once in the window.

    Offered as filter options, so the rule filter is chosen from what the data
    actually contains rather than typed from memory. Rule NAMES only (a fixed
    vocabulary from rules.py) -- never the reason sentence, which embeds the
    invoice's own amounts and would make every row its own group (§7c.11).
    """
    conn = storage.get_conn()
    try:
        params = []
        sql = "SELECT audit_json FROM runs WHERE 1=1" + \
            window.clause("runs.created_at", params)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()

    seen = {}
    for r in rows:
        audit = analytics._loads(r["audit_json"], dict)
        if not audit:
            continue
        for name in audit.get("rules_failed") or []:
            if isinstance(name, str) and name.strip():
                seen[name] = seen.get(name, 0) + 1
    ranked = sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"value": name, "count": n} for name, n in ranked[:limit]]


# --------------------------------------------------------------------------
# the per-run stage log
#
# The third history this phase makes queryable, and the one shaped differently
# from the other two. `invoice_activity` and `email_activity` are ROWS, so
# they UNION, filter, order and page in SQL. The per-run stage log is a JSON
# ARRAY on the run itself -- `runs.stages_json`, one entry per pipeline stage
# with its name, status, detail and milliseconds, written once by
# `run_pipeline` and never appended to. It cannot join that union without
# either inventing rows the database does not hold, or repeating every
# activity row once per stage.
#
# So it is its own view over the SAME filters: the same window, the same
# vendor / PO / decision / status / source / rule narrowing, returning ONE ROW
# PER STAGE instead of one row per event. `GET /api/runs/{id}` has always been
# able to show the stage log of ONE run, and Phase H reports per-stage timings
# in AGGREGATE (7c.2); neither can answer "which invoices failed at
# VENDOR_CHECK last week", which is the row-level question this phase exists
# to make askable.
#
# THE COST IS STATED, NOT HIDDEN. Like `runs_failing_rule` above, and for the
# identical reason Phase H records (7c.2), this parses the JSON of every run
# the filters select: the column is TEXT, not JSONB, and one malformed blob
# would abort a query that cast it and take the whole page down. A bad blob is
# skipped, counted, and reported in `data_quality` -- every other run still
# reads normally.
# --------------------------------------------------------------------------

# The stage statuses `run_pipeline` writes. A frozen tuple, so `stage_status`
# is validated against what the pipeline can actually produce rather than
# reaching a comparison as free text.
STAGE_STATUSES = ("ok", "warn", "fail")

_STAGE_SHAPE = re.compile(r"^[A-Za-z0-9_ -]{1,64}$")

# Runs read per round trip while walking the stage log. A run contributes a
# handful of stages, so this bounds how many rows exist at once -- an export
# never holds the whole result in memory, the same property `export_csv` has.
STAGE_RUN_CHUNK = 200

# What a free-text search over the stage view looks at. Frozen, like
# SEARCH_COLUMNS, and note that two of these live inside the JSON: the stage
# name and its detail line are the most useful things to search here, and they
# are matched in PYTHON, on values already parsed. No LIKE pattern is built,
# so the metacharacter question does not arise on this path at all.
STAGE_SEARCH_FIELDS = ("stage", "stage_status", "detail", "filename",
                       "vendor", "invoice_number", "po_number")


def _runs_where(f: LogFilters, params: list) -> str:
    """WHERE fragment selecting the RUNS whose stage logs a stage view covers.

    THE WINDOW APPLIES TO `runs.created_at`, not to an event time. A stage has
    no timestamp of its own -- it is what happened during the run, so the
    run's own arrival time is the only honest thing to window on, and it is
    the column every analytics query already windows on (7c.9).

    Bounds and values are always bind parameters; the only interpolation is
    this module's own SQL constants.
    """
    # Resolved FIRST, and in Python, for the reason `_invoice_where` records:
    # returning `1=0` after binding the window bounds would leave `params`
    # holding values the SQL no longer has placeholders for.
    matching_runs = None
    if f.rule_failed is not None:
        matching_runs = runs_failing_rule(f.window, f.rule_failed)
        if not matching_runs:
            return " WHERE 1=0"

    sql = " WHERE runs.stages_json IS NOT NULL"
    sql += f.window.clause("runs.created_at", params)

    if f.vendor is not None:
        sql += " AND runs.vendor_name = %s"
        params.append(f.vendor)
    if f.run_id is not None:
        sql += " AND runs.id = %s"
        params.append(f.run_id)
    if f.invoice_number is not None:
        sql += " AND runs.invoice_number = %s"
        params.append(f.invoice_number)
    if f.po_number is not None:
        # A multi-PO invoice names one PO in `runs.po_number` and all of them
        # in `run_allocations` (3). EXISTS rather than a join, so a run bound
        # to three POs is still selected ONCE.
        sql += (" AND (runs.po_number = %s OR EXISTS ("
                "SELECT 1 FROM run_allocations ra "
                "WHERE ra.run_id = runs.id AND ra.po_number = %s))")
        params.extend([f.po_number, f.po_number])
    if f.decision is not None:
        sql += f" AND {analytics.AUTOMATED} = %s"
        params.append(f.decision)
    if f.status is not None:
        sql += " AND runs.status = %s"
        params.append(f.status)
    if f.source is not None:
        sql += f" AND {_RUN_SOURCE} = %s"
        params.append(f.source)
    if matching_runs is not None:
        sql += " AND runs.id = ANY(%s)"
        params.append(matching_runs)
    return sql


def _stage_row(run: dict, seq: int, entry: dict) -> dict:
    """One stage as the API returns it, with the invoice it belongs to.

    The same exclusions `_row` observes: no `audit_json`, no extracted fields,
    no storage key. `detail` IS returned -- it is the sentence the pipeline
    wrote about that stage, which `GET /api/runs/{id}` has shown at
    `invoice:read` since the first phase, so a cross-run view of the same text
    widens nothing.
    """
    ms = entry.get("ms")
    stage_status = entry.get("status")
    detail_text = entry.get("detail")
    return {
        # Unique across the view, and stable: a stage log is written once and
        # never reordered, so a run id and a position identify one stage.
        "id": f"{run['id']}:{seq}",
        "run_id": run["id"],
        "seq": seq,
        "stage": entry["name"].strip(),
        # `null` rather than a guessed "ok": a stage entry that recorded no
        # status did not pass, it went unrecorded.
        "stage_status": stage_status if isinstance(stage_status, str)
                        and stage_status else None,
        "detail": detail_text if isinstance(detail_text, str) else None,
        # Unmeasured is null, never 0 -- the distinction Phase H's timing block
        # makes for exactly the same values (7c.10).
        "ms": float(ms) if analytics._is_number(ms) else None,
        # The RUN's timestamp, named as such: it is when the invoice arrived,
        # not when this stage ran, and the pipeline does not record the latter.
        "timestamp": run["created_at"],
        "filename": run["filename"],
        "vendor": run["vendor_name"],
        "invoice_number": run["invoice_number"],
        "po_number": run["po_number"],
        "decision": run["automated_decision"],
        # `run_status` and `stage_status` are both spelled out. One word for
        # both would be read as whichever the reader had in mind.
        "run_status": run["status"],
        "source": run["source"],
    }


def _stage_matches(row: dict, term: str) -> bool:
    """Case-insensitive, LITERAL substring match over the stage row.

    Python rather than SQL because two of the fields live inside the JSON
    column and are not queryable until they are parsed. A pleasant consequence
    is that `%` and `_` are ordinary characters on this path -- there is no
    LIKE pattern for them to be metacharacters in.
    """
    for field in STAGE_SEARCH_FIELDS:
        value = row.get(field)
        if isinstance(value, str) and term in value.lower():
            return True
    return False


def _iter_stage_rows(f: LogFilters, stage, stage_status, term, stats: dict):
    """Yield stage rows, a chunk of RUNS at a time, newest run first.

    A GENERATOR walking chunks rather than one `fetchall`, so the export and
    the list share one implementation of what a stage row IS and what the
    filters MEAN, and neither ever materialises the whole result.
    """
    params = []
    where = _runs_where(f, params)
    direction = "ASC" if f.order == "asc" else "DESC"
    sql = (f"SELECT runs.id, runs.created_at, runs.filename, runs.vendor_name, "
           f"runs.invoice_number, runs.po_number, runs.stages_json, "
           f"{analytics.AUTOMATED} AS automated_decision, runs.status, "
           f"{_RUN_SOURCE} AS source "
           f"FROM runs{where} "
           f"ORDER BY runs.created_at {direction}, runs.id {direction} "
           f"LIMIT %s OFFSET %s")

    conn = storage.get_conn()
    try:
        offset = 0
        while True:
            with conn.cursor() as cur:
                cur.execute(sql, list(params) + [STAGE_RUN_CHUNK, offset])
                fetched = cur.fetchall()
            if not fetched:
                return
            for raw in fetched:
                run = dict(raw)
                parsed = analytics._loads(run["stages_json"], list)
                if parsed is None:
                    if run["stages_json"]:
                        stats["malformed_stages"] = stats.get(
                            "malformed_stages", 0) + 1
                    continue
                # The stage log is ALWAYS read in the order the pipeline wrote
                # it, whichever direction the RUNS are ordered in. A run read
                # backwards would show DECISION before INGEST, which is not a
                # sort order, it is a false account of what happened.
                for seq, entry in enumerate(parsed, start=1):
                    if not isinstance(entry, dict):
                        continue
                    name = entry.get("name")
                    if not isinstance(name, str) or not name.strip():
                        continue
                    row = _stage_row(run, seq, entry)
                    if stage and row["stage"].lower() != stage:
                        continue
                    if stage_status and (row["stage_status"] or "").lower() != stage_status:
                        continue
                    if term and not _stage_matches(row, term):
                        continue
                    yield row
            offset += len(fetched)
            if len(fetched) < STAGE_RUN_CHUNK:
                return
    finally:
        conn.close()


def _resolve_stage_args(f: LogFilters, stage, stage_status):
    """Validate the two filters only the stage view has, and refuse the ones
    it cannot answer."""
    clashes = f.stage_conflicts()
    if clashes:
        raise LogError(
            "the stage log records what the pipeline did, so it has no "
            "actor, event type or message status -- remove "
            f"{', '.join(clashes)} to query it")
    stage = _shaped("stage", stage, _STAGE_SHAPE,
                    "a stage name like VENDOR_CHECK")
    # Blank is no filter at all, the reading `_text` gives every other value
    # here -- an empty query parameter is a control the user cleared, not a
    # value they chose.
    stage_status = _text("stage_status", stage_status, limit=16)
    if stage_status is not None:
        stage_status = _one_of("stage_status", stage_status.lower(),
                               STAGE_STATUSES)
    return (stage.lower() if stage else None, stage_status)


def stage_rows(f: LogFilters, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE,
               stage: str = None, stage_status: str = None) -> dict:
    """One page of the per-stage log, with the invoice each stage belongs to.

    Paged in Python, which is the honest consequence of the column being JSON:
    a database that cannot filter the array also cannot LIMIT it. The walk is
    bounded by COUNT_CEILING, exactly as the SQL count is, so the ceiling means
    the same thing on both views and neither can be asked for an unbounded
    amount of work.
    """
    stage, stage_status = _resolve_stage_args(f, stage, stage_status)
    term = f.search.lower() if f.search else None

    stats = {"malformed_stages": 0}
    start = (page - 1) * page_size
    rows, total, capped = [], 0, False
    for row in _iter_stage_rows(f, stage, stage_status, term, stats):
        if total >= COUNT_CEILING:
            capped = True
            break
        if start <= total < start + page_size:
            rows.append(row)
        total += 1

    described = f.describe()
    if stage:
        described["stage"] = stage.upper()
    if stage_status:
        described["stage_status"] = stage_status

    return {
        "rows": rows,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_is_exact": not capped,
        "has_more": capped or (start + len(rows) < total),
        # Reported the way Phase H reports it: keyed by what the value MEANS,
        # never by the column it lives in (7c.6).
        "data_quality": stats,
        "filters": described,
    }


def stage_vocabulary(window: Window, limit: int = MAX_FACET_VALUES) -> dict:
    """Which stages ran in the window, and which statuses they produced.

    Offered as filter options for the same reason `rule_vocabulary` is: a
    stage filter chosen from what the data contains beats one typed from
    memory. Windowed, also for the same reason -- an all-time scan of every
    run's JSON is a cost a filter panel should not pay on every load.
    """
    conn = storage.get_conn()
    try:
        params = []
        sql = ("SELECT runs.stages_json FROM runs "
               "WHERE runs.stages_json IS NOT NULL"
               + window.clause("runs.created_at", params))
        with conn.cursor() as cur:
            cur.execute(sql, params)
            fetched = cur.fetchall()
    finally:
        conn.close()

    names, statuses = {}, {}
    for raw in fetched:
        parsed = analytics._loads(raw["stages_json"], list)
        if parsed is None:
            continue
        for entry in parsed:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if isinstance(name, str) and name.strip():
                names[name.strip()] = names.get(name.strip(), 0) + 1
            st = entry.get("status")
            if isinstance(st, str) and st.strip():
                statuses[st.strip()] = statuses.get(st.strip(), 0) + 1
    ranked = sorted(names.items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        "stages": [{"value": n, "count": c} for n, c in ranked[:limit]],
        "stage_statuses": [{"value": s, "count": c} for s, c in
                           sorted(statuses.items(),
                                  key=lambda kv: (-kv[1], kv[0]))],
    }


# --------------------------------------------------------------------------
# detail
# --------------------------------------------------------------------------

def detail(stream: str, event_id: int) -> dict:
    """One event, with its structured metadata and its subject's context.

    STRUCTURED, NOT RAW. `metadata_json` is parsed and returned as an object
    (guarded by `analytics._loads`, so a malformed blob reads as absent rather
    than raising); the run's FAILED RULE NAMES are listed; the raw `audit_json`
    blob, the extracted fields and the document's storage key are not
    returned at all. A detail panel exists to explain one event, not to be a
    hole through which the rest of the record leaks.
    """
    stream = _one_of("stream", (stream or "").strip().lower(),
                     ("invoice", "email"))
    event_id = _int("event_id", event_id)
    if event_id is None or event_id < 1:
        raise LogError("'event_id' must be a positive whole number")

    select = _INVOICE_SELECT if stream == "invoice" else _EMAIL_SELECT
    alias, meta_table = ("a", "invoice_activity") if stream == "invoice" \
        else ("e", "email_activity")

    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM ({select} WHERE {alias}.id = %s) AS log",
                        (event_id,))
            row = cur.fetchone()
            if row is None:
                return None
            base = _row(dict(row))
            cur.execute(f"SELECT metadata_json FROM {meta_table} WHERE id = %s",
                        (event_id,))
            meta_row = cur.fetchone()
            base["metadata"] = analytics._loads(
                meta_row["metadata_json"] if meta_row else None, dict)

            if base["run_id"] is not None:
                cur.execute("SELECT audit_json, reasons_json, human_decision, "
                            "reviewed_by, reviewed_at FROM runs WHERE id = %s",
                            (base["run_id"],))
                run = cur.fetchone()
                base["run"] = _run_context(dict(run)) if run else None
            if base["email_id"] is not None:
                cur.execute("SELECT classification, status, ingest_status, "
                            "attachment_count, has_pdf_attachment, trusted_sender "
                            "FROM email_messages WHERE id = %s", (base["email_id"],))
                msg = cur.fetchone()
                # Counts and verdicts only. No address, no domain, no subject
                # -- Phase F's own endpoint owns those.
                base["message"] = dict(msg) if msg else None
    finally:
        conn.close()
    return base


def _run_context(run: dict) -> dict:
    """The decision context for the invoice an event belongs to.

    The rules that FAILED, by name, plus the reason sentences the reviewer was
    shown. Not the audit blob: `rules_failed` and `reason` are what a person
    reading a log line needs, and the rest of that structure is a different
    screen's job (`GET /api/runs/{id}`, which this caller can already reach).
    """
    audit = analytics._loads(run.get("audit_json"), dict) or {}
    reasons = analytics._loads(run.get("reasons_json"), list) or []
    failed = [r for r in (audit.get("rules_failed") or []) if isinstance(r, str)]
    return {
        "rules_failed": failed,
        "reason": audit.get("reason"),
        "reasons": [r.get("text") for r in reasons
                    if isinstance(r, dict) and r.get("text")],
        "human_decision": run.get("human_decision"),
        "reviewed_by": run.get("reviewed_by"),
        "reviewed_at": run.get("reviewed_at"),
    }


# --------------------------------------------------------------------------
# facets -- what a filter panel can offer
# --------------------------------------------------------------------------

def facets(window: Window) -> dict:
    """The distinct values worth offering as filter options.

    ALL-TIME, deliberately not narrowed by the window, with one exception. A
    dropdown that empties as you narrow the date range is a filter panel that
    fights the user: you pick "Today", the vendor you were about to select
    disappears, and the control you would use to widen the range again is the
    one you are already looking at. The rule filter IS windowed, because it is
    derived by scanning `audit_json` (see `rule_vocabulary`) and an all-time
    scan of every run is a cost the panel should not pay on every load.

    Capped at MAX_FACET_VALUES, ordered by frequency then name, so the values
    a user is most likely to want are the ones that survive the cap.
    """
    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            actors = _facet(cur, """
                SELECT actor AS k, COUNT(*) AS n FROM (
                    SELECT actor FROM invoice_activity
                    UNION ALL SELECT actor FROM email_activity
                ) AS a WHERE actor IS NOT NULL GROUP BY actor""")
            events = _facet(cur, """
                SELECT event_type AS k, COUNT(*) AS n FROM (
                    SELECT event_type FROM invoice_activity
                    UNION ALL SELECT event_type FROM email_activity
                ) AS e GROUP BY event_type""")
            vendors = _facet(cur, """
                SELECT vendor_name AS k, COUNT(*) AS n FROM runs
                WHERE vendor_name IS NOT NULL AND vendor_name <> ''
                GROUP BY vendor_name""")
            pos = _facet(cur, """
                SELECT po_number AS k, COUNT(*) AS n FROM run_allocations
                WHERE po_number IS NOT NULL GROUP BY po_number""")
            # Whether any system-generated events exist at all, so the panel
            # only offers the "System" option when it would match something.
            cur.execute("""SELECT EXISTS (
                SELECT 1 FROM invoice_activity WHERE actor IS NULL
                UNION ALL SELECT 1 FROM email_activity WHERE actor IS NULL
            ) AS present""")
            has_system = bool(cur.fetchone()["present"])
    finally:
        conn.close()

    return {
        "actors": actors,
        "system_actor": SYSTEM_ACTOR if has_system else None,
        "events": events,
        "vendors": vendors,
        "purchase_orders": pos,
        "rules_failed": rule_vocabulary(window),
        # Windowed for the same reason `rules_failed` is: both are derived by
        # scanning a JSON column, and an all-time scan is a cost a filter
        # panel should not pay on every load.
        **stage_vocabulary(window),
        "decisions": list(analytics.DECISIONS),
        "statuses": list(analytics.DECISIONS),
        "sources": ["MANUAL_UPLOAD", "EMAIL"],
        "email_statuses": ["ADMITTED", "QUARANTINED", "RELEASED", "DISCARDED"],
        "streams": list(STREAMS),
        "groupings": [{"value": k, "label": v[1]} for k, v in
                      sorted(GROUPINGS.items(), key=lambda kv: kv[1][1])],
        "range": window.as_dict(),
    }


def _facet(cur, sql: str) -> list:
    cur.execute(sql + " ORDER BY n DESC, k ASC LIMIT %s", (MAX_FACET_VALUES,))
    return [{"value": r["k"], "count": r["n"]} for r in cur.fetchall()]


# --------------------------------------------------------------------------
# CSV export
# --------------------------------------------------------------------------

# The exported columns, and their headers. One table, so the header row and
# the value row cannot drift.
#
# Chosen from what a person reconciling an invoice actually needs. Everything
# `_row` already excludes stays excluded, and nothing here is a credential, a
# token, a storage key, a file path, an email address or a message subject --
# a CSV leaves the application, and the authorization boundary with it.
EXPORT_COLUMNS = (
    ("timestamp", "Timestamp (UTC)"),
    ("stream", "Stream"),
    ("event", "Event"),
    ("actor", "Actor"),
    ("run_id", "Run"),
    ("invoice_number", "Invoice"),
    ("vendor", "Vendor"),
    ("po_number", "PO"),
    ("total", "Total"),
    ("decision", "Automated decision"),
    ("status", "Status"),
    ("final_decision", "Final decision"),
    ("source", "Source"),
    ("email_id", "Message"),
    ("email_status", "Message status"),
    ("summary", "Summary"),
)

# The characters a spreadsheet treats as the start of a formula. A cell
# beginning with one of these is executed on open by Excel and by Sheets, so a
# vendor name or a review note typed as `=HYPERLINK(...)` becomes live content
# in whoever opens the file.
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")

# A plain number. Checked FIRST, because the naive fix for the above breaks
# ordinary data: `-1250.00` starts with `-`, and prefixing it turns a negative
# amount into the text `'-1250.00`, which no longer sums. A number cannot
# carry a formula, so it is left exactly as it is.
_PLAIN_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")


def csv_safe(value) -> str:
    """One cell, neutralised against spreadsheet formula injection.

    Prefixes a single quote, which every major spreadsheet reads as "the rest
    of this cell is text" and strips on display -- so the value a reader sees
    is unchanged while the formula never runs. Applied only to values that
    could BE a formula: numbers and ordinary text pass through untouched.
    """
    if value is None:
        return ""
    text = str(value)
    if not text:
        return ""
    if _PLAIN_NUMBER.fullmatch(text):
        return text
    if text[0] in _FORMULA_LEAD:
        return "'" + text
    return text


def export_filename(prefix: str = "activity-log") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    return f"{prefix}-{stamp}.csv"


def export_csv(f: LogFilters, max_rows: int = MAX_EXPORT_ROWS):
    """Stream the filtered log as CSV, a chunk of rows at a time.

    A GENERATOR, not a string. The alternative -- build the whole file, then
    send it -- holds the entire export in this process's memory while the
    client downloads it, which is the failure mode that arrives exactly when
    someone finally exports something big.

    IT USES THE SAME `_union` THE LIST ENDPOINT USES. That is the security
    property: there is no second WHERE clause here that could drift from the
    one the user was shown, so "the export respects the filters" is true by
    construction rather than by two implementations agreeing. The same holds
    for authorization -- main.py builds one LogFilters for both endpoints
    behind the same scope.

    Rows past `max_rows` are not silently dropped: the last line of the file
    says the export was truncated and by what cap.
    """
    union, params = _union(f)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")

    def flush():
        out = buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
        return out

    writer.writerow([h for _, h in EXPORT_COLUMNS])
    yield flush()

    if union is None:
        return

    sql = f"SELECT * FROM {union} AS log{_order_by(f)} LIMIT %s OFFSET %s"
    sent, offset, more_beyond_chunk = 0, 0, False
    conn = storage.get_conn()
    try:
        while sent < max_rows:
            take = min(EXPORT_CHUNK, max_rows - sent)
            with conn.cursor() as cur:
                # One row past the chunk, purely to learn whether more exist.
                # It is discarded, never written.
                cur.execute(sql, list(params) + [take + 1, offset])
                rows = cur.fetchall()
            if not rows:
                break
            more_beyond_chunk = len(rows) > take
            if more_beyond_chunk:
                rows = rows[:take]
            for raw in rows:
                row = _row(dict(raw))
                writer.writerow([csv_safe(row.get(key)) for key, _ in EXPORT_COLUMNS])
            sent += len(rows)
            offset += len(rows)
            yield flush()
            if len(rows) < take:
                break
    finally:
        conn.close()

    # Only when the CAP is what stopped it. `more_beyond_chunk` is true on any
    # full chunk that had a successor, which is the normal case mid-export --
    # announcing truncation there would put a false warning at the end of a
    # complete file.
    if sent >= max_rows and more_beyond_chunk:
        writer.writerow([f"# truncated at {max_rows} rows; narrow the filters "
                         f"to export the rest"])
        yield flush()


# The exported stage columns. A second tuple rather than a reshaped first one:
# a stage row and an activity row genuinely have different columns, and one
# table pretending to cover both would carry an empty half on every line.
STAGE_EXPORT_COLUMNS = (
    ("timestamp", "Run timestamp (UTC)"),
    ("run_id", "Run"),
    ("invoice_number", "Invoice"),
    ("vendor", "Vendor"),
    ("po_number", "PO"),
    ("seq", "Step"),
    ("stage", "Stage"),
    ("stage_status", "Stage status"),
    ("ms", "Milliseconds"),
    ("decision", "Automated decision"),
    ("run_status", "Status"),
    ("source", "Source"),
    ("detail", "Detail"),
)


def export_stages_csv(f: LogFilters, stage: str = None, stage_status: str = None,
                      max_rows: int = MAX_EXPORT_ROWS):
    """Stream the filtered per-stage log as CSV.

    THE SAME GENERATOR THE LIST VIEW WALKS (`_iter_stage_rows`), so the export
    cannot show a row the list would not -- the property `export_csv` has for
    activity, established the same way: by there being only one implementation
    of the filter, not by two agreeing.

    Every cell goes through `csv_safe`, and the stage `detail` line is the
    reason that matters most here: it embeds a filename the uploader chose, so
    a PDF named `=cmd|...` must arrive as text.
    """
    stage, stage_status = _resolve_stage_args(f, stage, stage_status)
    term = f.search.lower() if f.search else None

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")

    def flush():
        out = buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
        return out

    writer.writerow([h for _, h in STAGE_EXPORT_COLUMNS])
    yield flush()

    sent, truncated = 0, False
    stats = {"malformed_stages": 0}
    for row in _iter_stage_rows(f, stage, stage_status, term, stats):
        if sent >= max_rows:
            truncated = True
            break
        writer.writerow([csv_safe(row.get(key)) for key, _ in STAGE_EXPORT_COLUMNS])
        sent += 1
        if sent % EXPORT_CHUNK == 0:
            yield flush()

    # Whatever the last partial chunk holds, plus -- only if the CAP is what
    # stopped it -- a line saying so. A complete file never carries the
    # warning, so its presence means exactly one thing.
    if truncated:
        writer.writerow([f"# truncated at {max_rows} rows; narrow the filters "
                         f"to export the rest"])
    yield flush()
