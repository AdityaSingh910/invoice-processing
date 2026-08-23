"""Background invoice processing: the work outlives the request that asked for it.

THE PROBLEM THIS MODULE EXISTS TO FIX
-------------------------------------
`POST /api/runs/stream` drove the whole nine-stage pipeline from inside the
body of its own `StreamingResponse`. An async generator only advances when
something pulls from it, and the thing pulling was the browser's `fetch`. So
refreshing the page -- or navigating away, or closing the tab -- aborted that
fetch, ASGI cancelled the response task, and `CancelledError` was raised into
the generator at whatever `await` it happened to be sitting on. The pipeline
stopped mid-invoice.

And because `runs` is written ONCE, at the very end (`storage.save_run_checked`
in the DECISION stage), nothing at all was persisted: no row, no document, no
activity. The upload had not failed, it had never happened. There was nothing
for a reloaded page to find, which is exactly what it found.

WHAT REPLACES IT
----------------
The upload endpoint now does the cheap, synchronous half -- authorize, read the
bytes, check the magic number, write a `processing_jobs` row -- and hands the
work to a small thread pool that has no reference to the request at all. The
request returns a job id immediately. Whatever the browser does next, the
worker keeps going and writes what it finds to Postgres.

WHY A THREAD POOL AND NOT A QUEUE SERVICE
-----------------------------------------
Nothing external was introduced because nothing external was needed. The
brief's requirement is that processing survive the FRONTEND disconnecting, not
that it survive the backend being replaced, and this deployment already runs
exactly one uvicorn worker in one Railway container (see the Dockerfile and
CLAUDE.md §7k.7 for why that one-replica decision was made deliberately). A
pool in that process satisfies the requirement with no broker, no second
service, no new credential and no new failure mode.

Threads rather than `asyncio.create_task`, and that is the interesting half:
this pipeline blocks. `extraction` makes a synchronous HTTPS call to Groq or
Gemini, `pdfplumber` parses on the CPU, and `psycopg2` blocks on the socket.
Several of those on the event loop would stall every HTTP request in the
process behind them -- the same reason `email_ingest` already runs its poller
through `asyncio.to_thread`. Each worker drives the existing async generator on
its own private loop with `asyncio.run`, which is precisely the pattern
`email_ingest._run_invoice_pipeline` established.

WHAT IS NOT REIMPLEMENTED HERE
------------------------------
The pipeline. This module consumes `main.run_pipeline` -- the same generator
the SSE endpoint drives and the same one email ingestion drives -- and reads
the frames it already emits. Every stage, the confidence gate, PO matching, the
allocation ledger, the audit trail, document persistence and review routing are
the ones every other door gets, because they ARE the ones every other door
gets. Nothing about extraction or about how an invoice is judged changed.
"""
import asyncio
import atexit
import json
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

import config
import storage

# One pool for the process, built on first use so importing this module opens
# no threads -- the test suite imports the app hundreds of times.
_pool = None
_pool_lock = threading.Lock()
_atexit_registered = False

# Job ids currently held by a worker in THIS process. Purely diagnostic (the
# `/api/jobs` surface reads the database, never this), and the reason it is not
# the source of truth is the whole point of the module: a set in memory is
# exactly the kind of state that disappears when something restarts.
_in_flight = set()
_in_flight_lock = threading.Lock()


def _get_pool() -> ThreadPoolExecutor:
    global _pool, _atexit_registered
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=config.job_workers(),
                                       thread_name_prefix="invoice-job")
            if not _atexit_registered:
                # Non-daemon threads, joined at interpreter exit. A container
                # being replaced therefore finishes the invoice it is holding
                # rather than abandoning it half-read, and a pipeline run is
                # seconds -- well inside any sensible graceful-shutdown window.
                # Registered once for the process, not once per pool: the app's
                # own shutdown handler builds a fresh pool if more work arrives
                # (which is what every TestClient in the suite does).
                atexit.register(_shutdown_pool)
                _atexit_registered = True
        return _pool


def _shutdown_pool():
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown(wait=True)


def shutdown(wait: bool = True):
    """Stop accepting work and let what is running finish. Called on app
    shutdown; safe to call when nothing was ever started."""
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown(wait=wait)


def in_flight() -> int:
    """How many invoices this process is reading right now."""
    with _in_flight_lock:
        return len(_in_flight)


def submit(job_id: str, filename: str, pdf_bytes: bytes, uploaded_by: str = None,
           source: str = "MANUAL_UPLOAD", portal_client=None):
    """Hand a queued job to the pool. Returns immediately.

    The caller has already written the job row, so the durable record of this
    upload exists before anything is submitted here -- if the process died
    between the two, the job would be visible as one nobody picked up rather
    than vanishing the way an upload used to.
    """
    _get_pool().submit(_work, job_id, filename, pdf_bytes, uploaded_by, source,
                       portal_client)


def _work(job_id, filename, pdf_bytes, uploaded_by, source, portal_client):
    """One invoice, start to finish, off the request thread."""
    # THE SECOND HALF OF THE DUPLICATE GUARD. The first is the partial unique
    # index that stops a second job being created for an upload already in
    # flight; this stops a job that somehow reached two workers being read
    # twice. Claiming is a locked queued -> processing transition, so exactly
    # one caller can win it and the loser does nothing at all.
    if not storage.claim_processing_job(job_id):
        return

    with _in_flight_lock:
        _in_flight.add(job_id)
    try:
        _drive(job_id, filename, pdf_bytes, uploaded_by, source, portal_client)
    except BaseException as exc:      # noqa: BLE001 -- a worker may not die quietly
        # A failure has to reach the database, because the database is the only
        # thing the reloaded page will read. A job left `processing` after its
        # worker crashed would report "still working on it" for ever.
        detail = f"{exc.__class__.__name__}: {exc}"
        print("[error] processing job " + str(job_id) + " failed: " +
              exc.__class__.__name__, file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        try:
            storage.fail_processing_job(
                job_id,
                "Processing failed and did not complete. " + detail)
        except Exception:
            print("[error] processing job " + str(job_id) +
                  " could not be marked failed", file=sys.stderr)
    finally:
        with _in_flight_lock:
            _in_flight.discard(job_id)


def _drive(job_id, filename, pdf_bytes, uploaded_by, source, portal_client):
    """Consume main.run_pipeline and record what it reports, as it reports it."""
    import main   # deferred: main imports this module

    async def go():
        stages = []
        result = None
        error = None
        async for frame in main.run_pipeline(filename, pdf_bytes,
                                             uploaded_by=uploaded_by, source=source,
                                             portal_client=portal_client):
            if not frame.startswith("data: "):
                continue
            try:
                payload = json.loads(frame[len("data: "):].strip())
            except (ValueError, TypeError):
                continue
            kind = payload.get("type")
            if kind == "stage":
                stages.append(payload["stage"])
                # Written per stage so a browser that reconnects mid-run sees
                # the same progress it would have watched. Never allowed to
                # fail the run: this is a progress report, and losing one is a
                # cosmetic loss against an invoice that still gets read.
                try:
                    storage.record_job_stages(job_id, stages)
                except Exception:
                    pass
            elif kind == "final":
                result = payload.get("result")
            elif kind == "error":
                error = payload.get("error") or "processing error"
        return stages, result, error

    stages, result, error = asyncio.run(go())

    if error:
        storage.fail_processing_job(job_id, error, stages=stages)
        return
    if not result or result.get("run_id") is None:
        # The generator finished without committing a run. Reported as a
        # failure rather than as a completion with nothing behind it.
        storage.fail_processing_job(
            job_id, "Processing finished without producing a result.", stages=stages)
        return

    storage.complete_processing_job(
        job_id,
        run_id=result["run_id"],
        run_status=result.get("status"),
        result=result,
        stages=result.get("stages") or stages,
    )
