# Refactoring Strategy — AP Decision Engine

Lead Systems Architect review · 2026-08-18 · companion to [AUDIT.md](AUDIT.md)

Design document. **No backend code has been changed** — Phase 1 remains gated
pending confirmation. Everything below is a pattern to implement against.

---

## 0. Three places I'd push back on the brief

Recording these first, because two of them change what gets built.

### 0.1 FX conversion must not silently widen auto-approval

The brief asks to "call a historical FX rate for the invoice date to normalize
before hitting TOLERANCE_CHECK." Normalising for *comparison* is right. But if a
converted amount can then reach `APPROVED` with no human involved, the process
now auto-authorises a payment whose correctness depends on a rate fetched from a
third party at run time. Change the rate source, or run the same invoice a day
later, and the verdict can flip.

That is a control weakness, not a feature. AUDIT.md deliberately scoped FX
conversion out for this reason.

**Recommendation:** convert, but treat conversion as *evidence for a reviewer*,
not as grounds for auto-approval. Default policy `on_mismatch: convert_and_review`.
Auto-approval across currencies stays available as an explicit opt-in
(`convert_and_allow`) requiring a pinned rate source, a max-age on the rate, and
a tight drift band. Every converted decision records the rate, its source, its
as-of date, and the pre-conversion amounts. Details in §3.2.

### 0.2 Pydantic `Field` is the wrong mechanism for confidence

`Field(...)` attaches metadata to a *schema* — it is fixed at class definition
time and identical for every instance. Confidence, evidence and page number are
*per-instance* data that differ for every invoice. They belong in the value, not
the schema.

Attempting `total: float = Field(..., json_schema_extra={"confidence": ...})`
cannot work: there is nowhere to put a per-invoice number.

**Recommendation:** a generic value wrapper, `Tracked[T]` (§2.1). Pydantic is
optional — a frozen dataclass with a `Generic[T]` parameter gives the same
guarantees with no dependency and cleaner serialisation. If validation of the
LLM payload is wanted, use Pydantic at the *parse boundary* only, then convert
into `Tracked` values.

### 0.3 A declarative rule engine is the wrong tool here

The brief suggests JSON-logic or a rule engine. I'd advise against it.

The logic that actually matters in this system is subtle in ways declarative DSLs
express badly: one-sided tolerance (`diff <= tol`, *not* `abs(diff) <= tol`),
balance derived from the run ledger, severity precedence, and the confidence gate
crossing field metadata. Encoding those as JSON-logic makes them harder to read,
much harder to test, and near-impossible to debug from a stack trace — and the
one bug that has already bitten this codebase twice was exactly a sign error in a
comparison.

Rule engines pay off when non-engineers author rules. Nobody does here.

**Recommendation:** split *policy* from *logic*. Thresholds, toggles, weights and
allowances go to versioned YAML (§5). Predicates stay as Python functions in a
registry keyed by `rule_id`, each receiving a typed policy slice. You get
versioning, auditability and hot-tunable thresholds without the opacity.

---

## 1. Bug fixes

### 1.1 Un-capped inferred PO match

**Present behaviour** (`matching.py:23-32`): when no PO reference is extracted,
pick the vendor's nearest-amount PO — no distance cap, no tie-breaking, no
content check. `rules.py` logs it at `warn`, which does not alter the verdict.
Auto-approval follows.

Three independent defects: no cap, no ambiguity handling, and a severity that
does nothing.

```python
# matching.py

import re
from typing import List, Optional

_TOKEN = re.compile(r"[a-z0-9]+")
_STOPWORDS = {"the", "and", "for", "of", "to", "a", "an", "inc", "ltd", "llc"}


def _tokens(text: str) -> set:
    return {t for t in _TOKEN.findall((text or "").lower())
            if len(t) > 2 and t not in _STOPWORDS}


def description_similarity(invoice_lines: List[dict], po_description: str) -> float:
    """Jaccard overlap between invoice line descriptions and the PO description.

    The PO table carries only a free-text `description` (no structured line
    items), so this is necessarily coarse. It is used as a *veto*, never as
    positive evidence: low overlap blocks an inferred match, high overlap does
    not by itself justify one.
    """
    inv = set()
    for li in invoice_lines or []:
        inv |= _tokens(li.get("description", ""))
    po = _tokens(po_description)
    if not inv or not po:
        return 0.0
    return len(inv & po) / len(inv | po)


def infer_po(candidates: List[dict], total: float, invoice_lines: List[dict],
             policy) -> tuple:
    """Returns (po_row | None, diagnostics).

    An inferred match must clear an absolute cap, a relative cap, a content
    similarity floor, and be unambiguously better than the runner-up.
    """
    diag = {"considered": len(candidates), "rejected": [], "ambiguous": False,
            "similarity": None, "distance": None}

    if not candidates or total is None:
        return None, diag

    scored = []
    for po in candidates:
        dist = abs(po["amount"] - total)
        rel = dist / po["amount"] if po["amount"] else 1.0
        sim = description_similarity(invoice_lines, po.get("description", ""))

        if dist > policy.inferred.max_absolute_distance:
            diag["rejected"].append((po["po_number"], "absolute distance"))
            continue
        if rel > policy.inferred.max_relative_distance:
            diag["rejected"].append((po["po_number"], "relative distance"))
            continue
        if sim < policy.inferred.min_description_similarity:
            diag["rejected"].append((po["po_number"], "description mismatch"))
            continue
        scored.append((dist, sim, po))

    if not scored:
        return None, diag

    scored.sort(key=lambda s: (s[0], -s[1]))
    best = scored[0]

    # Ambiguity: a runner-up of comparable closeness means we cannot claim to
    # know which PO this invoice belongs to. Guessing is worse than declining.
    if len(scored) > 1:
        runner_up = scored[1]
        if abs(runner_up[0] - best[0]) < policy.inferred.ambiguity_margin:
            diag["ambiguous"] = True
            return None, diag

    diag["distance"], diag["similarity"] = best[0], best[1]
    return best[2], diag
```

The verdict-side fix — the part that actually closes the leak:

```python
# rules.py, inside decide()

if po_match["matched_via"] == "inferred":
    if not policy.inferred.auto_approve:
        review = True                      # <-- the missing line
    add(
        f"No PO reference on the invoice. {po_match['po_number']} was inferred from "
        f"vendor, amount (within ${po_match['inferred_distance']:.2f}) and description "
        f"similarity {po_match['inferred_similarity']:.0%}. Inferred matches are not "
        f"auto-approved under policy {policy.version}.",
        "warn",
    )
```

Plus the ambiguity and no-match paths:

```python
elif po_match["matched_via"] == "ambiguous":
    review = True
    add("Two or more purchase orders match this invoice equally well — cannot "
        "determine which it belongs to. Needs a human to pick.", "fail")
```

**Recommended default: `auto_approve: false`.** An invoice that never named a PO
should always be seen by a person. The caps and similarity floor exist to
suppress *nonsense* suggestions, not to license auto-approval.

**Structural lesson:** the root cause was severity being decorative. `warn` must
either affect the verdict or not exist. Enforce it — a single function that maps
severity to verdict effect, so no future rule can log a concern that changes
nothing:

```python
SEVERITY_EFFECT = {"ok": None, "info": None, "warn": "review", "fail": "reject"}
```

...with any rule needing a different mapping stating so explicitly in policy,
never by hand-rolling its own `add()` call.

### 1.2 Currency never checked

Currency is extracted by both routes and read by neither. The PO table has a
`currency` column nobody consults.

Fix in two layers. **PO_MATCH** establishes comparability; **TOLERANCE_CHECK**
only ever compares like with like.

```python
# matching.py — inside match_po(), once po_row is resolved

inv_ccy = (extracted.get("currency") or "").upper() or None
po_ccy = (po_row.get("currency") or "").upper() or None

fx = {"required": False, "invoice_currency": inv_ccy, "po_currency": po_ccy,
      "rate": None, "rate_date": None, "rate_source": None,
      "original_total": total, "converted_total": None, "drift": None,
      "status": "same_currency"}

if inv_ccy and po_ccy and inv_ccy != po_ccy:
    fx["required"] = True
    fx["status"] = "mismatch"
    total, fx = apply_fx(total, inv_ccy, po_ccy, extracted.get("invoice_date"), fx, policy)
elif not inv_ccy:
    fx["status"] = "unknown_invoice_currency"
```

`total` used for tolerance is now guaranteed to be PO-currency, and `fx` carries
the full derivation for the trace. In `rules.decide()`:

```python
if fx["status"] == "unknown_invoice_currency":
    review = True
    add("Could not determine the invoice currency, so amounts cannot be safely "
        "compared to the PO. Confirm the currency before approving.", "fail")

elif fx["status"] == "mismatch_unconverted":
    review = True
    add(f"Invoice is in {fx['invoice_currency']}, PO {po_match['po_number']} is in "
        f"{fx['po_currency']}. No usable exchange rate, so no comparison was made.",
        "fail")

elif fx["status"] == "converted":
    if not policy.currency.auto_approve_converted:
        review = True
    add(f"Invoice {fx['original_total']:,.2f} {fx['invoice_currency']} converted to "
        f"{fx['converted_total']:,.2f} {fx['po_currency']} at {fx['rate']} "
        f"({fx['rate_source']}, {fx['rate_date']}). Cross-currency invoices are not "
        f"auto-approved under policy {policy.version}.", "warn")
```

Note the failure mode this closes: today a €3,000 invoice against a $5,000 PO is
compared as `3000` vs `5000` and approved as a partial invoice, when at ~1.08 it
is really ~$3,240 — still under, but the process had no idea and would have been
equally happy at a rate of 3.

---

## 2. Confidence and provenance (Phase 2)

The structural fix. Findings 3 and 5 in the audit both reduce to: extraction
discards how much it should be trusted, before the rules ever see it.

### 2.1 `Tracked[T]` — the value wrapper

```python
# schemas.py
from dataclasses import dataclass, field, asdict
from typing import Generic, Optional, Tuple, TypeVar, Any, Dict, List

T = TypeVar("T")

Source = str  # "llm-text" | "llm-vision" | "regex" | "derived" | "absent"


@dataclass(frozen=True)
class Tracked(Generic[T]):
    """One extracted value plus everything needed to judge and audit it.

    Immutable on purpose: a value's provenance must not be editable downstream.
    Deriving a new value from tracked inputs goes through `derive()`, which
    forces the caller to declare the derivation.
    """
    value: Optional[T] = None
    confidence: float = 0.0
    source: Source = "absent"
    evidence: Optional[str] = None      # verbatim text the value came from
    page: Optional[int] = None
    span: Optional[Tuple[int, int]] = None   # char offsets into raw_text
    note: Optional[str] = None          # e.g. "subtotal + tax"

    @property
    def present(self) -> bool:
        return self.value is not None

    @classmethod
    def absent(cls, note: str = None) -> "Tracked[T]":
        return cls(value=None, confidence=0.0, source="absent", note=note)

    @classmethod
    def derive(cls, value: T, inputs: List["Tracked"], note: str,
               penalty: float) -> "Tracked[T]":
        """A value computed rather than read. Confidence can only go down:
        it is the weakest input, further penalised for being inferred."""
        base = min([i.confidence for i in inputs], default=0.0)
        return cls(value=value, confidence=round(max(0.0, base - penalty), 3),
                   source="derived", evidence=None,
                   page=next((i.page for i in inputs if i.page is not None), None),
                   note=note)
```

This is what kills audit finding 5.1 — the synthesized total. Today:

```python
inv.total = round(inv.subtotal + inv.tax, 2)     # indistinguishable from read
```

Becomes:

```python
inv.total = Tracked.derive(
    round(inv.subtotal.value + inv.tax.value, 2),
    inputs=[inv.subtotal, inv.tax],
    note="no total found on document; computed as subtotal + tax",
    penalty=policy.confidence.derived_penalty,
)
```

The value still flows, the arithmetic still works — but it now arrives at the
rules carrying a lowered confidence and a printable explanation.

### 2.2 Keeping downstream contracts intact

The brief's constraint — don't break stage contracts — is met by making the
legacy flat view a *projection*:

```python
@dataclass
class ExtractedInvoice:
    fields: Dict[str, Tracked] = field(default_factory=dict)
    line_items: List[dict] = field(default_factory=list)
    raw_text: str = ""
    extraction_method: str = "regex"

    def get(self, name: str) -> Tracked:
        return self.fields.get(name, Tracked.absent())

    def value(self, name: str) -> Any:
        return self.get(name).value

    def to_dict(self) -> dict:
        """Backwards-compatible: flat values at the top level, exactly as before,
        with provenance alongside. Existing readers (storage, frontend,
        rules that only need the value) keep working unchanged."""
        d = {name: t.value for name, t in self.fields.items()}
        d["line_items"] = self.line_items
        d["raw_text"] = self.raw_text
        d["extraction_method"] = self.extraction_method
        d["_provenance"] = {name: asdict(t) for name, t in self.fields.items()}
        return d
```

`extracted["total"]` still returns a float. `storage.save_run` needs no change.
The frontend keeps rendering. Only code that *wants* provenance opts in. This
allows Phase 2 to land incrementally rather than as a big-bang rewrite.

### 2.3 Confidence per route

| Route | Basis | Typical |
|---|---|---|
| `llm-text` | model-reported per field, clamped | 0.85–0.98 |
| `llm-vision` | model-reported, multiplied by a route penalty | 0.60–0.85 |
| `regex` | per-pattern tier, assigned statically | see below |
| `derived` | weakest input − penalty | ≤ 0.6 |
| `absent` | — | 0.0 |

For regex, confidence comes from *which pattern tier matched*, which the
extractor already knows:

```python
PATTERN_TIERS = {
    "anchored_label": 0.92,   # "^Invoice #: (...)"  — labelled, line-anchored
    "loose_label":    0.78,   # label anywhere on the line
    "bare_pattern":   0.55,   # e.g. /\bINV-\d+\b/ with no label
    "positional":     0.35,   # _guess_vendor: "first line that looks like a name"
}
```

`_guess_vendor` scoring 0.35 is the honest number, and it is what stops audit
finding 5.4 from reaching auto-approval.

For the LLM, extend the existing JSON schema — no extra call:

```json
{"total": {"value": 3000.00, "confidence": 0.96,
           "evidence": "Total Due: $3,000.00", "page": 1}}
```

with a prompt instruction that confidence must reflect *legibility and
unambiguity*, and that a value inferred rather than read must score below 0.5.

### 2.4 The confidence gate

Inserted as a rule inside `DECISION`, reading policy, and — critically —
evaluated **independently of whether the numeric checks passed**:

```python
# rules.py

# Which fields can change the verdict. Gate only these; a low-confidence
# invoice_date should not block a payment that is otherwise sound.
MATERIAL_FIELDS = ("vendor_name", "invoice_number", "total")


def confidence_gate(extracted: "ExtractedInvoice", po_match: dict, policy):
    """Force review when a field the verdict *depends on* is weakly extracted.

    This is the rule that stops audit finding 5 as a class: previously, a value
    only had to be present and numerically plausible. Now it must also be
    trustworthy.
    """
    findings = []
    for name in MATERIAL_FIELDS:
        t = extracted.get(name)
        threshold = policy.confidence.threshold_for(name)

        if not t.present:
            continue                      # absence is REQUIRED_FIELDS' job

        if t.confidence < threshold:
            findings.append({
                "rule_id": "confidence.material_field",
                "field": name, "confidence": t.confidence,
                "threshold": threshold, "source": t.source,
                "evidence": t.evidence,
                "text": (f"{name.replace('_',' ').title()} was extracted with "
                         f"{t.confidence:.0%} confidence via {t.source} "
                         f"(policy requires {threshold:.0%})"
                         + (f" — {t.note}" if t.note else "")
                         + (f". Read from: “{t.evidence}”" if t.evidence else "")
                         + ". Verify before approving."),
                "level": "fail",
            })

    # A derived total is never sufficient on its own for auto-approval, however
    # close the arithmetic lands to the PO.
    total = extracted.get("total")
    if total.source == "derived" and not policy.confidence.allow_derived_total:
        findings.append({
            "rule_id": "confidence.derived_total",
            "field": "total", "confidence": total.confidence,
            "text": (f"The invoice total was not read from the document — it was "
                     f"computed ({total.note}). Confirm against the PDF."),
            "level": "fail",
        })
    return findings
```

Wire into `decide()` so it can only ever *tighten* the verdict:

```python
for f in confidence_gate(extracted, po_match, policy):
    review = True
    add(f["text"], f["level"])
```

**Test that proves it works** (the Phase 2 exit criterion): an invoice whose
total is legible enough to extract and lands *within* PO tolerance, but was read
by a low-tier pattern — must come out `NEEDS_REVIEW`, with the reason naming
confidence rather than any arithmetic failure.

---

## 3. Non-trivial edge case logic

### 3.1 Multi-PO consolidation

One invoice covering several open POs for the same vendor. Today `match_po`
returns a single PO and the excess reads as an over-tolerance breach.

The correct model is an *allocation*, not a match. Two properties matter: the
allocation must be explainable line by line, and consolidation must never be
auto-approved on amount arithmetic alone.

```python
# matching.py

def allocate_across_pos(extracted, candidates: List[dict], policy) -> dict:
    """Attempt to explain the invoice total as a sum across several open POs.

    Only attempted when a single-PO match has already failed, and only when the
    invoice explicitly references more than one PO, or policy enables amount-
    based consolidation. Greedy largest-first: with few POs per vendor this is
    adequate, and unlike subset-sum it always terminates and is explainable.
    """
    total = extracted.value("total")
    refs = [r for r in extracted.value("po_references") or []]

    pool = [po for po in candidates if po["status"] == "open"]
    if refs:
        pool = [po for po in pool if po["po_number"] in refs] or pool
    elif not policy.consolidation.allow_without_references:
        return {"consolidated": False, "reason": "no explicit multi-PO reference"}

    if len(pool) < 2:
        return {"consolidated": False, "reason": "fewer than two candidate POs"}

    pool.sort(key=lambda p: p["remaining"], reverse=True)

    allocations, residual = [], total
    for po in pool:
        if residual <= 0:
            break
        take = min(po["remaining"], residual)
        if take <= 0:
            continue
        allocations.append({"po_number": po["po_number"], "allocated": round(take, 2),
                            "po_remaining_before": po["remaining"],
                            "po_remaining_after": round(po["remaining"] - take, 2)})
        residual = round(residual - take, 2)

    tol = policy.tolerance.for_amount(total)
    return {
        "consolidated": True,
        "allocations": allocations,
        "pos_used": len(allocations),
        "unallocated": residual,
        "fully_allocated": residual <= tol,
        "tolerance": tol,
    }
```

Verdict handling:

```python
if alloc.get("consolidated"):
    detail = ", ".join(f"{a['po_number']} ${a['allocated']:,.2f}"
                       for a in alloc["allocations"])
    if not alloc["fully_allocated"]:
        review = True
        add(f"Invoice spans {alloc['pos_used']} purchase orders ({detail}) but "
            f"${alloc['unallocated']:,.2f} could not be allocated to any open PO.",
            "fail")
    else:
        if not policy.consolidation.auto_approve:
            review = True
        add(f"Invoice consolidates {alloc['pos_used']} purchase orders: {detail}. "
            f"Consolidated invoices are not auto-approved under policy "
            f"{policy.version}.", "warn")
```

**Ledger consequence, and the reason this is not a display-only change:** the
current schema stores exactly one `po_number` per run, and
`consumed_amount_for_po` sums `total` for runs matching that PO. A consolidated
invoice would over-consume every PO it touched. This requires a new
`run_allocations(run_id, po_number, amount)` table, with
`consumed_amount_for_po` summing *allocations* rather than run totals. Single-PO
runs become a one-row allocation — the same code path, no special case.

### 3.2 FX lookup and drift

Per §0.1, conversion informs the reviewer; it does not by default widen
auto-approval.

```python
# fx.py

from datetime import date, timedelta

class RateUnavailable(Exception):
    pass


class FxProvider:
    """Rates must be reproducible: same invoice, same rate, forever.

    Live rates are cached into `fx_rates` on first use and read from cache
    thereafter, so a re-run of a historical invoice cannot silently reprice.
    """
    def __init__(self, conn, policy):
        self.conn, self.policy = conn, policy

    def rate(self, base: str, quote: str, as_of: date) -> dict:
        cached = self._from_cache(base, quote, as_of)
        if cached:
            return cached
        if not self.policy.currency.fx.allow_live_lookup:
            raise RateUnavailable(f"No cached {base}/{quote} rate for {as_of}")
        fetched = self._fetch(base, quote, as_of)     # provider call
        self._cache(fetched)
        return fetched

    def _from_cache(self, base, quote, as_of):
        row = self.conn.execute(
            """SELECT * FROM fx_rates WHERE base=? AND quote=?
               AND rate_date <= ? ORDER BY rate_date DESC LIMIT 1""",
            (base, quote, as_of.isoformat())).fetchone()
        if not row:
            return None
        age = (as_of - date.fromisoformat(row["rate_date"])).days
        if age > self.policy.currency.fx.max_rate_age_days:
            return None
        return dict(row)


def apply_fx(total, inv_ccy, po_ccy, invoice_date, fx, policy, provider):
    """Convert invoice total into PO currency. Returns (converted_total, fx)."""
    try:
        as_of = date.fromisoformat(invoice_date) if invoice_date else date.today()
    except (TypeError, ValueError):
        as_of = date.today()
        fx["note"] = "invoice date unusable; used today's rate"

    try:
        r = provider.rate(inv_ccy, po_ccy, as_of)
    except RateUnavailable as exc:
        fx["status"] = "mismatch_unconverted"
        fx["note"] = str(exc)
        return total, fx          # unconverted; rules will force review

    converted = round(total * r["rate"], 2)
    fx.update({"rate": r["rate"], "rate_date": r["rate_date"],
               "rate_source": r["source"], "converted_total": converted,
               "status": "converted"})

    # Drift: how far the rate has moved since the PO was raised. A large move
    # means the PO's authorised amount no longer reflects today's economics —
    # a commercial question for a human, not an arithmetic one.
    po_rate = provider_safe_rate(provider, inv_ccy, po_ccy, policy.po_issued_date)
    if po_rate:
        drift = abs(r["rate"] - po_rate) / po_rate
        fx["drift"] = round(drift, 4)
        fx["drift_exceeded"] = drift > policy.currency.fx.max_drift
    return converted, fx
```

Drift handling in the verdict:

```python
if fx.get("drift_exceeded"):
    review = True
    add(f"The {fx['invoice_currency']}/{fx['po_currency']} rate has moved "
        f"{fx['drift']:.1%} since this PO was raised — beyond the "
        f"{policy.currency.fx.max_drift:.0%} band. The PO amount may no longer "
        f"reflect the commercial agreement.", "warn")
```

**Caching is the important design point.** Without it, replaying a historical run
can produce a different verdict than the one recorded, which destroys the audit
trail. Rates are written once, keyed by `(base, quote, rate_date)`, and pinned
into the trace.

### 3.3 Unlisted surcharges

The scenario the current design handles worst: core goods match the PO exactly,
one unlisted $50 "service fee" pushes the total over tolerance, and the process
reports a blanket tolerance failure. The reviewer is told the invoice is wrong
when in fact 99% of it is provably right.

Fix: decompose the total before comparing, and let each component meet its own
rule.

```python
# tolerance.py

import re

CLASSIFIERS = [
    ("tax",       re.compile(r"\b(tax|vat|gst|igst|cgst|sgst)\b", re.I)),
    ("freight",   re.compile(r"\b(freight|shipping|delivery|carriage|postage)\b", re.I)),
    ("surcharge", re.compile(r"\b(fee|surcharge|handling|admin|processing|fuel|levy)\b", re.I)),
    ("discount",  re.compile(r"\b(discount|credit|rebate|adjustment)\b", re.I)),
]


def classify_line(description: str) -> str:
    for label, pattern in CLASSIFIERS:
        if pattern.search(description or ""):
            return label
    return "goods"


def decompose(extracted, policy) -> dict:
    buckets = {"goods": 0.0, "tax": 0.0, "freight": 0.0,
               "surcharge": 0.0, "discount": 0.0}
    detail = {k: [] for k in buckets}

    for li in extracted.line_items or []:
        amount = li.get("amount") or 0.0
        kind = classify_line(li.get("description"))
        buckets[kind] += amount
        detail[kind].append({"description": li.get("description"), "amount": amount})

    total = extracted.value("total") or 0.0
    itemised = sum(buckets.values())
    buckets["unattributed"] = round(total - itemised, 2)
    return {"buckets": {k: round(v, 2) for k, v in buckets.items()}, "detail": detail}


def check_tolerance(decomp, po_match, policy) -> dict:
    """Two independent verdicts: do the core goods reconcile to the PO, and are
    the non-goods components permitted? Reporting them separately is the whole
    point — 'core matches, one fee is unauthorised' is far more actionable than
    'total is over tolerance'."""
    b = decomp["buckets"]
    remaining = po_match["remaining_before"]
    findings = []

    goods_basis = b["goods"] + b["discount"]
    goods_tol = policy.tolerance.for_amount(remaining)
    goods_diff = round(goods_basis - remaining, 2)
    goods_ok = goods_diff <= goods_tol          # one-sided, as before

    findings.append({
        "rule_id": "tolerance.goods",
        "ok": goods_ok, "diff": goods_diff, "tolerance": goods_tol,
        "text": (f"Core goods ${goods_basis:,.2f} reconcile to the remaining PO "
                 f"balance ${remaining:,.2f} (diff ${goods_diff:,.2f})."
                 if goods_ok else
                 f"Core goods ${goods_basis:,.2f} exceed the remaining PO balance "
                 f"${remaining:,.2f} by ${goods_diff:,.2f}."),
        "level": "ok" if goods_ok else "fail",
    })

    # Ancillary components, each against its own allowance.
    for kind in ("tax", "freight", "surcharge"):
        amount = b[kind]
        if amount <= 0:
            continue
        allow = policy.ancillary.allowance_for(kind, basis=goods_basis,
                                               po=po_match)
        if allow.permitted and amount <= allow.cap:
            findings.append({
                "rule_id": f"ancillary.{kind}", "ok": True, "amount": amount,
                "text": f"{kind.title()} ${amount:,.2f} is within the "
                        f"${allow.cap:,.2f} allowance.",
                "level": "ok"})
        else:
            findings.append({
                "rule_id": f"ancillary.{kind}", "ok": False, "amount": amount,
                "text": (f"{kind.title()} of ${amount:,.2f} "
                         + (f"exceeds the ${allow.cap:,.2f} allowance"
                            if allow.permitted else
                            f"is not authorised on {po_match['po_number']}")
                         + f" — {', '.join(d['description'] for d in decomp['detail'][kind])}. "
                         f"The core goods on this invoice reconcile correctly; only "
                         f"this charge needs a decision."),
                "level": "fail"})

    if abs(b["unattributed"]) > policy.tolerance.max_unattributed:
        findings.append({
            "rule_id": "tolerance.unattributed", "ok": False,
            "amount": b["unattributed"],
            "text": (f"${b['unattributed']:,.2f} of the total could not be attributed "
                     f"to any line item. The line items may be incomplete."),
            "level": "fail"})
    return {"findings": findings,
            "ok": all(f["ok"] for f in findings if "ok" in f)}
```

The resulting reviewer message for the brief's scenario: *"Core goods $2,450.00
reconcile to the remaining PO balance. Service fee of $50.00 is not authorised on
PO-1004 — 'Expedited service fee'. The core goods on this invoice reconcile
correctly; only this charge needs a decision."* One decision, tightly scoped.

**Dependency:** this only works when line items are extracted reliably. The
current regex line-item pattern is weak; the LLM route handles it far better. If
line items are absent, `decompose` degrades to `unattributed == total` — so the
gate must be: no line items → fall back to whole-total tolerance, and say so.

### 3.4 Concurrent split-PO over-run

**The race is real and the current code is exposed to it.** Every `storage`
function opens its own connection and closes it (verified: `get_conn()` called at
lines 19, 79, 86, 93, 103, 119, 133, 144, 171). So a transaction cannot span
read-balance → decide → write. Two invoices for `PO-1002` processed concurrently
both read `remaining = $5,000`, both approve $3,000, and $6,000 is committed
against a $5,000 PO. Neither run is individually wrong; the ledger is.

Three things must change.

**(a) Connection lifecycle.** Storage functions accept an optional connection so
callers can compose a transaction:

```python
from contextlib import contextmanager

@contextmanager
def transaction(immediate: bool = False):
    """A unit of work. `immediate` takes the write lock up front, which is what
    makes read-then-write sequences safe under concurrency."""
    conn = sqlite3.connect(DB_PATH, timeout=policy.db.busy_timeout_s,
                           isolation_level=None)   # explicit transaction control
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
```

`BEGIN IMMEDIATE` acquires the RESERVED lock at statement one, so a second writer
blocks (up to `busy_timeout`) rather than reading stale state. WAL keeps readers
— the dashboard — unblocked throughout.

**(b) Keep expensive work outside the lock.** The critical section must not span
an LLM call:

```python
# main.py — pipeline restructured

extracted, info = extraction.extract_invoice(pdf_bytes, pre=...)   # seconds; no lock
candidates    = storage.candidate_pos(vendor)                      # read-only

with storage.transaction(immediate=True) as conn:                  # milliseconds
    consumed  = storage.consumed_amount_for_po(po_number, conn=conn)
    po_match  = matching.finalise(po_row, consumed, extracted, policy)
    status, reasons = rules.decide(...)
    if status == "APPROVED":
        # Re-validate under the lock: the balance may have moved since the
        # optimistic read used for the live stage display.
        if po_match["diff"] > po_match["tolerance"]:
            status = "NEEDS_REVIEW"
            reasons.append({"level": "fail", "rule_id": "concurrency.revalidate",
                            "text": "Another invoice consumed this PO while this "
                                    "one was being processed; re-checked under "
                                    "lock and it no longer fits."})
    run_id = storage.save_run(..., conn=conn)
```

The stage display can still show an optimistic balance for responsiveness; the
committed decision is the one taken under lock. That distinction should be
visible in the trace.

**(c) Idempotency.** A retried upload must not create a second consuming run:

```sql
ALTER TABLE runs ADD COLUMN idempotency_key TEXT;
CREATE UNIQUE INDEX ix_runs_idem ON runs(idempotency_key)
    WHERE idempotency_key IS NOT NULL;
```

keyed on `sha256(pdf_bytes)` + vendor + invoice number. An insert collision
returns the existing run rather than reprocessing.

**Honest scope note:** SQLite is single-writer. This design is correct for the
one-process deployment in play and will hold to modest volume. "Hundreds of
invoices a month" from the problem statement is comfortably inside that. If this
ever became multi-worker, the locking model should move to Postgres with
`SELECT ... FOR UPDATE` on the PO row — the transaction boundaries designed here
port directly.

---

## 4. Versioned policy: `rules.yaml`

```yaml
# policy/rules.yaml
version: 3
effective_from: "2026-08-18"
description: >
  Tolerances, matching rules and confidence gates for the AP decision engine.
  Every run records the version that produced it.

tolerance:
  relative: 0.02
  minimum_absolute: 25.00
  applies_to: remaining_balance      # remaining_balance | po_face_value
  direction: over_only               # over_only | symmetric
  max_unattributed: 1.00

confidence:
  default_minimum: 0.75
  derived_penalty: 0.30
  allow_derived_total: false
  per_field:
    total: 0.90
    vendor_name: 0.70
    invoice_number: 0.80
  route_ceiling:
    llm-vision: 0.85

po_matching:
  explicit:
    auto_approve: true
  inferred:
    enabled: true
    auto_approve: false              # closes the leak in §1.1
    max_absolute_distance: 500.00
    max_relative_distance: 0.10
    min_description_similarity: 0.25
    ambiguity_margin: 50.00

consolidation:
  enabled: true
  allow_without_references: false
  max_pos: 4
  auto_approve: false

currency:
  on_mismatch: convert_and_review    # review | convert_and_review | convert_and_allow
  auto_approve_converted: false
  fx:
    provider: ecb
    allow_live_lookup: true
    max_rate_age_days: 7
    max_drift: 0.05

ancillary:
  tax:       {permitted: true,  max_rate: 0.25}
  freight:   {permitted: true,  cap_absolute: 250.00, cap_relative: 0.05}
  surcharge: {permitted: false, cap_absolute: 0.00}

vendor_matching:
  strategy: normalized_exact         # exact | normalized_exact | fuzzy
  normalize:
    casefold: true
    strip_punctuation: true
    strip_legal_suffixes: [inc, llc, ltd, limited, gmbh, bv, pvt, "co"]
  fuzzy_fallback:
    enabled: true
    min_similarity: 0.90
    auto_approve: false              # fuzzy hit = candidate, never confirmation

duplicates:
  identity_fields: [vendor_name, invoice_number, total]
  amount_epsilon: 0.01
  scope_statuses: [APPROVED, NEEDS_REVIEW]

ledger:
  consuming_statuses: [APPROVED]

required_fields: [vendor_name, invoice_number, total]

severity_effect:
  ok: none
  info: none
  warn: review
  fail: reject
```

### Loader

Typed, validated at startup, immutable, and **pinned for the duration of a run**
so a mid-run reload cannot produce a self-inconsistent decision:

```python
# policy.py
from dataclasses import dataclass
import hashlib, yaml

@dataclass(frozen=True)
class TolerancePolicy:
    relative: float
    minimum_absolute: float
    applies_to: str
    direction: str
    max_unattributed: float

    def for_amount(self, amount: float) -> float:
        return max(abs(amount) * self.relative, self.minimum_absolute)

    def within(self, diff: float, tol: float) -> bool:
        return diff <= tol if self.direction == "over_only" else abs(diff) <= tol


@dataclass(frozen=True)
class Policy:
    version: int
    checksum: str
    tolerance: TolerancePolicy
    # ... confidence, po_matching, currency, ancillary, vendor_matching ...

    @classmethod
    def load(cls, path: str) -> "Policy":
        raw = open(path, "rb").read()
        data = yaml.safe_load(raw)
        cls._validate(data)
        return cls(version=data["version"],
                   checksum=hashlib.sha256(raw).hexdigest()[:12],
                   tolerance=TolerancePolicy(**data["tolerance"]))

    @staticmethod
    def _validate(d: dict):
        # Fail loudly at startup, never silently at decision time.
        assert d["tolerance"]["direction"] in ("over_only", "symmetric")
        assert 0 < d["tolerance"]["relative"] < 1
        for f, v in d["confidence"]["per_field"].items():
            assert 0 <= v <= 1, f"confidence threshold for {f} out of range"
```

The `checksum` matters as much as `version`: it catches an edited file that
someone forgot to re-version, so two runs claiming `version: 3` can never
disagree silently.

**Migration rule:** no numeric literal survives in `rules.py` or `matching.py`.
A grep for bare decimals in those modules should return nothing — worth adding as
a lint test, since that is precisely how the current magic numbers accumulated.

---

## 5. `DecisionTrace` — replayable audit

Design principle: **prose is rendered from the trace; the trace is never prose.**
Everything today is the inverse, which is why §4 of the audit failed.

```json
{
  "trace_version": "1.0",
  "run_id": 42,
  "idempotency_key": "sha256:9f2c…",
  "created_at": "2026-08-18T14:03:11Z",

  "versions": {
    "code": "51e16b9",
    "policy_version": 3,
    "policy_checksum": "a91c4e77b2f0",
    "extraction_model": "claude-sonnet-5",
    "trace_schema": "1.0"
  },

  "input": {
    "filename": "03b_split_po_globex_overflow.pdf",
    "sha256": "e3b0c442…",
    "bytes": 2255,
    "page_count": 1
  },

  "extraction": {
    "route": "llm-text",
    "has_text_layer": true,
    "fields": {
      "total": {"value": 2500.0, "confidence": 0.96, "source": "llm-text",
                "evidence": "Total Due: $2,500.00", "page": 1, "span": [402, 425]},
      "vendor_name": {"value": "Globex Logistics", "confidence": 0.94,
                      "source": "llm-text", "evidence": "Globex Logistics", "page": 1}
    },
    "raw_text_sha256": "1b4f0e98…"
  },

  "reference_snapshot": {
    "po": {"po_number": "PO-1002", "vendor": "Globex Logistics",
           "amount": 5000.0, "currency": "USD", "status": "open",
           "captured_at": "2026-08-18T14:03:11Z"},
    "consuming_runs": [
      {"run_id": 1, "amount": 3000.0, "status": "APPROVED"},
      {"run_id": 2, "amount": 2000.0, "status": "APPROVED"}
    ],
    "consumed_total": 5000.0
  },

  "checks": [
    {
      "rule_id": "po.match.explicit",
      "policy_path": "po_matching.explicit",
      "inputs": [{"field": "po_references", "value": ["PO-1002"], "confidence": 0.97}],
      "computation": {"op": "lookup", "key": "PO-1002", "found": true},
      "outcome": "matched",
      "severity": "ok"
    },
    {
      "rule_id": "tolerance.goods",
      "policy_path": "tolerance",
      "inputs": [
        {"name": "invoice_total", "value": 2500.0, "provenance_ref": "extraction.fields.total"},
        {"name": "remaining_before", "value": 0.0, "derived_from": "reference_snapshot"}
      ],
      "computation": {
        "expression": "diff = invoice_total - remaining_before",
        "diff": 2500.0,
        "tolerance": {"value": 100.0, "formula": "max(0.02 * 5000.00, 25.00)"},
        "direction": "over_only",
        "predicate": "diff <= tolerance",
        "result": false
      },
      "outcome": "breach",
      "severity": "fail"
    }
  ],

  "verdict": {
    "status": "NEEDS_REVIEW",
    "driven_by": ["tolerance.goods"],
    "severity_rollup": {"fail": 1, "warn": 0, "ok": 3},
    "decided_under_lock": true
  }
}
```

**Properties this buys:**

1. **Replay without the pipeline.** Every input to every check is present with
   its value at decision time. A verifier can recompute each `computation` block
   and assert the same `outcome` — no PDF, no model, no database.
2. **Immunity to reference drift.** `reference_snapshot` fixes the audit's most
   serious finding: editing `purchase_orders.json` can no longer change what a
   historical run meant.
3. **Provenance is linked, not duplicated.** `provenance_ref` points into
   `extraction.fields`, so a reviewer asking "where did $2,500 come from?" gets
   the evidence string and page.
4. **Diffable across policy versions.** Re-run a stored trace against policy v4
   and see exactly which checks change outcome — the safe way to tune tolerance.

Store as a separate `decision_traces` table (`run_id`, `trace_version`,
`trace_json`, indexed on `policy_version`), leaving the existing `runs` columns
untouched for the dashboard.

**Rendering contract:** `reasons[]` becomes a pure function of the trace.

```python
def render_reasons(trace: dict, policy) -> list:
    return [RENDERERS[c["rule_id"]](c, policy) for c in trace["checks"]
            if c["severity"] != "ok" or policy.display.show_passing]
```

Message wording then becomes a presentation change, testable independently and
retro-applicable to historical runs.

---

## 6. Sequencing

Revised against AUDIT.md now that the edge cases are specified.

| Phase | Work | Depends on | Risk |
|---|---|---|---|
| **0** | Restore green build, pytest suite | — | ✅ steps 1–2 done |
| **1a** | Inferred-PO cap + `review=True`; severity-effect table | 0 | Low |
| **1b** | Currency mismatch → review (no FX yet) | 0 | Low |
| **1c** | Vendor normalised-exact matching | 0 | Low |
| **2a** | `Tracked[T]`, dual-view `to_dict()` | 1 | Medium — touches every extractor |
| **2b** | Per-route confidence, LLM schema extension | 2a | Low |
| **2c** | Confidence gate + arithmetic consistency | 2b | Low |
| **3** | `rules.yaml`, typed loader, stamp version | 1 | Medium |
| **4a** | Transaction boundaries, WAL, idempotency | 0 | **High — concurrency** |
| **4b** | `run_allocations` table; ledger from allocations | 4a | High — data migration |
| **5a** | `DecisionTrace` + snapshot; stop re-seeding | 3, 4a | Medium |
| **5b** | Renderers derived from trace | 5a | Low |
| **6a** | Line-item decomposition, ancillary allowances | 2a, 3 | Medium |
| **6b** | Multi-PO consolidation | 4b, 6a | High |
| **6c** | FX provider + rate cache | 1b, 3 | Medium |
| **7** | UI: confidence badges, evidence, allocation view | 2a, 5a | Low |

**Critical path worth stating plainly:** 4a (transactions) blocks 4b
(allocations) blocks 6b (consolidation). Multi-PO consolidation is not a
matching feature — it is a *ledger* feature, and building it before the
transaction boundaries exist would bake a data-integrity bug into the schema.

If only three things get built: **1a + 1b** (the live bugs), **2a + 2c** (the
confidence gate, which closes finding 5 as a class), and **4a** (the race that
silently corrupts the PO ledger — the only defect here that produces a wrong
number rather than a wrong routing).

---

## 7. Testing strategy

Each fix needs a fixture that fails today.

| # | Fixture | Asserts |
|---|---|---|
| 1 | Invoice, no PO ref, amount near an unrelated PO, different services | `NEEDS_REVIEW`, reason cites description mismatch — not `APPROVED` |
| 2 | Two POs equally close | `NEEDS_REVIEW`, `matched_via == "ambiguous"` |
| 3 | €3,000 invoice vs $5,000 USD PO | `NEEDS_REVIEW`; never compared as `3000 < 5000` |
| 4 | Same, with a cached rate | Converted, still review, rate recorded in trace |
| 5 | Total absent; subtotal + tax present, lands within tolerance | `NEEDS_REVIEW` citing *derived total*, not arithmetic |
| 6 | Vendor `Acme Corp` vs approved `Acme Office Supplies` | Not auto-approved |
| 7 | Core goods exact + unlisted $50 fee | `NEEDS_REVIEW` naming only the fee; goods reported as reconciling |
| 8 | Two concurrent invoices, one PO, sum > balance | Exactly one approved; ledger never exceeds PO |
| 9 | Same PDF twice with idempotency key | One run; second returns the first |
| 10 | Stored trace replayed offline | Recomputed outcomes match recorded |
| 11 | Seed JSON edited after a run | Historical trace unchanged |
| 12 | Lint: bare decimals in `rules.py` / `matching.py` | None |

Test 8 needs real concurrency — two threads hitting the endpoint, asserting on
the committed ledger, repeated enough times to expose the race probabilistically.

---

## Appendix: answers to the research questions

**Pydantic field metadata.** Don't — see §0.2. `Field(json_schema_extra=...)` is
class-level and cannot carry per-invoice confidence. Use `Tracked[T]`. Pydantic
belongs at the LLM parse boundary (validating the model's JSON), after which
values convert into `Tracked`. This also keeps `rules.py` free of any Pydantic
import, preserving the deterministic core as plain Python.

**Declarative rules engine.** Recommended against — see §0.3. YAML for policy,
Python predicates in a `rule_id → callable` registry. The registry gives the two
things a rule engine would have: rules addressable by stable id (so the trace can
reference them), and policy tunable without code changes. It does not give
runtime-authored rules, which nothing here needs.

**Audit trail serialisation.** §5. The load-bearing decisions are: prose rendered
*from* the trace rather than stored as truth; a `reference_snapshot` so external
data drift cannot rewrite history; `provenance_ref` pointers rather than
duplicated field data; and version + checksum stamping so any two runs claiming
the same policy are genuinely comparable.
