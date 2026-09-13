# Demo Script — Invoice Processing (PS-1, Finance / AP)

**Record against the LIVE site:** https://invoice-processing-seven.vercel.app
Sign-in is pre-filled (`demo` / `zampisthebest`) — you press one button.

**Flow:** intro → sign-in page → happy path → one edge case (the concurrency race).

---

## Before you hit record (5 minutes of setup)

1. Open the site, sign in, go to **Overview**. If any runs are listed, click
   **Reset demo data** — the samples are order-dependent, and a clean history is
   what makes the verdicts come out as labelled. The concurrency pair specifically
   needs PO-7000-CONC at its full $7,000 balance for the race to mean anything.
2. Sign out, so the recording starts on the sign-in screen.
3. Close every other tab. Browser at 100% zoom, window maximised.
4. Have this script on a second screen or your phone.
5. **Verified working today:** live API healthy, 12 POs + 11 vendors seeded, all
   14 samples present, a real run takes **~20 seconds** end to end.

**Pacing rule:** every run takes ~20s. Do not sit in silence — the narration
below is written to be spoken *while the stages tick*.

---

## 0:00 – 0:40 · Intro, on the sign-in page

*Recording starts on the sign-in screen. Don't click anything yet.*

> "Hello, my name is Aditya Singh.
>
> This is an accounts-payable process I've automated end to end. A vendor
> invoice arrives as a PDF, and somebody in finance has to decide: pay it, hold
> it, or refuse it.
>
> The one design decision everything else follows from: **the AI reads, the
> rules decide.** An LLM extracts fields from the PDF, because invoice layouts
> vary enormously. But the *decision* — approve, hold, reject — is a hundred
> percent deterministic Python. No model is involved in it. No prompt anywhere
> in this codebase contains the words approve, reject, or tolerance. That
> matters because an AP decision has to be reproducible and auditable — if a
> model decided, I couldn't tell you why, and I couldn't promise the same
> invoice gets the same answer twice."

---

## 0:40 – 0:55 · Sign in

*Point out the sign-in box before clicking.*

> "Signing in is OAuth2 under the hood, and the box is already pre-filled with
> a working demo credential, so I'll just press Sign in."

*Click Sign in.*

> "It's deployed — frontend on Vercel, API on Railway, Postgres on Supabase."

---

## 0:55 – 2:05 · Happy path, live

*Click **Process invoice**. The sample library is on the right — click **Happy
path**, then **Run**.*

> "I'll start with a clean invoice — Acme Office Supplies, against purchase
> order 1001."

*While the nine stages tick — narrate over them:*

> "Nine stages, and you can watch each one. Ingest. Extract the text layer.
> Extract fields — that's the only place the model is used, and it tells me
> which route it took: Groq over the text, because this PDF has a text layer. A
> scanned one would go to Gemini vision instead.
>
> Then validate — required fields present, and the arithmetic actually adds up.
> Vendor check against the approved list. PO match. Duplicate check. Tolerance
> check. Then the decision.
>
> One thing worth pointing out: the stages **don't short-circuit**. If the
> vendor check fails, it still runs everything after it. That's deliberate — a
> reviewer wants the whole picture, not the first thing that went wrong."

*It lands **APPROVED**. Scroll to the audit trail / reasoning panel.*

> "Approved. And here's the audit trail — built by the rule engine *as it
> evaluates*, not written afterwards by a second pass that could disagree with
> the decision. It records the PO it matched and which file and row that PO came
> from, the variance, the tolerance it was compared against, every rule that
> passed or failed, and per-field confidence and provenance.
>
> Note the tolerance is **one-sided**. This invoice came in five dollars *under*
> the PO. Billing under is a normal partial invoice; billing over is a problem.
> Getting that wrong was a real bug I fixed early — I'd used `abs()`, which
> flagged every legitimate partial invoice as an exception."

---

## 2:05 – 3:25 · Edge case — two invoices race one purchase order

*Back to **Process invoice**. Click the sample **Concurrency race — 1 of 2**.
A second button appears: **Run both at once — race for PO-7000-CONC**.*

> "Now the edge case I most want to show you.
>
> These two invoices are four thousand dollars each, and they both charge the
> **same** seven-thousand-dollar purchase order. Individually each one is a
> perfectly ordinary partial invoice. Together they want eight thousand against
> an order that authorises seven.
>
> Notice neither card predicts a verdict — they both say **Ready to run**. That's
> deliberate, because there isn't a right answer to show you yet. Which one wins
> is decided by the database."

*Click **Run both at once**.*

> "That's two separate HTTP requests, dispatched in the same instant, landing on
> two different worker threads. They both run all nine stages independently.
>
> The interesting part is the commit. The pipeline computes its verdict *outside*
> any transaction — it has to, because extraction takes seconds and holding a
> write lock across a model call would serialise the whole system. So by the time
> either one commits, the balance it decided against may already be stale.
>
> So at commit time each one takes `SELECT FOR UPDATE` on that purchase order
> row, re-reads what's actually been consumed, and if the invoice no longer
> fits, it downgrades itself to needs-review *before* inserting. The loser
> literally blocks on the winner's lock."

*Both land. One is **APPROVED**, the other **NEEDS_REVIEW** — and the cards now
show the real outcome.*

> "One approved, one held, and the order has exactly three thousand left. Never
> both — that's the guarantee. And which one wins genuinely varies: I've run
> this repeatedly and it goes the other way depending on scheduling. Nothing in
> the code knows which invoice is which.
>
> This isn't just a demo trick either — it's tested directly, with real threads
> against real Postgres: eight threads racing a ten-thousand-dollar PO with
> two-thousand-dollar invoices resolve to exactly five approved, three held,
> every single time."

*Click **Review queue**.*

> "The held one lands here. This is a shared queue — several people work it at
> once, so a reviewer **claims** an invoice, and that claim is a lease enforced
> by the same row-locking pattern. Ten threads racing one claim: exactly one
> wins. Same mechanism, reused, rather than a different concurrency scheme per
> feature."

*Claim it, then click **Accept**.*

> "And when a human rules on it, the ruling is recorded **beside** the automated
> decision, never on top of it. `automated_decision` stays NEEDS_REVIEW forever
> — the permanent record of what the rules concluded. `human_decision` becomes
> ACCEPTED. Only the ledger status moves."

---

## 3:25 – 3:45 · Close

> "So — PDF in, decision out, with a deterministic audit trail behind every one,
> a human path for anything held, a database-level guarantee that two invoices
> can never overspend the same purchase order, and the whole thing deployed.
>
> Thanks."

---

## If something goes wrong on the day

| Problem | Do this |
|---|---|
| A run seems stuck | It polls once a second; ~20s is normal. Keep talking. |
| Verdict isn't what the label says | History isn't clean. Reset demo data, re-run in order. |
| Route says `regex`, not `groq` | Groq free tier is 429-ing. Say: "the model was rate-limited so it fell back to the local extractor — which is exactly why that fallback exists." |
| Concurrency race gives one APPROVED / one NEEDS_REVIEW but not the other way | That's correct — either order is a valid outcome, only "never both approved" is guaranteed. Say so. |
| Site feels slow | The database is in a different region to the API. Say so — it's an honest infra note, not a bug. |

## Do NOT demo these — not in the current UI

- Email queue screen — removed from the nav
- Settings / Gmail connection screen — removed from the nav (the API still exists)

Current nav is exactly: Overview · Process invoice · Invoices · Review queue ·
Analytics · Assistant · Purchase orders · Approved vendors.
