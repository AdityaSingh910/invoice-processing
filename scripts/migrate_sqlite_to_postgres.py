"""One-time import: copy run history from the old SQLite database into Postgres.

WHAT THIS DOES AND DOES NOT COPY

Only `runs` and `run_allocations`. Purchase orders, vendors and users are seed
data owned by data/*.json and are reloaded into Postgres from those files by
storage.init_db() on every startup -- copying them from SQLite would just be
copying a stale snapshot of the same files. The extraction_quota counter is
also skipped: it is a calendar-day budget that resets naturally, and importing
a partial day's count from the old database would just make today's budget
look more spent than it is.

WHY THIS IS SAFE TO RUN MORE THAN ONCE

Every row insert checks whether a row with the same identity already exists in
Postgres (by run id) and skips it. Running this script twice against the same
source database is a no-op the second time, not a duplicate-runs bug.

Usage:

    .\\venv\\Scripts\\python.exe scripts\\migrate_sqlite_to_postgres.py [path-to-old-app.db]

Defaults to data/app.db. Requires DATABASE_URL to be set (same variable the
app itself reads) and the Postgres schema to already exist -- run the app
once first (which calls storage.init_db()), or run
`python -c "import storage; storage.init_db()"` from backend/, before this.
"""
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import config   # noqa: E402
import storage  # noqa: E402


def migrate(sqlite_path: str):
    if not os.path.isfile(sqlite_path):
        print(f"No SQLite database at {sqlite_path} -- nothing to migrate.")
        return

    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row

    tables = {r["name"] for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "runs" not in tables:
        print(f"{sqlite_path} has no 'runs' table -- nothing to migrate.")
        src.close()
        return

    runs = [dict(r) for r in src.execute("SELECT * FROM runs ORDER BY id ASC")]
    allocations = (
        [dict(r) for r in src.execute("SELECT * FROM run_allocations ORDER BY run_id, seq")]
        if "run_allocations" in tables else []
    )
    src.close()

    if not runs:
        print("No runs in the source database -- nothing to migrate.")
        return

    print(f"Found {len(runs)} run(s) and {len(allocations)} allocation row(s) in {sqlite_path}.")

    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM runs")
            existing_ids = {r["id"] for r in cur.fetchall()}

            imported_runs = 0
            # Columns explicit and matched by name on both sides -- a positional
            # VALUES(...) across two different schemas (SQLite's, Postgres's) is
            # exactly the kind of fragile the rest of this codebase avoids.
            run_cols = [
                "id", "filename", "status", "created_at", "vendor_name",
                "invoice_number", "total", "po_number", "extracted_json",
                "po_match_json", "stages_json", "reasons_json", "audit_json",
                "automated_decision", "human_decision", "final_decision",
                "reviewed_by", "reviewed_at", "review_note",
            ]
            for run in runs:
                if run["id"] in existing_ids:
                    continue
                values = [run.get(c) for c in run_cols]
                placeholders = ",".join(["%s"] * len(run_cols))
                cur.execute(
                    f"INSERT INTO runs ({','.join(run_cols)}) VALUES ({placeholders})",
                    values,
                )
                # Postgres's SERIAL sequence does not know about an explicitly
                # inserted id; advance it so the next auto-generated id does not
                # collide with an id this import just used.
                cur.execute(
                    "SELECT setval(pg_get_serial_sequence('runs', 'id'), %s, true)",
                    (run["id"],))
                imported_runs += 1

            imported_allocs = 0
            for alloc in allocations:
                if alloc["run_id"] not in {r["id"] for r in runs} and alloc["run_id"] not in existing_ids:
                    continue  # orphaned allocation row with no matching run; skip rather than fail
                cur.execute(
                    """INSERT INTO run_allocations (run_id, po_number, amount, seq)
                       SELECT %s, %s, %s, %s
                       WHERE NOT EXISTS (
                           SELECT 1 FROM run_allocations WHERE run_id=%s AND po_number=%s AND seq=%s
                       )""",
                    (alloc["run_id"], alloc["po_number"], alloc["amount"], alloc.get("seq", 0),
                     alloc["run_id"], alloc["po_number"], alloc.get("seq", 0)),
                )
                imported_allocs += cur.rowcount

        conn.commit()
    finally:
        conn.close()

    print(f"Imported {imported_runs} new run(s), {imported_allocs} new allocation row(s). "
          f"Already-present rows were skipped.")


if __name__ == "__main__":
    config.load_dotenv()
    src_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "data", "app.db")
    migrate(src_path)
