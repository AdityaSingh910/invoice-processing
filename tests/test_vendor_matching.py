"""Vendor name normalisation and matching.

THE GAP THIS CLOSES

`find_vendor` matched bidirectional raw substrings. Measured against the seed
list before this change:

    'Acme'                     -> Acme Office Supplies
    'Office'                   -> Acme Office Supplies
    'Supplies'                 -> Acme Office Supplies
    's'                        -> Acme Office Supplies      <- one letter
    'Initech Consulting Group' -> Initech Consulting
    'Stark   Industrial Parts' -> None                      <- double space

Loose exactly where it was dangerous, strict exactly where it was not. A single
letter resolved to an approved vendor and unlocked that vendor's POs; a real
vendor name with a stray space did not resolve at all.

THE APPROACH

Normalise both sides, then require an exact match on the normalised form. No
substring fallback and no edit-distance scoring -- both would reintroduce the
"close enough" behaviour that caused the problem. Normalisation only collapses
differences that are genuinely cosmetic: case, whitespace, punctuation, "&" vs
"and", and legal-form abbreviations.

Three outcomes, and the distinction between them is the point: exactly one match
is confident, zero is confidently not approved (reject), and more than one is
ambiguous and belongs to a human (review).
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
TESTS = os.path.dirname(os.path.abspath(__file__))
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)

import rules       # noqa: E402
import storage     # noqa: E402
import pg_schema   # noqa: E402

norm = storage.normalize_vendor_name


@pytest.fixture
def db(monkeypatch):
    """Seed vendors: Acme Office Supplies, Globex Logistics, Initech Consulting,
    Stark Industrial Parts, Umbrella Cleaning Co."""
    schema = pg_schema.fresh_schema(monkeypatch)
    yield schema
    pg_schema.drop_schema(schema)


def add_vendor(name, vendor_id="V-900", status="approved"):
    conn = storage.get_conn()
    conn.execute("INSERT INTO vendors VALUES (%s,%s,%s)", (name, vendor_id, status))
    conn.commit()
    conn.close()


def check(name):
    return rules.vendor_check({"vendor_name": name})


# --------------------------------------------------------------------------
# A-D. normalisation collapses cosmetic differences
# --------------------------------------------------------------------------

def test_identical_names_match():
    assert norm("ABC Corporation") == norm("ABC Corporation")


@pytest.mark.parametrize("a,b", [
    ("ABC Corp.", "ABC Corporation"),
    ("ABC Inc", "ABC Incorporated"),
    ("ABC Ltd.", "ABC Limited"),
    ("ABC Co", "ABC Company"),
    ("ABC Corp. of America", "ABC Corporation of America"),   # not only the last token
])
def test_legal_suffix_variations_match(a, b):
    assert norm(a) == norm(b), f"{a!r} and {b!r} should normalise the same"


@pytest.mark.parametrize("a,b", [
    ("ACME, INC.", "Acme Inc"),
    ("acme office supplies", "ACME OFFICE SUPPLIES"),
    ("O'Brien & Sons", "OBrien and Sons"),
    ("Smith-Jones Ltd", "Smith Jones Limited"),
])
def test_case_and_punctuation_variations_match(a, b):
    assert norm(a) == norm(b)


@pytest.mark.parametrize("a,b", [
    ("Stark   Industrial Parts", "Stark Industrial Parts"),
    ("  Acme Office Supplies  ", "Acme Office Supplies"),
    ("Acme\tOffice  Supplies", "Acme Office Supplies"),
])
def test_whitespace_variations_match(a, b):
    assert norm(a) == norm(b)


def test_ampersand_and_the_word_and_are_equivalent():
    assert norm("Smith & Sons Ltd") == norm("Smith and Sons Limited")


# --------------------------------------------------------------------------
# E. genuinely different vendors must not match
# --------------------------------------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("ABC Supplies", "XYZ Supplies"),
    ("ABC Corp", "ABC Inc"),          # different legal entities
    ("Acme Office Supplies", "Acme Office Services"),
    ("Globex Logistics", "Globex Consulting"),
])
def test_different_vendors_do_not_match(a, b):
    assert norm(a) != norm(b), f"{a!r} and {b!r} must stay distinct"


def test_unknown_vendor_is_confidently_rejected(db):
    """Existing behaviour: a name that IS readable and is NOT on the list rejects."""
    ok, row, detail = check("XYZ Supplies Ltd")
    assert ok is False and row is None
    assert "not on the approved vendor list" in detail


# --------------------------------------------------------------------------
# F. the dangerous substring cases are gone
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["Acme", "Office", "Supplies", "s", "Consulting",
                                  "Acme Office Supplies Holdings International"])
def test_substrings_no_longer_match_an_approved_vendor(db, name):
    """Every one of these resolved to an approved vendor before this change."""
    assert storage.find_vendor(name) is None, f"{name!r} must not match by substring"


def test_a_substring_vendor_does_not_auto_approve(db):
    """"Acme" is not "Acme Office Supplies". Treating it as such approves an
    invoice from an entity nobody vetted, and unlocks that vendor's POs."""
    ok, _, detail = check("Acme")
    assert ok is False
    assert "not on the approved vendor list" in detail


def test_a_longer_name_containing_an_approved_vendor_does_not_match(db):
    """"Initech Consulting Group" is a different entity from "Initech
    Consulting" and previously matched it."""
    assert storage.find_vendor("Initech Consulting Group") is None


# --------------------------------------------------------------------------
# G. ambiguity -> review, not approval and not rejection
# --------------------------------------------------------------------------

def test_two_vendors_normalising_alike_is_ambiguous(db):
    """"Acme Corp." and "Acme Corporation" as two separate approved rows.

    The name IS on the list -- we just cannot say which row is meant, and picking
    one is a guess about who gets paid. That is review, not rejection.
    """
    add_vendor("Acme Corp.", "V-901")
    add_vendor("Acme Corporation", "V-902")

    matches = storage.find_vendor_matches("Acme Corp")
    assert len(matches) == 2

    ok, row, detail = check("Acme Corp")
    assert ok is None, "ambiguity must be the review state, not the reject state"
    assert row is None
    assert "matches 2 approved vendors" in detail
    assert "V-901" in detail and "V-902" in detail


def test_find_vendor_returns_none_when_ambiguous(db):
    """The single-result helper must not silently pick one."""
    add_vendor("Acme Corp.", "V-901")
    add_vendor("Acme Corporation", "V-902")
    assert storage.find_vendor("Acme Corp") is None


def test_ambiguity_does_not_reject(db):
    """Regression guard: routing ambiguity to REJECTED would be wrong, since the
    vendor is very likely legitimate."""
    add_vendor("Acme Corp.", "V-901")
    add_vendor("Acme Corporation", "V-902")
    ok, _, _ = check("Acme Corp")
    assert ok is not False


# --------------------------------------------------------------------------
# H/I. existing behaviour preserved
# --------------------------------------------------------------------------

def test_every_seeded_vendor_still_matches_itself(db):
    for v in storage.list_vendors():
        found = storage.find_vendor(v["vendor_name"])
        assert found is not None and found["vendor_id"] == v["vendor_id"], \
            f"{v['vendor_name']!r} must still match itself"
        assert check(v["vendor_name"])[0] is True


@pytest.mark.parametrize("variant", [
    "acme office supplies",
    "ACME OFFICE SUPPLIES",
    "Acme  Office  Supplies",
    "Acme Office Supplies.",
    " Acme Office Supplies ",
])
def test_cosmetic_variants_of_a_seeded_vendor_still_approve(db, variant):
    ok, row, _ = check(variant)
    assert ok is True and row["vendor_id"] == "V-001"


def test_umbrella_cleaning_company_matches_umbrella_cleaning_co(db):
    """The seed list has "Umbrella Cleaning Co"; an invoice may spell it out."""
    found = storage.find_vendor("Umbrella Cleaning Company")
    assert found is not None and found["vendor_id"] == "V-004"


def test_missing_vendor_name_is_still_review_not_reject(db):
    """Unchanged tri-state: unreadable is not the same as unapproved."""
    for value in (None, "", "   "):
        ok, row, detail = rules.vendor_check({"vendor_name": value})
        assert ok is None and row is None
        assert "No vendor name could be extracted" in detail


def test_a_vendor_on_file_but_not_approved_still_rejects(db):
    add_vendor("Suspended Supplies Ltd", "V-903", status="suspended")
    ok, row, detail = check("Suspended Supplies Limited")
    assert ok is False
    assert row is not None and "not approved" in detail


def test_normalisation_of_empty_and_punctuation_only_input(db):
    """Must not raise, and must not match everything."""
    assert norm("") == "" and norm(None) == "" and norm("...,,,") == ""
    assert storage.find_vendor_matches("...,,,") == []
    assert storage.find_vendor_matches("") == []
