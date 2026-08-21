"""Production safety: environment separation, and the daily extraction breaker.

TWO SEPARATE CONCERNS THAT SHARE A THEME

Both are about things that work perfectly right up until they matter.

1. The demo configuration -- published passwords, an ephemeral signing key -- is
   exactly right for a case study on a laptop and exactly wrong anywhere else.
   It fails silently: the app runs, sign-in works, and it is simply insecure. So
   production refuses to START rather than warning about it.

2. The per-minute rate limit stops a runaway script but not steady, ordinary use
   quietly draining a provider's daily allowance. Gemini's free tier is 20
   requests per DAY on the only route that can read a scan.

No live provider call is made anywhere in this file.
"""
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

import auth         # noqa: E402
import config       # noqa: E402
import extraction   # noqa: E402
import quota        # noqa: E402
import storage      # noqa: E402
import pg_schema   # noqa: E402

TEXT_PDF = os.path.join(SAMPLES, "01_happy_path_acme.pdf")
SCANNED_PDF = os.path.join(SAMPLES, "05_scanned_no_text.pdf")


def read(path):
    with open(path, "rb") as f:
        return f.read()


@pytest.fixture
def db(monkeypatch):
    schema = pg_schema.fresh_schema(monkeypatch)
    yield schema
    pg_schema.drop_schema(schema)


@pytest.fixture
def prod(monkeypatch):
    """Pretend to be a production process."""
    monkeypatch.setenv(config.APP_ENV_VAR, "production")


@pytest.fixture
def real_users(tmp_path, monkeypatch):
    """A user store with no demo accounts, as a deployment would have."""
    import json
    path = tmp_path / "users.json"
    path.write_text(json.dumps([{
        "username": "a.singh", "roles": ["reviewer"],
        "password_hash": auth.hash_password("a-real-password"),
    }]), encoding="utf-8")
    monkeypatch.setenv("AUTH_USERS_FILE", str(path))
    return str(path)


# --------------------------------------------------------------------------
# 1. environment separation
# --------------------------------------------------------------------------

def test_the_default_environment_is_development(monkeypatch):
    monkeypatch.delenv(config.APP_ENV_VAR, raising=False)
    assert config.app_env() == "development"
    assert config.is_production() is False


@pytest.mark.parametrize("value", ["production", "prod", "live", "PRODUCTION", " Prod "])
def test_production_is_recognised_however_it_is_spelled(monkeypatch, value):
    monkeypatch.setenv(config.APP_ENV_VAR, value)
    assert config.is_production() is True


@pytest.mark.parametrize("value", ["development", "dev", "staging", "test", ""])
def test_anything_else_is_not_production(monkeypatch, value):
    monkeypatch.setenv(config.APP_ENV_VAR, value)
    assert config.is_production() is False


def test_development_never_reports_configuration_problems(monkeypatch):
    """The demo must keep working with no setup at all."""
    monkeypatch.delenv(config.APP_ENV_VAR, raising=False)
    monkeypatch.delenv(config.AUTH_SECRET_ENV, raising=False)
    assert auth.validate_production_config() == []
    auth.enforce_production_config()          # must not raise


# --------------------------------------------------------------------------
# 2. demo credentials cannot be used in production
# --------------------------------------------------------------------------

def test_the_shipped_user_store_is_marked_as_demo():
    """The flag is what the production check keys off, so it has to be there.

    Spelled out in full rather than counted, so that adding an account to
    data/users.json without the flag fails HERE -- at the one test whose job
    is to notice -- rather than in production, where the symptom is a
    published password quietly accepted.

    `acme` and `globex` are the Phase J supplier accounts. They are external
    client logins rather than employee ones, and they carry the same flag for
    exactly the same reason: their passwords are in this repository and on the
    sign-in screen.
    """
    assert auth.demo_usernames() == ["acme", "admin", "analyst", "globex",
                                     "reviewer", "viewer"]


def test_production_refuses_to_start_with_demo_credentials(monkeypatch, prod):
    monkeypatch.setenv(config.AUTH_SECRET_ENV, "a-real-secret-value-for-this-test")
    problems = auth.validate_production_config()
    assert any("demo credentials" in p for p in problems)

    with pytest.raises(RuntimeError) as exc:
        auth.enforce_production_config()
    assert "demo credentials" in str(exc.value)
    assert "reviewer" in str(exc.value), "it should name the offending accounts"


def test_moving_the_demo_file_does_not_launder_it(monkeypatch, prod, tmp_path):
    """The flag is on the RECORD, not the path -- copying the file elsewhere and
    pointing AUTH_USERS_FILE at it must not make it acceptable."""
    import json, shutil
    copied = tmp_path / "somewhere-else.json"
    shutil.copy(config.USERS_SEED, copied)
    monkeypatch.setenv("AUTH_USERS_FILE", str(copied))
    monkeypatch.setenv(config.AUTH_SECRET_ENV, "a-real-secret-value-for-this-test")

    assert any("demo credentials" in p for p in auth.validate_production_config())


def test_production_starts_with_a_real_user_store(monkeypatch, prod, real_users):
    monkeypatch.setenv(config.AUTH_SECRET_ENV, "a-real-secret-value-for-this-test")
    assert auth.validate_production_config() == []
    auth.enforce_production_config()          # must not raise


def test_an_empty_user_store_is_refused_in_production(monkeypatch, prod, tmp_path):
    empty = tmp_path / "none.json"
    empty.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("AUTH_USERS_FILE", str(empty))
    monkeypatch.setenv(config.AUTH_SECRET_ENV, "a-real-secret-value-for-this-test")
    assert any("nobody could sign in" in p for p in auth.validate_production_config())


def test_wildcard_cors_is_refused_in_production(monkeypatch, prod, real_users):
    monkeypatch.setenv(config.AUTH_SECRET_ENV, "a-real-secret-value-for-this-test")
    monkeypatch.setattr(config, "CORS_ORIGINS", ["*"])
    assert any("CORS_ORIGINS" in p for p in auth.validate_production_config())


def test_demo_credentials_still_work_in_development(monkeypatch):
    monkeypatch.delenv(config.APP_ENV_VAR, raising=False)
    monkeypatch.delenv("AUTH_USERS_FILE", raising=False)
    assert auth.authenticate_user("reviewer", "demo-reviewer") is not None


# --------------------------------------------------------------------------
# 3. the signing secret is mandatory in production
# --------------------------------------------------------------------------

def test_production_refuses_to_start_without_a_signing_secret(monkeypatch, prod, real_users):
    monkeypatch.delenv(config.AUTH_SECRET_ENV, raising=False)
    problems = auth.validate_production_config()
    assert any(config.AUTH_SECRET_ENV in p for p in problems)
    with pytest.raises(RuntimeError):
        auth.enforce_production_config()


def test_production_never_signs_with_an_ephemeral_key(monkeypatch, prod):
    """The second gate, in case the process was started some other way."""
    monkeypatch.delenv(config.AUTH_SECRET_ENV, raising=False)
    monkeypatch.setattr(auth, "_RUNTIME_SECRET", None)
    with pytest.raises(RuntimeError) as exc:
        auth.signing_secret()
    assert config.AUTH_SECRET_ENV in str(exc.value)


def test_production_uses_the_configured_secret(monkeypatch, prod):
    monkeypatch.setenv(config.AUTH_SECRET_ENV, "the-real-one")
    assert auth.signing_secret() == "the-real-one"


def test_development_still_generates_an_ephemeral_key(monkeypatch):
    monkeypatch.delenv(config.APP_ENV_VAR, raising=False)
    monkeypatch.delenv(config.AUTH_SECRET_ENV, raising=False)
    monkeypatch.setattr(auth, "_RUNTIME_SECRET", None)
    key = auth.signing_secret()
    assert key and auth.signing_secret() == key, "it must be stable within a process"


def test_all_production_problems_are_reported_at_once(monkeypatch, prod):
    """Being told about one, restarting, then being told about the next is a
    poor way to learn this."""
    monkeypatch.delenv(config.AUTH_SECRET_ENV, raising=False)
    problems = auth.validate_production_config()
    assert len(problems) >= 2


# --------------------------------------------------------------------------
# 4. daily extraction quota -- the counter
# --------------------------------------------------------------------------

def test_the_budget_allows_exactly_its_limit(db, monkeypatch):
    monkeypatch.setattr(config, "DAILY_QUOTA_VISION", 3)
    assert [quota.try_consume(quota.VISION) for _ in range(5)] == \
           [True, True, True, False, False]


def test_usage_is_reported_accurately(db, monkeypatch):
    monkeypatch.setattr(config, "DAILY_QUOTA_VISION", 5)
    quota.try_consume(quota.VISION)
    quota.try_consume(quota.VISION)
    st = quota.status(quota.VISION)
    assert (st["used"], st["limit"], st["remaining"]) == (2, 5, 3)


def test_the_two_providers_have_separate_budgets(db, monkeypatch):
    monkeypatch.setattr(config, "DAILY_QUOTA_VISION", 1)
    monkeypatch.setattr(config, "DAILY_QUOTA_TEXT", 3)
    assert quota.try_consume(quota.VISION) is True
    assert quota.try_consume(quota.VISION) is False
    assert quota.try_consume(quota.TEXT) is True, "text must not share the vision budget"


def test_the_count_survives_a_restart(db, monkeypatch):
    """The budget is a calendar-day quantity. An in-process counter would hand
    out a fresh allowance every time uvicorn reloaded."""
    monkeypatch.setattr(config, "DAILY_QUOTA_VISION", 2)
    quota.try_consume(quota.VISION)
    quota.try_consume(quota.VISION)

    import importlib
    importlib.reload(quota)          # a new process would start here
    monkeypatch.setattr(config, "DAILY_QUOTA_VISION", 2)
    assert quota.try_consume(quota.VISION) is False
    assert quota.used(quota.VISION) == 2


def test_yesterdays_usage_does_not_count_against_today(db, monkeypatch):
    monkeypatch.setattr(config, "DAILY_QUOTA_VISION", 2)
    conn = storage.get_conn()
    quota._ensure_table(conn)
    conn.execute("INSERT INTO extraction_quota (day, provider, used) VALUES (%s,%s,%s)",
                 ("2020-01-01", quota.VISION, 99))
    conn.commit(); conn.close()

    assert quota.try_consume(quota.VISION) is True


def test_a_zero_budget_disables_the_provider(db, monkeypatch):
    monkeypatch.setattr(config, "DAILY_QUOTA_VISION", 0)
    assert quota.try_consume(quota.VISION) is False


def test_the_budget_can_be_switched_off(db, monkeypatch):
    monkeypatch.setattr(config, "DAILY_QUOTA_ENABLED", False)
    monkeypatch.setattr(config, "DAILY_QUOTA_VISION", 1)
    assert all(quota.try_consume(quota.VISION) for _ in range(10))


def test_a_broken_counter_does_not_stop_extraction(db, monkeypatch):
    """Fail-open, deliberately. This is a cost guard, not a security control;
    refusing to process invoices because a bookkeeping table would not open
    would be a self-inflicted outage."""
    def boom(*a, **k):
        raise RuntimeError("database is on fire")

    monkeypatch.setattr(storage, "write_txn", boom)
    assert quota.try_consume(quota.VISION) is True


# --------------------------------------------------------------------------
# 5. the breaker, wired into extraction
# --------------------------------------------------------------------------

class FakeGroq:
    def __init__(self):
        self.calls = 0
        outer = self

        class Completions:
            def create(self, **kw):
                outer.calls += 1
                raise AssertionError("the provider must not be called past its budget")

        self.chat = type("C", (), {"completions": Completions()})()


@pytest.fixture
def both_providers(monkeypatch):
    monkeypatch.setattr(config, "has_groq_key", lambda: True)
    monkeypatch.setattr(config, "has_api_key", lambda: True)
    monkeypatch.setattr(config, "groq_api_key", lambda: "k")
    monkeypatch.setattr(config, "api_key", lambda: "k")


def test_an_exhausted_vision_budget_never_calls_the_provider(db, monkeypatch, both_providers):
    called = {"n": 0}

    def must_not_run(*a, **k):
        called["n"] += 1
        raise AssertionError("provider called with no budget left")

    monkeypatch.setattr(config, "DAILY_QUOTA_VISION", 0)
    monkeypatch.setattr(extraction, "llm_extract_vision", must_not_run)

    inv, info = extraction.extract_invoice(read(SCANNED_PDF))

    assert called["n"] == 0
    assert info["route"] == "none"
    assert info["quota_exhausted"] == quota.VISION


def test_an_exhausted_vision_budget_fails_safely(db, monkeypatch, both_providers):
    """The existing safe path: no fields invented, so the invoice reaches a human."""
    import matching, rules
    monkeypatch.setattr(config, "DAILY_QUOTA_VISION", 0)

    inv, info = extraction.extract_invoice(read(SCANNED_PDF))
    assert inv.total is None and inv.invoice_number is None

    extracted = inv.to_dict()
    status, _ = rules.decide(
        info, rules.validate_required_fields(extracted), True, "vendor approved",
        None, "", matching.empty_match(None),
        arithmetic=rules.validate_arithmetic(extracted),
        amount=rules.validate_amount(extracted))
    assert status == "NEEDS_REVIEW", "an unread scan must never be approved"


def test_an_exhausted_text_budget_falls_back_to_regex(db, monkeypatch, both_providers):
    fake = FakeGroq()
    monkeypatch.setattr(config, "DAILY_QUOTA_TEXT", 0)
    monkeypatch.setattr(extraction, "_groq_client", lambda: fake)

    inv, info = extraction.extract_invoice(read(TEXT_PDF))

    assert fake.calls == 0
    assert info["route"] == "regex"
    assert info["quota_exhausted"] == quota.TEXT
    assert inv.invoice_number == "INV-2201", "regex still reads the invoice"


def test_the_run_trail_explains_why_the_provider_was_skipped(db, monkeypatch, both_providers):
    monkeypatch.setattr(config, "DAILY_QUOTA_VISION", 0)
    _, info = extraction.extract_invoice(read(SCANNED_PDF))

    notes = " ".join(info["notes"])
    assert "daily budget" in notes
    assert "held for a human" in notes
    # The misleading "set GEMINI_API_KEY" advice must not appear: the key is
    # present and working, and sending an operator to check it wastes their time.
    assert "GEMINI_API_KEY" not in notes


def test_spending_the_text_budget_leaves_vision_alone(db, monkeypatch, both_providers):
    """The budgets are independent, so a busy day of text invoices must not
    disable the route that has no alternative."""
    monkeypatch.setattr(config, "DAILY_QUOTA_TEXT", 1)
    monkeypatch.setattr(config, "DAILY_QUOTA_VISION", 1)
    monkeypatch.setattr(extraction, "_groq_client", lambda: FakeGroq())

    quota.try_consume(quota.TEXT)
    assert quota.try_consume(quota.TEXT) is False
    assert quota.status(quota.VISION)["remaining"] == 1


def test_a_normal_run_consumes_exactly_one_request(db, monkeypatch, both_providers):
    import schemas

    monkeypatch.setattr(config, "DAILY_QUOTA_TEXT", 10)
    monkeypatch.setattr(extraction, "groq_extract_text",
                        lambda text, prompt=None: schemas.ExtractedInvoice(
                            invoice_number="INV-1", extraction_method="groq (text)"))

    extraction.extract_invoice(read(TEXT_PDF))
    assert quota.used(quota.TEXT) == 1
    assert quota.used(quota.VISION) == 0, "a text invoice must not touch the vision budget"


def test_a_provider_failure_still_spends_its_request(db, monkeypatch, both_providers):
    """The budget tracks requests SENT, not requests that succeeded -- the
    provider counts a failed call against its own quota too, so pretending
    otherwise would let a failing key burn the real allowance invisibly."""
    monkeypatch.setattr(config, "DAILY_QUOTA_TEXT", 10)
    monkeypatch.setattr(extraction, "groq_extract_text",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    _, info = extraction.extract_invoice(read(TEXT_PDF))
    assert info["route"] == "regex"
    assert quota.used(quota.TEXT) == 1
