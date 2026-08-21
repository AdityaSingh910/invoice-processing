"""KPIs and analytics, derived at read time from the rows the app already keeps.

WHY THERE IS NO ANALYTICS TABLE IN HERE

Everything below is a query. Nothing in this module writes, and no counter,
rollup or summary row is stored anywhere. That is the same choice the PO
ledger made (a balance is a SUM over `run_allocations`, never a stored
`consumed` column) and the same choice the review claim made (the holder is
derived from `review_claims`, never a `runs.current_reviewer` column), for the
same reason: a counter is authoritative, so the moment one code path forgets
to bump it the number is wrong and nothing notices. A derived figure cannot
drift from the rows it is derived from, because it IS the rows.

If a query here becomes slow, the fix is an index or a better query -- see
`init_db` in storage.py, which grew four indexes for exactly this module --
not a second copy of the data.

--------------------------------------------------------------------------
WHERE THE NUMBERS COME FROM
--------------------------------------------------------------------------

Two mechanisms, chosen per metric rather than mixed arbitrarily:

1. **SQL aggregation** for anything expressible in real columns -- counts,
   rates, date buckets, latencies, per-vendor and per-PO groupings. The
   database does the work and only the answer crosses the wire.

2. **One Python pass over `runs`** (`_scan_run_json`) for the metrics that
   live inside the JSON columns: per-stage timings (`stages_json`), which
   rules failed (`audit_json`), the extraction route, and invoice value by
   currency. This is deliberate, not laziness:

   * `stages_json` and `audit_json` are TEXT, not JSONB, and Postgres has no
     total `try_cast` to jsonb. A single malformed blob -- from a partial
     write, a hand-edited row, an older schema -- would abort the whole
     aggregate query and take a working dashboard down with it. A Python
     pass skips that one row and reports the rest, which is what an
     operations screen has to do.
   * It is ONE query serving several breakdowns, so the row-scan is paid once
     per request, not once per metric.

   The cost is that this pass reads the JSON of every run in the window. At
   this application's volume that is the right trade; at a much larger one the
   answer is a JSONB column with a GIN index, and that is a self-contained
   change to this one function. Stated here rather than discovered later.

--------------------------------------------------------------------------
TIME: EVERYTHING IS UTC
--------------------------------------------------------------------------

Every timestamp this application writes is `datetime.now(timezone.utc)
.isoformat()` -- ISO-8601, UTC, with an explicit `+00:00` offset, stored as
TEXT. Because every writer uses that one call the strings are directly
comparable with `>=` / `<`, which is already how `get_active_claim` tests a
lease expiry. Date windows here are therefore half-open ISO string ranges
`[start, end)`, which an index on the column serves directly.

Day buckets are **UTC calendar days**, matching how `quota.py` already reckons
the extraction budget. Responses say so (`"timezone": "UTC"`), so a dashboard
labels its axis honestly instead of quietly implying local midnight.

--------------------------------------------------------------------------
WHAT THE HEADLINE KPIs ACTUALLY MEAN
--------------------------------------------------------------------------

Metric names are cheap and easy to misread, so each one below is defined
against columns, ships its own numerator and denominator in the response, and
carries its definition string to the client. A rate whose denominator is zero
is `null`, never `0.0` and never `100%`.

**AUTOMATION RATE** -- how much work the rules disposed of unaided.

    numerator    runs whose automated_decision is APPROVED or REJECTED
    denominator  every run that entered in the window

  A REJECTED run counts as automated: correctly stopping a duplicate is the
  process working, not failing. Runs held for a person are the complement.
  Computed from `automated_decision`, which is immutable -- a later human
  ruling or admin override cannot retroactively change how automated the
  process was at the time.

**PROCESSING SUCCESS RATE** -- how often the pipeline could actually read the
document it was given.

    numerator    runs whose extraction produced a usable route
    denominator  every run that entered in the window

  This is a MACHINERY metric, not a business one. It says nothing about
  whether the invoice was approved: a correctly rejected duplicate is a
  processing success, and an unreadable scan held for a human is a processing
  failure even though the hold was the right response to it. The two are kept
  apart on purpose, because a "success rate" that collapses them is exactly
  the misleading number this docstring exists to prevent.

**TASK SUCCESS RATIO** -- of the work that entered, how much reached a final
outcome through the designed path.

    numerator    runs that are RESOLVED and were not OVERRIDDEN
    denominator  every run that entered in the window

    resolved     the run is waiting on nobody: its automated decision was
                 terminal (APPROVED/REJECTED), or a person has ruled on it
    overridden   an administrator changed its status outside the review path
                 (a STATUS_OVERRIDDEN event in invoice_activity)

  Distinct from automation rate, and the distinction is the point. A held
  invoice a reviewer then accepted is NOT automated, but it IS a task success
  -- the process invited a person, a person came, the work finished. An
  administrator reaching past the process to correct a decision is not, even
  though the run ends up terminal either way.

  **This measures operational success, not correctness.** The database holds
  no independent record of what the right answer was -- no ground-truth label,
  no downstream payment confirmation -- so nothing here can or does claim a
  decision was correct. It claims the work finished, by the route it was meant
  to finish by.

**HUMAN REVIEW RATE** -- runs the rules held (automated_decision =
NEEDS_REVIEW) over all runs. The exact complement of the automation rate.

**REVIEW EFFECTIVENESS** -- what people did with what they were handed: of
held runs that have been ruled on, the share accepted versus rejected, plus
the full automated-decision-to-human-decision transition matrix.

  A hold a reviewer ACCEPTED means the reviewer judged the invoice fine after
  all; a hold they REJECTED means they judged the concern real. Neither is
  reported as the hold having been "right" or "wrong" -- that would need a
  ground truth this database does not have. The numbers say what was decided,
  and the field names say only that.
"""
import json
import re
import statistics
from datetime import datetime, timedelta, timezone

import config
import quota
import storage

# Half-open windows, named the way an operator asks for them. `all` is
# unbounded; `custom` reads `from`/`to`.
RANGE_KEYS = ("today", "7d", "30d", "month", "all", "custom")

RANGE_LABELS = {
    "today": "Today (UTC)",
    "7d": "Last 7 days",
    "30d": "Last 30 days",
    "month": "This month",
    "all": "All time",
    "custom": "Custom range",
}

# A trend series is one row per bucket; a caller asking for a decade of daily
# buckets is asking for a 3,650-row response nobody renders. Refused rather
# than silently truncated, so the client knows it did not get what it asked
# for.
MAX_TREND_BUCKETS = 400

# How many rows a grouped breakdown returns before it is cut off. A per-vendor
# table is a ranking, not an export -- Phase I owns exports.
DEFAULT_GROUP_LIMIT = 25
MAX_GROUP_LIMIT = 200


class AnalyticsError(ValueError):
    """A caller-supplied parameter was invalid. main.py maps this to a 400."""


# A bare `column` or `table.column` identifier. See Window.clause() for what
# this is guarding and why a column cannot simply be a bind parameter.
_SAFE_COLUMN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?")


# --------------------------------------------------------------------------
# time windows
# --------------------------------------------------------------------------

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _day_start(d: datetime) -> datetime:
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def _parse_date(value: str, field: str) -> datetime:
    """A YYYY-MM-DD calendar date, read as UTC midnight.

    Deliberately date-only. Accepting a full timestamp would invite a caller to
    supply one in their own offset, and the point of the TIME section above is
    that exactly one convention is ever in play.
    """
    try:
        parsed = datetime.strptime((value or "").strip(), "%Y-%m-%d")
    except (AttributeError, TypeError, ValueError):
        raise AnalyticsError(
            f"'{field}' must be a calendar date in YYYY-MM-DD form (got {value!r})")
    return parsed.replace(tzinfo=timezone.utc)


class Window:
    """A resolved, half-open `[start, end)` range in ISO-8601 UTC strings.

    `start`/`end` are None for the unbounded `all` range, and every query below
    treats None as "no bound on that side" rather than substituting a sentinel
    date -- an epoch-shaped placeholder would quietly exclude any row written
    before it.
    """

    __slots__ = ("key", "start", "end", "label", "start_date", "end_date")

    def __init__(self, key, start, end, label):
        self.key = key
        self.start = start.isoformat() if start else None
        self.end = end.isoformat() if end else None
        self.start_date = start
        self.end_date = end
        self.label = label

    def clause(self, column: str, params: list) -> str:
        """`AND <column> >= %s AND <column> < %s`, appending to `params`.

        Returns an empty string for an unbounded window, so a caller can
        interpolate it into a WHERE clause unconditionally.

        THE BOUNDS ARE ALWAYS PARAMETERS, never interpolated -- they are the
        only part of this that a caller can influence. The COLUMN is
        interpolated, because a column name cannot be a bind parameter in SQL,
        and every call site in this module passes a hard-coded literal. That
        convention is enforced rather than merely stated: a name that is not a
        plain `table.column` identifier raises here, so a future edit that
        threads a request value into this argument fails loudly instead of
        becoming an injection point.
        """
        if not _SAFE_COLUMN.fullmatch(column):
            raise AnalyticsError(f"unsafe column reference {column!r}")
        sql = ""
        if self.start:
            sql += f" AND {column} >= %s"
            params.append(self.start)
        if self.end:
            sql += f" AND {column} < %s"
            params.append(self.end)
        return sql

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "from": self.start,
            "to": self.end,
            # Named explicitly so a dashboard can label its axis truthfully
            # rather than leaving the reader to assume local midnight.
            "timezone": "UTC",
        }

    def bucket_days(self) -> int:
        """How many UTC day buckets this window spans; 0 when unbounded."""
        if not (self.start_date and self.end_date):
            return 0
        return max(0, (self.end_date - self.start_date).days)


def resolve_window(range_key: str = None, date_from: str = None,
                   date_to: str = None) -> Window:
    """Turn caller-supplied range parameters into a validated Window.

    Anything unrecognised raises rather than falling back to a default: a typo
    in `range` silently becoming "all time" would put a number on screen that
    answers a question nobody asked.
    """
    key = (range_key or "30d").strip().lower()

    # A caller who supplies explicit dates means `custom`, whether or not they
    # also said so. Silently ignoring the dates would be the worse failure.
    if (date_from or date_to) and key != "custom":
        key = "custom"

    if key not in RANGE_KEYS:
        raise AnalyticsError(
            f"unknown range '{range_key}'. Valid ranges: {', '.join(RANGE_KEYS)}")

    now = _utc_now()
    today = _day_start(now)
    # The window ends at the START of tomorrow, so "today" includes everything
    # written so far today. Half-open throughout: `< end`, never `<= end`.
    tomorrow = today + timedelta(days=1)

    if key == "all":
        return Window("all", None, None, RANGE_LABELS["all"])
    if key == "today":
        return Window("today", today, tomorrow, RANGE_LABELS["today"])
    if key == "7d":
        return Window("7d", today - timedelta(days=6), tomorrow, RANGE_LABELS["7d"])
    if key == "30d":
        return Window("30d", today - timedelta(days=29), tomorrow, RANGE_LABELS["30d"])
    if key == "month":
        return Window("month", today.replace(day=1), tomorrow, RANGE_LABELS["month"])

    # custom
    if not date_from or not date_to:
        raise AnalyticsError("a custom range needs both 'from' and 'to' (YYYY-MM-DD)")
    start = _parse_date(date_from, "from")
    end_day = _parse_date(date_to, "to")
    if end_day < start:
        raise AnalyticsError("'from' must not be later than 'to'")
    # `to` names a day the caller expects to be INCLUDED, so the half-open end
    # is the start of the day after it. from == to is therefore one full day,
    # not an empty window.
    return Window("custom", start, end_day + timedelta(days=1),
                  f"{date_from} to {date_to}")


def resolve_limit(limit, default=DEFAULT_GROUP_LIMIT) -> int:
    if limit is None:
        return default
    try:
        value = int(limit)
    except (TypeError, ValueError):
        raise AnalyticsError(f"'limit' must be a whole number (got {limit!r})")
    if value < 1 or value > MAX_GROUP_LIMIT:
        raise AnalyticsError(f"'limit' must be between 1 and {MAX_GROUP_LIMIT}")
    return value


# --------------------------------------------------------------------------
# small shared helpers
# --------------------------------------------------------------------------

def _rate(numerator, denominator):
    """A ratio, or None when it is undefined.

    NEVER 0.0 for an empty denominator. "No invoices were processed" and "no
    invoices were automated" are different facts, and a dashboard that renders
    the first as 0% is stating something false about an idle day.
    """
    if not denominator:
        return None
    return numerator / denominator


def _kpi(numerator, denominator, definition):
    """One KPI, shipped with the arithmetic behind it.

    The counts travel with the rate on purpose: a share with nothing under it
    cannot be checked, and "89%" over 9 runs deserves to be read differently
    from "89%" over 9,000. Clients use `denominator` to decide whether to
    render a figure at all.
    """
    return {
        "value": _rate(numerator, denominator),
        "numerator": numerator,
        "denominator": denominator,
        "definition": definition,
    }


def _stats(values):
    """Descriptive statistics for a sample, or explicit nulls for an empty one.

    `samples` is always present and always honest, so a caller can tell an
    average of zero (every measurement really was zero) from no average at all
    (nothing was measured).
    """
    clean = [v for v in values if v is not None]
    if not clean:
        return {"samples": 0, "average": None, "median": None,
                "p95": None, "min": None, "max": None}
    ordered = sorted(clean)
    # Nearest-rank p95, not an interpolated one: with a handful of samples an
    # interpolated percentile invents a value that never occurred.
    idx = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return {
        "samples": len(clean),
        "average": statistics.fmean(clean),
        "median": statistics.median(clean),
        "p95": ordered[idx],
        "min": ordered[0],
        "max": ordered[-1],
    }


def _counts(rows, key="k", value="n"):
    return {r[key]: r[value] for r in rows}


def _fill(counts: dict, vocabulary) -> dict:
    """Every known key present, zero where nothing occurred.

    A decision that did not happen in this window is a real zero and should
    render as one. A key OUTSIDE the vocabulary is kept as it was found rather
    than dropped, so a value this code does not recognise still shows up
    instead of silently vanishing.
    """
    out = {k: 0 for k in vocabulary}
    out.update(counts)
    return out


# The automated decision is the immutable one, but `save_run` (the
# unreadable-document path, main.py's _abort_unreadable) writes no
# `automated_decision` at all -- init_db backfills it from `status` on the next
# startup, which means a freshly written run can legitimately hold NULL there
# in the meantime. COALESCE is exactly what record_human_review already does
# for the same reason, so the same expression is used throughout this module.
AUTOMATED = "COALESCE(runs.automated_decision, runs.status)"

DECISIONS = ("APPROVED", "NEEDS_REVIEW", "REJECTED")
HUMAN_DECISIONS = ("ACCEPTED", "REJECTED")
FINAL_DECISIONS = ("APPROVED", "NEEDS_REVIEW", "REJECTED",
                   "HUMAN_APPROVED", "HUMAN_REJECTED")

# An administrator changing a status outside the review path. The event name
# is the one main.py already logs; it is the only signal in the database that
# the designed path was stepped around.
OVERRIDE_EVENT = "STATUS_OVERRIDDEN"


# --------------------------------------------------------------------------
# the one JSON pass
# --------------------------------------------------------------------------

def _loads(raw, expected):
    """Parse a JSON column, or None if it is absent, malformed, or the wrong
    shape. Never raises -- see `_scan_run_json` for why that matters here."""
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, expected) else None


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _scan_run_json(window: Window) -> dict:
    """Read the JSON columns of every run in the window, once.

    Serves several breakdowns that all need the same rows -- per-stage timings,
    which rules failed, which extraction route ran, and invoice value by
    currency -- so together they cost one query rather than one each.

    EVERY per-row parse is guarded. A run whose `stages_json`, `audit_json` or
    `extracted_json` cannot be parsed contributes nothing to the metrics that
    need it and is counted in `malformed`, while every other run in the window
    still reports normally. A dashboard that goes blank because one row is
    corrupt is worse than one that says "999 of 1,000 runs".
    """
    params = []
    sql = ("SELECT runs.id, runs.stages_json, runs.audit_json, runs.extracted_json, "
           f"runs.total, {AUTOMATED} AS automated FROM runs WHERE 1=1")
    sql += window.clause("runs.created_at", params)

    run_ms = []
    stage_ms = {}
    stage_status = {}
    routes = {}
    providers = {}
    failed_rules = {}
    value_by_currency = {}
    extraction_failures = 0
    # Keyed by what the value MEANS, not by the column it lives in: an API
    # response is not the place to publish this database's column names.
    malformed = {"stages": 0, "audit": 0, "extracted": 0}
    scanned = 0

    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for row in cur.fetchall():
                scanned += 1

                # -- stage timings ------------------------------------------
                stages = _loads(row["stages_json"], list)
                if stages is None:
                    if row["stages_json"]:
                        malformed["stages"] += 1
                else:
                    total_ms = 0.0
                    timed = False
                    for s in stages:
                        if not isinstance(s, dict):
                            continue
                        name, ms, st = s.get("name"), s.get("ms"), s.get("status")
                        if name and _is_number(ms):
                            stage_ms.setdefault(name, []).append(float(ms))
                            total_ms += float(ms)
                            timed = True
                        if name and st:
                            per = stage_status.setdefault(name, {})
                            per[st] = per.get(st, 0) + 1
                    # A run with stages but no usable `ms` on any of them is
                    # not a zero-millisecond run, it is an unmeasured one --
                    # folding it in as 0 would drag every average down.
                    if timed:
                        run_ms.append(total_ms)

                # -- extraction route, and value in its own currency --------
                extracted = _loads(row["extracted_json"], dict)
                if extracted is None:
                    if row["extracted_json"]:
                        malformed["extracted"] += 1
                    extracted = {}
                method = extracted.get("extraction_method")

                # "Nothing could be read" is what main.py's _abort_unreadable
                # records, and it is the one processing outcome that is a
                # failure of the machinery rather than a verdict about the
                # invoice. Read from the extraction method the pipeline wrote,
                # not inferred from fields happening to be absent.
                if not method or method == "none":
                    extraction_failures += 1
                routes[method or "(unrecorded)"] = routes.get(method or "(unrecorded)", 0) + 1

                # Money is summed PER CURRENCY and never across currencies:
                # 1,000 EUR added to 1,000 USD produces a number that is not an
                # amount of anything.
                if _is_number(row["total"]):
                    ccy = extracted.get("currency") or "(unknown)"
                    bucket = value_by_currency.setdefault(
                        ccy, {"runs": 0, "processed": 0.0, "approved": 0.0,
                              "held": 0.0, "rejected": 0.0})
                    bucket["runs"] += 1
                    bucket["processed"] += float(row["total"])
                    slot = {"APPROVED": "approved", "NEEDS_REVIEW": "held",
                            "REJECTED": "rejected"}.get(row["automated"])
                    if slot:
                        bucket[slot] += float(row["total"])

                # -- which rules failed, and which provider read it ---------
                audit = _loads(row["audit_json"], dict)
                if audit is None:
                    if row["audit_json"]:
                        malformed["audit"] += 1
                else:
                    extraction = audit.get("extraction")
                    provider = extraction.get("provider") if isinstance(extraction, dict) else None
                    if provider:
                        providers[provider] = providers.get(provider, 0) + 1
                    # `rules_failed` is the list of rule NAMES that did not
                    # pass -- a stable, hand-written vocabulary from rules.py.
                    # Deliberately not the reason SENTENCE, which carries
                    # amounts and invoice numbers and so groups into a list of
                    # individual invoices rather than a list of causes.
                    for name in (audit.get("rules_failed") or []):
                        if isinstance(name, str):
                            failed_rules[name] = failed_rules.get(name, 0) + 1
    finally:
        conn.close()

    return {
        "scanned": scanned,
        "run_ms": run_ms,
        "stage_ms": stage_ms,
        "stage_status": stage_status,
        "routes": routes,
        "providers": providers,
        "failed_rules": failed_rules,
        "value_by_currency": value_by_currency,
        "extraction_failures": extraction_failures,
        "malformed": malformed,
    }


# --------------------------------------------------------------------------
# run-level counts -- the base every headline KPI is built from
# --------------------------------------------------------------------------

def _run_counts(window: Window) -> dict:
    """One query for every count the headline KPIs need.

    Deliberately a single pass with FILTERed aggregates rather than one query
    per figure: the alternative is eight scans of the same rows producing eight
    numbers that were each true at a slightly different instant.
    """
    params = []
    sql = f"""
        SELECT
            COUNT(*)                                                        AS runs,
            COUNT(*) FILTER (WHERE {AUTOMATED} = 'APPROVED')                AS auto_approved,
            COUNT(*) FILTER (WHERE {AUTOMATED} = 'NEEDS_REVIEW')            AS auto_held,
            COUNT(*) FILTER (WHERE {AUTOMATED} = 'REJECTED')                AS auto_rejected,
            COUNT(*) FILTER (WHERE runs.human_decision = 'ACCEPTED')        AS human_accepted,
            COUNT(*) FILTER (WHERE runs.human_decision = 'REJECTED')        AS human_rejected,
            COUNT(*) FILTER (WHERE runs.human_decision IS NOT NULL)         AS reviewed,
            COUNT(*) FILTER (WHERE {AUTOMATED} = 'NEEDS_REVIEW'
                             AND runs.human_decision IS NULL)               AS awaiting_review,
            COUNT(*) FILTER (WHERE runs.status = 'APPROVED')                AS status_approved,
            COUNT(*) FILTER (WHERE runs.status = 'NEEDS_REVIEW')            AS status_held,
            COUNT(*) FILTER (WHERE runs.status = 'REJECTED')                AS status_rejected,
            COUNT(*) FILTER (WHERE EXISTS (
                SELECT 1 FROM invoice_activity ia
                WHERE ia.run_id = runs.id AND ia.event_type = %s))          AS overridden
        FROM runs WHERE 1=1"""
    params.append(OVERRIDE_EVENT)
    sql += window.clause("runs.created_at", params)

    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
    finally:
        conn.close()
    return dict(row)


def _headline_kpis(counts: dict, scan: dict) -> dict:
    """The five headline KPIs. Definitions live in the module docstring and
    travel to the client on each one."""
    runs = counts["runs"]
    automated = counts["auto_approved"] + counts["auto_rejected"]
    readable = scan["scanned"] - scan["extraction_failures"]

    # RESOLVED: waiting on nobody. Either the rules reached a terminal verdict
    # on their own, or a person has ruled on the hold.
    resolved = automated + counts["reviewed"]
    # ...and reached that state without an administrator stepping outside the
    # review path. `overridden` counts runs with at least one STATUS_OVERRIDDEN
    # event, which may include runs that are also `resolved`, so it is
    # subtracted from the numerator rather than from the denominator.
    resolved_clean = max(0, resolved - counts["overridden"])

    return {
        "automation_rate": _kpi(
            automated, runs,
            "Runs the deterministic rules decided outright (APPROVED or "
            "REJECTED), over every run that entered. A correct automatic "
            "rejection counts as automation. Read from automated_decision, "
            "which no later human ruling rewrites."),
        "processing_success_rate": _kpi(
            readable, scan["scanned"],
            "Runs whose document the pipeline could actually read and "
            "evaluate, over every run that entered. A machinery metric: it "
            "says nothing about whether the invoice was approved."),
        "task_success_ratio": _kpi(
            resolved_clean, runs,
            "Runs that reached a final outcome through the designed path -- "
            "terminal by rules, or held and then ruled on by a reviewer -- "
            "and were not overridden by an administrator, over every run that "
            "entered. Measures operational success, NOT correctness: this "
            "database holds no ground truth about which decision was right."),
        "human_review_rate": _kpi(
            counts["auto_held"], runs,
            "Runs the rules held for a person (automated_decision = "
            "NEEDS_REVIEW), over every run that entered. The exact complement "
            "of the automation rate."),
        "review_completion_rate": _kpi(
            counts["reviewed"], counts["auto_held"],
            "Held runs a person has since ruled on, over all held runs. The "
            "remainder is the open review backlog."),
    }


# --------------------------------------------------------------------------
# public: overview
# --------------------------------------------------------------------------

def overview(window: Window) -> dict:
    """Headline KPIs, the decision mix, value by currency, and the backlog."""
    counts = _run_counts(window)
    scan = _scan_run_json(window)
    backlog = _backlog(window)

    return {
        "range": window.as_dict(),
        "generated_at": _utc_now().isoformat(),
        "volume": {
            "runs": counts["runs"],
            "automated": counts["auto_approved"] + counts["auto_rejected"],
            "held": counts["auto_held"],
            "reviewed": counts["reviewed"],
            "overridden": counts["overridden"],
            "extraction_failures": scan["extraction_failures"],
        },
        "kpis": _headline_kpis(counts, scan),
        "decisions": {
            # What the RULES concluded. Immutable, and therefore the only one
            # of the three that answers "how did the process behave".
            "automated": {
                "APPROVED": counts["auto_approved"],
                "NEEDS_REVIEW": counts["auto_held"],
                "REJECTED": counts["auto_rejected"],
            },
            # What PEOPLE concluded about the ones they were handed.
            "human": {
                "ACCEPTED": counts["human_accepted"],
                "REJECTED": counts["human_rejected"],
                "not_reviewed": counts["runs"] - counts["reviewed"],
            },
            # What the LEDGER currently reads. Differs from `automated` exactly
            # where a person or an administrator moved a run.
            "status": {
                "APPROVED": counts["status_approved"],
                "NEEDS_REVIEW": counts["status_held"],
                "REJECTED": counts["status_rejected"],
            },
        },
        # Never a single cross-currency total -- see _scan_run_json.
        "value_by_currency": scan["value_by_currency"],
        "backlog": backlog,
        "data_quality": _data_quality(scan),
    }


def _data_quality(scan: dict) -> dict:
    """What this window's answer is built on, and what it had to skip.

    Surfaced rather than swallowed: a caller comparing two figures deserves to
    know if some rows contributed to neither.
    """
    malformed = scan["malformed"]
    return {
        "runs_scanned": scan["scanned"],
        "runs_with_timing": len(scan["run_ms"]),
        "malformed_json": malformed,
        "malformed_total": sum(malformed.values()),
    }


def _backlog(window: Window) -> dict:
    """What is open right now.

    NOT windowed on the claim side: a claim is a live fact about this moment,
    and filtering it by the reporting window would report "who is working right
    now" as of last month. The held-run counts ARE windowed, because those ask
    about the work that entered in the window.
    """
    params = []
    sql = f"""
        SELECT COUNT(*) AS awaiting,
               MIN(runs.created_at) AS oldest_created_at
        FROM runs
        WHERE {AUTOMATED} = 'NEEDS_REVIEW' AND runs.human_decision IS NULL"""
    sql += window.clause("runs.created_at", params)

    now_iso = _utc_now().isoformat()
    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            # "Who holds a claim right now" is derived the same way
            # get_active_claim derives it -- most recent unreleased row per
            # run, lease not yet expired -- rather than from any stored
            # current-holder field, because there is not one.
            cur.execute(
                """SELECT COUNT(*) AS n FROM (
                       SELECT DISTINCT ON (rc.run_id) rc.run_id, rc.released_at, rc.expires_at
                       FROM review_claims rc
                       ORDER BY rc.run_id, rc.id DESC) latest
                   WHERE latest.released_at IS NULL AND latest.expires_at > %s""",
                (now_iso,))
            claimed = cur.fetchone()["n"]
    finally:
        conn.close()

    oldest = row["oldest_created_at"]
    age = None
    if oldest:
        try:
            age = (_utc_now() - datetime.fromisoformat(oldest)).total_seconds()
        except (TypeError, ValueError):
            age = None

    return {
        "awaiting_review": row["awaiting"],
        "claimed_now": claimed,
        "oldest_awaiting_at": oldest,
        "oldest_awaiting_age_seconds": age,
    }


# --------------------------------------------------------------------------
# public: trends
# --------------------------------------------------------------------------

def trends(window: Window) -> dict:
    """One row per UTC calendar day: volume, outcomes, automation, timing.

    Aggregated in the database in a single grouped query, then padded in Python
    so days with no activity appear as explicit zeroes rather than as gaps the
    axis has to guess about. An empty day is a real fact about the day.

    Average processing time per day comes from the same JSON pass everything
    else does, keyed by day, so the trend and the headline figure can never
    disagree about what a millisecond is.
    """
    days = window.bucket_days()
    if days == 0:
        # `all` has no natural start. Rather than scanning to the beginning of
        # time, the series starts at the first run there is.
        first = _first_run_day()
        if first is None:
            return {"range": window.as_dict(), "buckets": [], "timezone": "UTC"}
        start = first
        end = _day_start(_utc_now()) + timedelta(days=1)
        days = max(1, (end - start).days)
    else:
        start, end = window.start_date, window.end_date

    if days > MAX_TREND_BUCKETS:
        raise AnalyticsError(
            f"that range covers {days} daily buckets; the maximum is "
            f"{MAX_TREND_BUCKETS}. Narrow the range.")

    params = []
    # substring(created_at from 1 for 10) is the UTC calendar day: the stored
    # value is ISO-8601 with a +00:00 offset, so its first ten characters ARE
    # the UTC date, with no cast and no timezone conversion to get wrong.
    sql = f"""
        SELECT substring(runs.created_at from 1 for 10) AS day,
               COUNT(*) AS runs,
               COUNT(*) FILTER (WHERE {AUTOMATED} = 'APPROVED')     AS approved,
               COUNT(*) FILTER (WHERE {AUTOMATED} = 'NEEDS_REVIEW') AS needs_review,
               COUNT(*) FILTER (WHERE {AUTOMATED} = 'REJECTED')     AS rejected,
               COUNT(*) FILTER (WHERE runs.human_decision IS NOT NULL) AS reviewed
        FROM runs WHERE 1=1"""
    sql += window.clause("runs.created_at", params)
    sql += " GROUP BY 1 ORDER BY 1"

    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = {r["day"]: dict(r) for r in cur.fetchall()}
    finally:
        conn.close()

    timing = _daily_run_ms(window)

    buckets = []
    for i in range(days):
        day = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        r = rows.get(day)
        total = r["runs"] if r else 0
        automated = (r["approved"] + r["rejected"]) if r else 0
        samples = timing.get(day) or []
        buckets.append({
            "day": day,
            "runs": total,
            "approved": r["approved"] if r else 0,
            "needs_review": r["needs_review"] if r else 0,
            "rejected": r["rejected"] if r else 0,
            "reviewed": r["reviewed"] if r else 0,
            # Null, not zero, on a day with no runs: there was no automation
            # rate that day, which is not the same as an automation rate of 0%.
            "automation_rate": _rate(automated, total),
            "approval_rate": _rate(r["approved"] if r else 0, total),
            "rejection_rate": _rate(r["rejected"] if r else 0, total),
            "review_rate": _rate(r["needs_review"] if r else 0, total),
            "avg_processing_ms": (statistics.fmean(samples) if samples else None),
            "timed_runs": len(samples),
        })

    return {"range": window.as_dict(), "timezone": "UTC", "buckets": buckets}


def _first_run_day():
    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MIN(created_at) AS first FROM runs")
            first = cur.fetchone()["first"]
    finally:
        conn.close()
    if not first:
        return None
    try:
        return _day_start(datetime.fromisoformat(first))
    except (TypeError, ValueError):
        return None


def _daily_run_ms(window: Window) -> dict:
    """{UTC day: [total ms per run]}. Same guarded parse as _scan_run_json --
    a malformed blob costs its own row and nothing else."""
    params = []
    sql = ("SELECT substring(runs.created_at from 1 for 10) AS day, runs.stages_json "
           "FROM runs WHERE runs.stages_json IS NOT NULL")
    sql += window.clause("runs.created_at", params)

    out = {}
    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for row in cur.fetchall():
                stages = _loads(row["stages_json"], list)
                if stages is None:
                    continue
                total = 0.0
                timed = False
                for s in stages:
                    if isinstance(s, dict) and _is_number(s.get("ms")):
                        total += float(s["ms"])
                        timed = True
                if timed:
                    out.setdefault(row["day"], []).append(total)
    finally:
        conn.close()
    return out


# --------------------------------------------------------------------------
# public: processing efficiency
# --------------------------------------------------------------------------

def processing(window: Window) -> dict:
    """Run and per-stage timing, extraction routes, and extraction budget use.

    Per-stage figures are what answer "where is the bottleneck": the pipeline
    already writes an `ms` on every stage it runs, so the slowest stage is a
    fact the application recorded, not an estimate.
    """
    scan = _scan_run_json(window)

    stages = []
    for name, samples in scan["stage_ms"].items():
        s = _stats(samples)
        stages.append({
            "stage": name,
            "runs": len(samples),
            "total_ms": sum(samples),
            "statuses": scan["stage_status"].get(name, {}),
            **s,
        })
    # Slowest first: the reason to look at this table is to find the bottleneck.
    stages.sort(key=lambda s: (s["average"] is None, -(s["average"] or 0)))

    total_measured = sum(s["total_ms"] for s in stages)
    for s in stages:
        # Share of all measured processing time this stage accounts for. Null
        # rather than 0 when nothing was measured at all.
        s["share_of_time"] = _rate(s["total_ms"], total_measured)

    return {
        "range": window.as_dict(),
        "run_time_ms": _stats(scan["run_ms"]),
        "stages": stages,
        "extraction": {
            # Which route read each invoice, and which provider the audit trail
            # says served it. Two different questions: the route is what the
            # pipeline chose, the provider is who actually answered.
            "by_route": scan["routes"],
            "by_provider": scan["providers"],
            "failures": scan["extraction_failures"],
            "failure_rate": _rate(scan["extraction_failures"], scan["scanned"]),
        },
        "quota": _quota_usage(),
        "data_quality": _data_quality(scan),
    }


def _quota_usage() -> dict:
    """Daily extraction budget consumption, per provider.

    NO MONETARY COST IS REPORTED. This application persists request counts
    (quota.py's `extraction_quota` table) and nothing else -- no per-token
    accounting, no price table, no provider invoice. A dollar figure here
    would have to be invented, so counts are what is reported and the
    limitation is stated in the payload rather than only in documentation.
    """
    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT day, provider, used FROM extraction_quota
                   ORDER BY day DESC, provider LIMIT 60""")
            rows = [dict(r) for r in cur.fetchall()]
    except Exception:
        # extraction_quota is created lazily by quota.py on first use, so a
        # database that has never run an extraction genuinely has no table.
        # That is "nothing has been extracted yet", not a failure.
        rows = []
    finally:
        conn.close()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    providers = []
    for provider in (quota.VISION, quota.TEXT):
        used = next((r["used"] for r in rows
                     if r["day"] == today and r["provider"] == provider), 0)
        limit = quota.limit_for(provider)
        providers.append({
            "provider": provider,
            "used_today": used,
            "limit": limit,
            "remaining": max(0, limit - used),
            "utilisation": _rate(used, limit),
        })

    return {
        "today": today,
        "providers": providers,
        "history": rows,
        "cost_available": False,
        "note": ("Request counts only. This application does not persist token "
                 "counts or monetary cost, so no spend figure can be derived "
                 "from its data."),
    }


# --------------------------------------------------------------------------
# public: reviews
# --------------------------------------------------------------------------

def reviews(window: Window) -> dict:
    """The human-review funnel, its latency, and what reviewers decided.

    Aggregate only -- no per-person figures. Those live behind `users()`, which
    is authorised differently for the reasons set out there.
    """
    counts = _run_counts(window)
    held = counts["auto_held"]
    ruled = counts["reviewed"]

    return {
        "range": window.as_dict(),
        "funnel": {
            "runs": counts["runs"],
            "held_for_review": held,
            "ruled_on": ruled,
            "accepted": counts["human_accepted"],
            "rejected": counts["human_rejected"],
            "still_awaiting": counts["awaiting_review"],
        },
        "rates": {
            "review_rate": _kpi(
                held, counts["runs"],
                "Runs the rules held for a person, over every run that "
                "entered."),
            "completion_rate": _kpi(
                ruled, held,
                "Held runs a person has ruled on, over all held runs."),
            "accept_rate": _kpi(
                counts["human_accepted"], ruled,
                "Held runs a reviewer accepted, over all held runs ruled on. "
                "An acceptance means the reviewer judged the invoice fine "
                "despite the hold -- it is NOT evidence the hold was wrong, "
                "which this database cannot establish."),
            "reject_rate": _kpi(
                counts["human_rejected"], ruled,
                "Held runs a reviewer rejected, over all held runs ruled on. "
                "A rejection means the reviewer judged the concern real -- it "
                "is NOT evidence the hold was right, which this database "
                "cannot establish."),
        },
        # What the rules said, against what the person said. Every held run
        # that has been ruled on appears exactly once.
        "transitions": _transition_matrix(window),
        "latency": _review_latency(window),
        "reasons": _hold_reasons(window),
        "activity": _review_activity(window),
    }


def _transition_matrix(window: Window) -> list:
    """automated_decision x human_decision x final ledger status.

    Reported as rows rather than as a nested object so a client can render it
    as a table without knowing the vocabulary in advance -- and so a
    combination nobody anticipated shows up instead of being dropped.
    """
    params = []
    sql = f"""
        SELECT {AUTOMATED} AS automated,
               COALESCE(runs.human_decision, '(none)') AS human,
               runs.status AS final_status,
               COALESCE(runs.final_decision, runs.status) AS final_decision,
               COUNT(*) AS n
        FROM runs WHERE 1=1"""
    sql += window.clause("runs.created_at", params)
    sql += " GROUP BY 1,2,3,4 ORDER BY n DESC, 1, 2"

    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _review_latency(window: Window) -> dict:
    """How long invoices waited, measured two different ways.

    `time_to_decision` -- created_at to reviewed_at. Every reviewed run has
    both, so this is available for all of them. It answers "how long did the
    vendor wait", which is the number an AP lead is asked about.

    `handling_time` -- the first claim on the run to reviewed_at. Answers "how
    long did the reviewer take once they picked it up", and is available ONLY
    for runs that were actually claimed. Claiming is optional in this
    application (§6.4: every review submitted before Phase D had no claim), so
    the sample is deliberately reported alongside `unclaimed`, and a client
    that does not check the sample count will at least see it.

    Both computed in SQL. The timestamps involved are machine-written by one
    code path each, so casting them is safe in a way that casting the
    hand-extensible JSON columns is not.
    """
    params = []
    sql = """
        WITH reviewed AS (
            SELECT runs.id,
                   runs.created_at::timestamptz  AS created_at,
                   runs.reviewed_at::timestamptz AS reviewed_at,
                   (SELECT MIN(rc.claimed_at)::timestamptz FROM review_claims rc
                     WHERE rc.run_id = runs.id) AS first_claim_at
            FROM runs
            WHERE runs.reviewed_at IS NOT NULL AND runs.created_at IS NOT NULL"""
    sql += window.clause("runs.created_at", params)
    sql += """
        )
        SELECT
            COUNT(*) AS reviewed,
            COUNT(*) FILTER (WHERE first_claim_at IS NULL) AS unclaimed,
            AVG(EXTRACT(EPOCH FROM (reviewed_at - created_at)))            AS ttd_avg,
            MIN(EXTRACT(EPOCH FROM (reviewed_at - created_at)))            AS ttd_min,
            MAX(EXTRACT(EPOCH FROM (reviewed_at - created_at)))            AS ttd_max,
            percentile_cont(0.5) WITHIN GROUP (
                ORDER BY EXTRACT(EPOCH FROM (reviewed_at - created_at)))   AS ttd_median,
            COUNT(*) FILTER (WHERE first_claim_at IS NOT NULL)             AS handled,
            AVG(EXTRACT(EPOCH FROM (reviewed_at - first_claim_at)))        AS ht_avg,
            MIN(EXTRACT(EPOCH FROM (reviewed_at - first_claim_at)))        AS ht_min,
            MAX(EXTRACT(EPOCH FROM (reviewed_at - first_claim_at)))        AS ht_max,
            percentile_cont(0.5) WITHIN GROUP (
                ORDER BY EXTRACT(EPOCH FROM (reviewed_at - first_claim_at))) AS ht_median
        FROM reviewed"""

    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
    finally:
        conn.close()

    def block(samples, avg, median, lo, hi):
        # Explicit nulls, never zeros, when nothing was measured.
        return {
            "samples": samples,
            "average_seconds": float(avg) if avg is not None else None,
            "median_seconds": float(median) if median is not None else None,
            "min_seconds": float(lo) if lo is not None else None,
            "max_seconds": float(hi) if hi is not None else None,
        }

    return {
        "time_to_decision": {
            **block(row["reviewed"], row["ttd_avg"], row["ttd_median"],
                    row["ttd_min"], row["ttd_max"]),
            "definition": "Invoice created to human ruling recorded.",
        },
        "handling_time": {
            **block(row["handled"], row["ht_avg"], row["ht_median"],
                    row["ht_min"], row["ht_max"]),
            "unclaimed_reviews": row["unclaimed"],
            "definition": ("First review claim to human ruling recorded. "
                           "Available only for runs that were claimed; "
                           "claiming is optional, so `unclaimed_reviews` "
                           "counts the rulings this cannot measure."),
        },
    }


def _hold_reasons(window: Window) -> list:
    """Which rules actually failed, ranked.

    Grouped by RULE NAME from `audit_json.rules_failed` -- a fixed vocabulary
    written by rules.py -- rather than by the reason sentence, which embeds the
    invoice's own amounts and numbers and would therefore group into a list of
    individual invoices instead of a list of causes.

    A run failing three rules contributes to three rows, so these counts sum to
    more than the run count. Said here because a table of them looks like it
    should sum to the total and does not.
    """
    scan = _scan_run_json(window)
    total = scan["scanned"]
    return [
        {"rule": name, "runs": n, "share_of_runs": _rate(n, total)}
        for name, n in sorted(scan["failed_rules"].items(),
                              key=lambda kv: (-kv[1], kv[0]))
    ]


def _review_activity(window: Window) -> dict:
    """Counts of what happened in the activity log, by event type.

    Aggregate and unattributed -- the actor column is deliberately not read
    here. Per-person figures are `users()`, which is authorised separately.
    """
    params = []
    sql = ("SELECT event_type AS k, COUNT(*) AS n FROM invoice_activity WHERE 1=1")
    sql += window.clause("invoice_activity.created_at", params)
    sql += " GROUP BY 1 ORDER BY n DESC"

    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return _counts(cur.fetchall())
    finally:
        conn.close()


# --------------------------------------------------------------------------
# public: vendors and purchase orders
# --------------------------------------------------------------------------

def vendors(window: Window, limit: int = DEFAULT_GROUP_LIMIT) -> dict:
    """Per-vendor invoice behaviour, and per-PO budget position.

    The PO half reuses the ledger rule rather than restating it -- see
    storage.consumed_amounts_by_po(), which is the set-based sibling of
    `_consumed` and lives beside it so the two cannot drift.
    """
    params = []
    sql = f"""
        SELECT COALESCE(runs.vendor_name, '(unidentified)') AS vendor,
               COUNT(*) AS runs,
               COUNT(*) FILTER (WHERE {AUTOMATED} = 'APPROVED')     AS approved,
               COUNT(*) FILTER (WHERE {AUTOMATED} = 'NEEDS_REVIEW') AS held,
               COUNT(*) FILTER (WHERE {AUTOMATED} = 'REJECTED')     AS rejected,
               COUNT(*) FILTER (WHERE runs.human_decision IS NOT NULL) AS reviewed,
               COUNT(*) FILTER (WHERE runs.status = 'APPROVED')     AS approved_now
        FROM runs WHERE 1=1"""
    sql += window.clause("runs.created_at", params)
    sql += " GROUP BY 1 ORDER BY runs DESC, vendor ASC LIMIT %s"
    params.append(limit)

    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    timing = _vendor_run_ms(window)
    out = []
    for r in rows:
        n = r["runs"]
        samples = timing.get(r["vendor"]) or []
        out.append({
            "vendor": r["vendor"],
            "runs": n,
            "approved": r["approved"],
            "held": r["held"],
            "rejected": r["rejected"],
            "reviewed": r["reviewed"],
            "approved_now": r["approved_now"],
            "approval_rate": _rate(r["approved"], n),
            "hold_rate": _rate(r["held"], n),
            "rejection_rate": _rate(r["rejected"], n),
            "avg_processing_ms": statistics.fmean(samples) if samples else None,
            "timed_runs": len(samples),
        })

    return {
        "range": window.as_dict(),
        "vendors": out,
        "truncated": len(out) >= limit,
        "purchase_orders": purchase_orders(window),
    }


def _vendor_run_ms(window: Window) -> dict:
    params = []
    sql = ("SELECT COALESCE(runs.vendor_name, '(unidentified)') AS vendor, runs.stages_json "
           "FROM runs WHERE runs.stages_json IS NOT NULL")
    sql += window.clause("runs.created_at", params)

    out = {}
    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for row in cur.fetchall():
                stages = _loads(row["stages_json"], list)
                if stages is None:
                    continue
                total, timed = 0.0, False
                for s in stages:
                    if isinstance(s, dict) and _is_number(s.get("ms")):
                        total += float(s["ms"])
                        timed = True
                if timed:
                    out.setdefault(row["vendor"], []).append(total)
    finally:
        conn.close()
    return out


def purchase_orders(window: Window) -> list:
    """Every PO with its budget position and the invoice activity against it.

    THE BUDGET FIGURES ARE THE LEDGER'S OWN. `consumed` comes from
    storage.consumed_amounts_by_po(), which is the same SUM over
    `run_allocations` joined to `runs.status = 'APPROVED'` that `_consumed`
    performs for a single PO -- not a second calculation that happens to agree
    today. A test asserts the two produce identical answers for every PO.

    Consumption is deliberately NOT windowed: a PO's remaining balance is its
    balance, and reporting "remaining as of the last 30 days" would be a number
    with no meaning to anyone about to approve an invoice against it. The
    invoice COUNTS beside it are windowed, and are labelled as such.
    """
    consumed = storage.consumed_amounts_by_po()

    params = []
    sql = f"""
        SELECT ra.po_number AS po_number,
               COUNT(DISTINCT runs.id) AS runs,
               COUNT(DISTINCT runs.id) FILTER (WHERE {AUTOMATED} = 'APPROVED') AS approved,
               COUNT(DISTINCT runs.id) FILTER (WHERE {AUTOMATED} = 'NEEDS_REVIEW') AS held,
               COUNT(DISTINCT runs.id) FILTER (WHERE {AUTOMATED} = 'REJECTED') AS rejected,
               SUM(ra.amount) FILTER (WHERE runs.status = 'APPROVED') AS allocated_approved
        FROM run_allocations ra
        JOIN runs ON runs.id = ra.run_id
        WHERE 1=1"""
    sql += window.clause("runs.created_at", params)
    sql += " GROUP BY 1"

    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            activity = {r["po_number"]: dict(r) for r in cur.fetchall()}
    finally:
        conn.close()

    out = []
    for po in storage.list_purchase_orders():
        number = po["po_number"]
        used = consumed.get(number, 0.0)
        amount = po.get("amount") or 0.0
        act = activity.get(number, {})
        out.append({
            "po_number": number,
            "vendor": po.get("vendor"),
            "currency": po.get("currency"),
            "status": po.get("status"),
            "amount": amount,
            # Ledger figures -- all time, by design (see the docstring).
            "consumed": used,
            "remaining": amount - used,
            "utilisation": _rate(used, amount) if amount else None,
            "over_budget": used > amount,
            # Window figures -- invoice activity inside the reporting range.
            "runs_in_range": act.get("runs", 0),
            "approved_in_range": act.get("approved", 0),
            "held_in_range": act.get("held", 0),
            "rejected_in_range": act.get("rejected", 0),
            "allocated_approved_in_range": float(act["allocated_approved"])
            if act.get("allocated_approved") is not None else 0.0,
        })
    out.sort(key=lambda p: (-(p["utilisation"] or 0), p["po_number"]))
    return out


# --------------------------------------------------------------------------
# public: email ingestion funnel
# --------------------------------------------------------------------------

def email(window: Window) -> dict:
    """Did email ingestion actually deliver invoices, and where did mail stop?

    The funnel follows the order email_ingest.py evaluates in, because that
    order is the design: triage stops junk BEFORE Phase F's cryptography and
    long before an LLM call, so a drop-off between two steps names the stage
    that stopped it.
    """
    params = []
    sql = """
        SELECT
            COUNT(*) AS received,
            COUNT(*) FILTER (WHERE relevance IN ('HIGH','POSSIBLE'))    AS relevant,
            COUNT(*) FILTER (WHERE relevance IN ('LOW','IRRELEVANT'))   AS filtered_out,
            COUNT(*) FILTER (WHERE relevance IS NULL)                   AS relevance_unrecorded,
            COUNT(*) FILTER (WHERE status IN ('ADMITTED','RELEASED'))   AS admitted,
            COUNT(*) FILTER (WHERE status = 'QUARANTINED')              AS quarantined,
            COUNT(*) FILTER (WHERE status = 'DISCARDED')                AS discarded,
            COUNT(*) FILTER (WHERE has_pdf_attachment)                  AS with_pdf,
            COUNT(*) FILTER (WHERE run_id IS NOT NULL)                  AS produced_a_run
        FROM email_messages WHERE 1=1"""
    sql += window.clause("email_messages.received_at", params)

    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            funnel = dict(cur.fetchone())

            def by(column):
                # Same rule as Window.clause: hard-coded call sites only, and
                # checked rather than trusted.
                if not _SAFE_COLUMN.fullmatch(column):
                    raise AnalyticsError(f"unsafe column reference {column!r}")
                p = []
                q = (f"SELECT COALESCE({column}::text,'(none)') AS k, COUNT(*) AS n "
                     f"FROM email_messages WHERE 1=1")
                q += window.clause("email_messages.received_at", p)
                q += " GROUP BY 1 ORDER BY n DESC"
                cur.execute(q, p)
                return _counts(cur.fetchall())

            by_relevance = by("relevance")
            by_classification = by("classification")
            by_ingest_status = by("ingest_status")
            by_security_status = by("status")
            by_sender_type = by("sender_type")
            by_trust = by("trust_status")

            # Attachments are the real unit of invoice work: ONE email can
            # carry three invoices and produce three runs, so counting emails
            # would understate what ingestion delivered.
            ap = []
            aq = """
                SELECT COALESCE(ea.status,'(none)') AS k, COUNT(*) AS n
                FROM email_attachments ea
                JOIN email_messages em ON em.id = ea.email_id
                WHERE 1=1"""
            aq += window.clause("em.received_at", ap)
            aq += " GROUP BY 1 ORDER BY n DESC"
            cur.execute(aq, ap)
            attachments_by_status = _counts(cur.fetchall())

            rp = []
            rq = f"""
                SELECT COUNT(*) AS attachments,
                       COUNT(*) FILTER (WHERE ea.is_invoice_candidate) AS candidates,
                       COUNT(*) FILTER (WHERE ea.run_id IS NOT NULL)   AS runs_created,
                       COUNT(*) FILTER (WHERE ea.run_id IS NOT NULL
                                        AND {AUTOMATED} = 'APPROVED')  AS runs_approved,
                       COUNT(*) FILTER (WHERE ea.run_id IS NOT NULL
                                        AND {AUTOMATED} = 'NEEDS_REVIEW') AS runs_held,
                       COUNT(*) FILTER (WHERE ea.run_id IS NOT NULL
                                        AND {AUTOMATED} = 'REJECTED')  AS runs_rejected
                FROM email_attachments ea
                JOIN email_messages em ON em.id = ea.email_id
                LEFT JOIN runs ON runs.id = ea.run_id
                WHERE 1=1"""
            rq += window.clause("em.received_at", rp)
            cur.execute(rq, rp)
            attachment_outcomes = dict(cur.fetchone())
    finally:
        conn.close()

    received = funnel["received"]
    return {
        "range": window.as_dict(),
        "funnel": {
            "received": received,
            "relevant": funnel["relevant"],
            "filtered_out": funnel["filtered_out"],
            "relevance_unrecorded": funnel["relevance_unrecorded"],
            "admitted": funnel["admitted"],
            "quarantined": funnel["quarantined"],
            "discarded": funnel["discarded"],
            "with_pdf_attachment": funnel["with_pdf"],
            "attachments": attachment_outcomes["attachments"],
            "invoice_candidates": attachment_outcomes["candidates"],
            "runs_created": attachment_outcomes["runs_created"],
            "runs_approved": attachment_outcomes["runs_approved"],
            "runs_held": attachment_outcomes["runs_held"],
            "runs_rejected": attachment_outcomes["runs_rejected"],
        },
        "rates": {
            "relevance_rate": _kpi(
                funnel["relevant"], received,
                "Messages triage judged worth processing (HIGH or POSSIBLE), "
                "over all messages received. Triage is biased toward "
                "processing: a missed invoice costs more than an extra LLM "
                "call."),
            "admission_rate": _kpi(
                funnel["admitted"], received,
                "Messages that passed trusted-source verification (ADMITTED "
                "or RELEASED), over all messages received."),
            "invoice_yield": _kpi(
                attachment_outcomes["runs_created"], received,
                "Invoice runs produced, over all messages received. Can "
                "exceed 1: one email carrying three invoices produces three "
                "runs."),
        },
        "by_relevance": _fill(by_relevance, config.EMAIL_RELEVANCE),
        "by_classification": _fill(by_classification, config.EMAIL_CLASSIFICATIONS),
        # Two different columns, deliberately reported separately: `status` is
        # Phase F's security verdict (has it been admitted, quarantined,
        # released, discarded) and `ingest_status` is Phase G's pipeline state
        # (was it filtered out, processed, did it fail). Collapsing them would
        # lose the difference between "we would not accept it" and "we never
        # tried".
        "by_security_status": _fill(by_security_status, config.EMAIL_STATUSES),
        "by_ingest_status": _fill(by_ingest_status, config.EMAIL_INGEST_STATUSES),
        "by_sender_type": _fill(by_sender_type, config.EMAIL_SENDER_TYPES),
        "by_trust_status": _fill(by_trust, config.EMAIL_TRUST_STATUSES),
        "attachments_by_status": _fill(attachments_by_status,
                                       config.EMAIL_ATTACHMENT_STATUSES),
    }


# --------------------------------------------------------------------------
# public: per-person workload
# --------------------------------------------------------------------------

def users(window: Window, viewer: str, see_everyone: bool) -> dict:
    """Reviewer workload -- the caller's own, or everyone's for an administrator.

    WHY THIS IS AUTHORISED DIFFERENTLY FROM EVERY OTHER ENDPOINT HERE

    Everything else in this module is about invoices. This is about PEOPLE: how
    many decisions a named employee made, and how quickly. That is
    employee-performance data, and `invoice:read` -- the scope a viewer account
    holds -- is not consent to see a colleague's throughput.

    The existing scope model (auth.SCOPES) has four scopes and no `manager`
    role, so the honest choice is between `invoice:review`, which every peer
    reviewer holds and would therefore expose each of them to all the others,
    and `invoice:admin`, which is the only scope in this application that
    already denotes authority OVER the review process rather than
    participation in it. `invoice:admin` it is.

    Rather than a second endpoint, this follows the pattern
    release_review_claim() already uses -- your own, unless you are an
    administrator -- and reports which of the two it did in `scope`, so a
    client never has to guess whether it is seeing everything.

    THE WINDOW HERE MEANS SOMETHING DIFFERENT from the window everywhere else
    in this module -- see the comment on the query below. In short: this
    endpoint counts decisions BY WHEN THEY WERE MADE, because that is what a
    workload is; every other endpoint counts invoices by when they arrived.

    NOT inventing a fifth scope is deliberate: Phases F and G both added
    endpoints without adding scopes, and a `analytics:people` scope would need
    a role to carry it, which means editing every deployment's user store.
    Recorded as a limitation rather than worked around.
    """
    params = []
    sql = """
        SELECT runs.reviewed_by AS username,
               COUNT(*) AS reviews,
               COUNT(*) FILTER (WHERE runs.human_decision = 'ACCEPTED') AS accepted,
               COUNT(*) FILTER (WHERE runs.human_decision = 'REJECTED') AS rejected,
               AVG(EXTRACT(EPOCH FROM (runs.reviewed_at::timestamptz
                   - runs.created_at::timestamptz))) AS avg_time_to_decision,
               percentile_cont(0.5) WITHIN GROUP (
                   ORDER BY EXTRACT(EPOCH FROM (runs.reviewed_at::timestamptz
                       - runs.created_at::timestamptz))) AS median_time_to_decision,
               MAX(runs.reviewed_at) AS last_review_at
        FROM runs
        WHERE runs.human_decision IS NOT NULL AND runs.reviewed_by IS NOT NULL
          AND runs.reviewed_at IS NOT NULL AND runs.created_at IS NOT NULL"""
    # WINDOWED ON `reviewed_at`, NOT ON `created_at` -- the one place in this
    # module that is.
    #
    # Every other endpoint asks about a COHORT OF INVOICES ("of the work that
    # entered last week, how much was automated"), so it windows on when the
    # invoice arrived. This one asks about WORK A PERSON DID, and "your
    # workload this week" plainly means the decisions you made this week, not
    # the decisions you made about invoices that happened to arrive this week.
    # Windowing this on `created_at` reports a reviewer who spent today
    # clearing a month-old backlog as having done nothing, which is both wrong
    # and the exact case a backlog queue produces most often. It also keeps
    # these counts consistent with the `events` counts below, which are
    # windowed on when the event happened, for the same reason.
    sql += window.clause("runs.reviewed_at", params)
    if not see_everyone:
        sql += " AND runs.reviewed_by = %s"
        params.append(viewer)
    sql += " GROUP BY 1 ORDER BY reviews DESC, username ASC"

    now_iso = _utc_now().isoformat()
    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]

            # Live claims -- the same derivation get_active_claim uses, one
            # row per run, most recent unreleased claim with an unexpired
            # lease. Not windowed: "what am I holding right now" is a fact
            # about now, not about the reporting range.
            cp = [now_iso]
            cq = """
                SELECT latest.claimed_by AS username, COUNT(*) AS n FROM (
                    SELECT DISTINCT ON (rc.run_id)
                           rc.run_id, rc.claimed_by, rc.released_at, rc.expires_at
                    FROM review_claims rc ORDER BY rc.run_id, rc.id DESC) latest
                WHERE latest.released_at IS NULL AND latest.expires_at > %s"""
            if not see_everyone:
                cq += " AND latest.claimed_by = %s"
                cp.append(viewer)
            cq += " GROUP BY 1"
            cur.execute(cq, cp)
            holding = _counts(cur.fetchall(), key="username")

            # What each person DID, from the append-only activity log. This is
            # the only place uploads and comments are attributable, because
            # `runs` records no uploader column.
            ap = []
            aq = """
                SELECT actor AS username, event_type, COUNT(*) AS n
                FROM invoice_activity
                WHERE actor IS NOT NULL"""
            aq += window.clause("invoice_activity.created_at", ap)
            if not see_everyone:
                aq += " AND actor = %s"
                ap.append(viewer)
            aq += " GROUP BY 1,2"
            cur.execute(aq, ap)
            events = {}
            for r in cur.fetchall():
                events.setdefault(r["username"], {})[r["event_type"]] = r["n"]
    finally:
        conn.close()

    by_user = {}
    for r in rows:
        by_user[r["username"]] = {
            "username": r["username"],
            "reviews": r["reviews"],
            "accepted": r["accepted"],
            "rejected": r["rejected"],
            "accept_rate": _rate(r["accepted"], r["reviews"]),
            "avg_time_to_decision_seconds": float(r["avg_time_to_decision"])
            if r["avg_time_to_decision"] is not None else None,
            "median_time_to_decision_seconds": float(r["median_time_to_decision"])
            if r["median_time_to_decision"] is not None else None,
            "last_review_at": r["last_review_at"],
            "claims_held_now": 0,
            "events": {},
        }

    # Someone who claimed or commented but has ruled on nothing yet still has a
    # workload, so they get a row rather than being invisible until their first
    # decision.
    for username, n in holding.items():
        by_user.setdefault(username, _blank_user(username))["claims_held_now"] = n
    for username, counts in events.items():
        by_user.setdefault(username, _blank_user(username))["events"] = counts

    users_out = sorted(by_user.values(),
                       key=lambda u: (-u["reviews"], -u["claims_held_now"], u["username"]))

    return {
        "range": window.as_dict(),
        # "self" or "all" -- so a client shows "your activity" rather than
        # implying it is looking at the whole team.
        "scope": "all" if see_everyone else "self",
        "viewer": viewer,
        "users": users_out,
        "note": (None if see_everyone else
                 "Showing your own activity only. Team-wide reviewer figures "
                 "require the invoice:admin permission."),
    }


def _blank_user(username: str) -> dict:
    return {
        "username": username,
        "reviews": 0,
        "accepted": 0,
        "rejected": 0,
        "accept_rate": None,
        "avg_time_to_decision_seconds": None,
        "median_time_to_decision_seconds": None,
        "last_review_at": None,
        "claims_held_now": 0,
        "events": {},
    }
