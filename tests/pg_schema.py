"""Per-test Postgres schema isolation.

WHAT THIS REPLACES

Every `db(tmp_path, monkeypatch)` fixture in this suite used to do:

    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "xyz.db"))
    storage.init_db(reset_runs=True)

-- a fresh SQLite FILE per test, isolated for free because pytest's `tmp_path`
is unique per test and nothing else on the machine could see it. Postgres has
no per-test-file equivalent; the same isolation ("this test's data cannot
collide with or be seen by any other test") is recreated here with a fresh,
uniquely-named SCHEMA per test inside one shared database, dropped again on
teardown.

WHY A SHARED HELPER RATHER THAN 12 COPIES OF THE SAME LOGIC

The old fixture body was ~3 lines and easy to hand-copy across files. This one
needs a teardown step (SQLite never did, because pytest's own tmp_path
retention policy cleaned up the files; a Postgres schema left behind
accumulates forever across test runs unless something drops it), which is
enough extra moving parts that 12 hand-copies would drift. One helper here,
called from each fixture, keeps them identical by construction.
"""
import uuid

import config
import storage


def fresh_schema(monkeypatch, reset_runs: bool = True) -> str:
    """Point storage at a brand-new, empty schema and initialise it.

    Returns the schema name (the old fixtures returned `storage.DB_PATH`;
    callers that used that return value for logging/display can use this the
    same way).
    """
    config.load_dotenv()
    name = f"test_{uuid.uuid4().hex[:16]}"
    monkeypatch.setattr(storage, "PG_SCHEMA", name)
    storage.init_db(reset_runs=reset_runs)
    return name


def drop_schema(name: str):
    """Teardown: drop the schema and everything in it.

    CASCADE, not a plain DROP -- the schema holds tables with a foreign key
    between them (run_allocations -> runs), so an unqualified DROP SCHEMA
    would refuse to remove a non-empty schema.

    Uses a connection OUTSIDE the test's own PG_SCHEMA (search_path does not
    matter for a fully-qualified DROP SCHEMA statement), so this still works
    correctly even after monkeypatch has already reverted PG_SCHEMA back to
    whatever it was before the test.
    """
    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        conn.commit()
    finally:
        conn.close()
