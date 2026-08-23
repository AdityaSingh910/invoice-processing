"""Which language this application SPEAKS to a person (Phase L).

WHAT THIS MODULE IS, IN ONE SENTENCE

    The language changes the words. It never changes the decision.

That is the whole design, and it is rules.py's "the AI reads, the rules
decide" pointed at a different problem. Every locale-dependent thing in this
application is presentation: a run's status, the rules it failed, the amounts
on it and who may see it are computed before any language is chosen and are
identical whichever language asked. A locale is a rendering instruction, never
an input to a decision and never an input to an authorization check.

THE TWO HALVES OF "MULTILINGUAL", AND WHY THEY ARE TWO MODULES

This module is the SPEAKING half: it takes an HTTP header, picks a language,
and looks up sentences this application wrote. `doclang.py` is the READING
half: it takes the text of a vendor's PDF and works out what language the
DOCUMENT is in so the extractor can find its fields.

They are deliberately not one module, and the reason is a security property
rather than tidiness. If the two shared a notion of "the current language",
the locale a supplier picked in their browser could influence how their own
invoice was parsed -- which is precisely the coupling that turns a preference
into an attack surface. Nothing here is ever passed to `doclang`, and nothing
`doclang` detects is ever used to pick a UI language.

WHERE THE STRINGS LIVE, AND WHY ENGLISH LIVES IN PYTHON

`MESSAGES` below is the reference catalogue: key -> English. It is code,
beside the code that uses it, for the same reason `rules._SUGGESTED_RESOLUTIONS`
is code -- an English sentence this application says about somebody's invoice
must not be able to go missing because a data file was not deployed.
Translations ARE data (`data/locales/<tag>.json`), so adding a language is a
file drop with no code change and no redeploy of logic.

A key with no translation falls back to English. A locale with no file does
not exist. Neither is an error, and neither can raise: this module has no
failure mode that stops a page rendering, because a missing translation must
degrade to a language the reader may not want rather than to a 500.

THE FROZEN-KEY PROPERTY

`t()` takes a KEY, and a key is only ever a literal written in this codebase.
No caller-supplied string is ever used as a key, as a template, or as a path
component -- so there is no lookup a request can steer, and no translation
file a request can name. The Accept-Language header is bounded, shape-checked
and matched against the supported set before it is used for anything at all;
an unrecognised value is not an error, it is English.

WHAT IS DELIBERATELY NOT TRANSLATED

  * Amounts, dates, invoice numbers, vendor names and purchase-order numbers.
    No string in this catalogue interpolates one, so no locale can reformat a
    figure into something the ledger did not say. Formatting a number for
    display is the browser's job, from the raw value the API returned.
  * The internal decision vocabulary. APPROVED, NEEDS_REVIEW, REJECTED, rule
    names and event types are identifiers other code groups by (analytics
    groups on rule names; the portal keys its explanations on them).
    Translating an identifier would break every one of those joins.
  * HTTP error detail for internal endpoints. Phase K deliberately made those
    six words with the detail in the server log; localising them would widen
    what an error body says, for no reader who needs it.
"""
import json
import os
import re
import threading

import config

# --------------------------------------------------------------------------
# the supported set
#
# `en` is the reference and is always available because its strings are in
# this file. Every other tag needs a catalogue on disk; a tag listed here with
# no file is simply not offered, which is why `supported_locales()` reads what
# actually loaded rather than trusting this tuple alone.
# --------------------------------------------------------------------------
DEFAULT_LOCALE = "en"

# The languages this application has been translated into. Latin-script
# European languages, chosen because they are the ones an accounts-payable
# team in this deployment's market actually receives invoices in -- and
# because doclang.py can genuinely read invoices in all of them, so a supplier
# who switches the interface to Portuguese is not then handed an extractor
# that cannot read their document. Adding a language means adding a file to
# data/locales and a row here.
KNOWN_LOCALES = ("en", "es", "fr", "de", "pt", "it", "nl")

# What to call each language, IN that language. Never the English name alone:
# a person looking for their own language scans for the word they would write,
# not for the word we would.
LOCALE_NAMES = {
    "en": "English",
    "es": "Español",
    "fr": "Français",
    "de": "Deutsch",
    "pt": "Português",
    "it": "Italiano",
    "nl": "Nederlands",
}

# None of the seven is right-to-left. Reported anyway, because a client that
# has to ask "is this RTL?" should ask the server that decided the locale
# rather than keep its own list -- and because the day an RTL language is
# added, every consumer already reads the answer.
RTL_LOCALES = frozenset()

LOCALE_DIR = os.path.join(config.ROOT, "data", "locales")

# --------------------------------------------------------------------------
# Accept-Language parsing bounds
#
# The header is chosen entirely by the caller, so it is treated the way every
# other caller-supplied value in this codebase is: bounded before it is
# parsed, shape-checked before it is compared, and never used to build a path.
# --------------------------------------------------------------------------
MAX_HEADER_CHARS = 512
MAX_TAGS = 24

# BCP 47 in the only shape this application needs: a language subtag, and up
# to two more. Anything else -- a path fragment, a format string, a null byte,
# a 900-character blob -- fails this and is dropped without comment.
_TAG_RE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{2,8}){0,2}$")

# Placeholder substitution is a plain named-group replacement and NOT
# str.format / format_map, deliberately. "{x.__class__}".format_map({"x": s})
# reaches an attribute; "{0!r}" reaches a repr; a format spec can be made to
# do arithmetic on a caller's value. A translation file is operator-supplied
# data, and data must not be able to reach into objects. This regex admits a
# bare identifier and nothing else.
_PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]{0,40})\}")


# --------------------------------------------------------------------------
# the reference catalogue -- key -> English
#
# Keys are namespaced by the surface that says them (portal., chat.,
# pipeline.). The namespace is not decoration: it is how a reader of a
# translation file knows who is speaking and to whom. portal.* is read by an
# external supplier and must never mention an employee, another vendor, a run
# id or a balance; chat.* is read by our own finance staff.
# --------------------------------------------------------------------------
MESSAGES = {
    # ---- the portal's own status vocabulary (Phase J) --------------------
    "portal.state.approved": "Approved for payment.",
    "portal.state.in_review":
        "Received and being checked by our accounts payable team.",
    "portal.state.declined": "Not accepted for payment.",
    "portal.state.unknown": "Received and on file.",

    # ---- what is holding an invoice, in a supplier's words ---------------
    # One key per rule name in rules.py's frozen vocabulary. A rule with no
    # key here falls through to portal.hold.generic, which is the property
    # that keeps an internal sentence from ever reaching a vendor.
    "portal.rule.duplicate_check":
        "This invoice appears to duplicate one already submitted. No action is "
        "needed unless you believe it was sent in error.",
    "portal.rule.vendor_approved":
        "We could not match this invoice to your supplier record. Our team is "
        "confirming it.",
    "portal.rule.document_is_an_invoice":
        "The document submitted did not read as an invoice. Please check that "
        "the correct file was sent.",
    "portal.rule.required_fields_present":
        "Some details could not be read from the document -- typically the "
        "invoice number, date or total. Our team is checking it.",
    "portal.rule.document_readable":
        "The document could not be read automatically, so it is being checked "
        "by hand. A text-based PDF processes faster than a scan or photograph.",
    "portal.rule.extraction_confidence":
        "Some details could not be read with confidence, so the invoice is "
        "being checked by hand.",
    "portal.rule.invoice_amount_valid":
        "The invoice total could not be accepted as stated. Our team is "
        "checking it.",
    "portal.rule.invoice_arithmetic":
        "The stated subtotal, tax and total do not add up. Please check the "
        "figures on the document.",
    "portal.rule.po_matched":
        "This invoice could not be matched to a purchase order. Quoting the "
        "purchase order number on the document helps it clear automatically.",
    "portal.rule.invoice_to_po_split_stated":
        "This invoice covers more than one purchase order and does not state "
        "how the total is divided between them, so it is being confirmed by "
        "hand.",
    "portal.rule.po_remaining_check":
        "The amount billed is more than the purchase order has remaining. Our "
        "team is confirming it.",
    "portal.rule.currency_match":
        "The invoice currency differs from the purchase order currency, so it "
        "is being confirmed by hand.",
    "portal.rule.currency_reuse":
        "The invoice currency does not appear to match the amount as stated. "
        "Please check the currency on the document.",
    "portal.rule.security_screen":
        "This document is being checked by hand before any action is taken.",
    "portal.rule.vendor_identity":
        "The supplier named on this document is not one your account "
        "represents. Our team is confirming who it is from.",
    "portal.hold.generic": "Being checked by our accounts payable team.",
    "portal.declined.generic":
        "Not accepted for payment. Please contact your accounts payable contact.",

    # ---- the client-visible timeline -------------------------------------
    "portal.event.processing_completed": "Received and processed",
    "portal.event.review_required": "Referred for checking",
    "portal.event.accepted": "Approved by our team",
    "portal.event.rejected": "Declined by our team",
    "portal.event.auto_approved": "Approved",
    "portal.event.status_overridden": "Decision updated",

    # ---- account problems, stated rather than shown as missing invoices --
    "portal.notice.unknown_vendor_link":
        "Part of this account's supplier link is not recognised. Some invoices "
        "may not be listed. Please contact your accounts payable contact.",
    "portal.notice.ambiguous_vendor_link":
        "Part of this account's supplier link matches more than one supplier "
        "record, so it has been left out rather than guessed at. Please "
        "contact your accounts payable contact.",
    "portal.error.not_linked":
        "This account is not linked to a supplier record. Please contact your "
        "accounts payable contact.",
    "portal.error.not_set_up":
        "This account is not set up for the supplier portal. Please contact "
        "your accounts payable contact.",
    "portal.error.unknown_state_filter": "Unknown status filter: {state}",
    "portal.error.daily_limit":
        "This account's daily submission limit of {limit} invoices has been "
        "reached. Please try again tomorrow.",
    "portal.error.no_such_invoice": "No such invoice",
    "portal.error.no_document": "No document is stored for this invoice",
    "portal.error.document_gone": "Document content is no longer available",
    "portal.error.processing_failed":
        "Processing failed. The invoice was not submitted.",

    # ---- the assistant (Phase K2) ----------------------------------------
    # The three fixed answers. Each exists because the honest answer to that
    # question depends on no record at all, and a model asked to improvise
    # around a gap is a model inventing a payment amount.
    "chat.oos.payment":
        "This application processes and approves invoices; it holds no "
        "payment, remittance or bank information at all. Approving an invoice "
        "here records a decision, not a payment. Whether it was actually paid "
        "lives in whatever system issues payments.",
    "chat.oos.correctness":
        "The application records what its rules decided and what people "
        "decided, but it holds no independent record of whether a decision was "
        "right -- there is no ground-truth label and no downstream "
        "confirmation to compare against. I can tell you what was decided and "
        "by whom.",
    "chat.oos.configuration":
        "I only have access to invoice records. I have no access to "
        "credentials, keys, environment settings or anything about how this "
        "system is deployed, and I could not retrieve them if asked.",
    "chat.unrecognised":
        "I could not tell which records that question is about. I can list "
        "the invoices, the vendors or the purchase orders; look up an invoice "
        "by its number or a purchase order's remaining balance; say what is "
        "waiting for review; or give the headline figures for a period. Ask "
        "about one of those and I'll retrieve it.",
    "chat.notice.no_model":
        "Answering from the records directly -- no language model is "
        "configured, so this is not phrased as prose.",
    "chat.notice.budget_spent":
        "The daily assistant budget is spent, so this is the retrieved data "
        "without the written summary. The records themselves are unaffected.",
    "chat.notice.provider_failed":
        "The assistant could not reach the language model ({detail}), so this "
        "is the retrieved data without the written summary.",
    "chat.notice.figures_not_translated":
        "The figures below are the ledger's own and are shown exactly as "
        "recorded, in whatever language they were entered.",
    # Labels for the answer built in Python when no model is available. Only
    # the words are translated -- every figure beside them is the ledger's own
    # and is printed verbatim.
    "chat.structured.none": "I retrieved no records for that question.",
    "chat.structured.not_found":
        "No matching record exists in this application.",
    "chat.structured.no_record_of":
        "No record of {reference} exists in this application.",
    "chat.structured.can_answer": "I can answer:",
    "chat.structured.cannot_answer": "I cannot answer:",
    "chat.structured.queue_empty": "Nothing is waiting for review.",
    "chat.structured.no_invoice": "No matching invoice.",
    # Starter questions. Translated because a suggestion a user cannot read is
    # not a suggestion.
    "chat.suggestion.queue": "What invoices are waiting for review?",
    "chat.suggestion.volume": "How many invoices were processed this week?",
    "chat.suggestion.po_balance": "What is the remaining balance on PO-1002?",
    "chat.suggestion.why_held": "Why was the last invoice held?",
    "chat.suggestion.list_invoices": "Show me the invoices",
    "chat.suggestion.list_vendors": "List all vendors",
    "chat.suggestion.list_pos": "List all purchase orders",
    "chat.suggestion.capabilities": "What can you help me with?",

    # ---- what the pipeline says about a document's language --------------
    "pipeline.language.detected": "Document language: {language}.",
    "pipeline.language.undetermined":
        "Document language could not be determined from the text.",
    "pipeline.language.unsupported_script":
        "Document appears to use the {script} script, which this extractor has "
        "no field vocabulary for.",
}

# Every key an operator's translation file may legitimately carry. Anything
# else in a file is ignored rather than trusted -- a catalogue is not a place
# to introduce a new message.
MESSAGE_KEYS = frozenset(MESSAGES)

_CATALOGUES = {}
_LOCK = threading.Lock()
_LOADED = False


# --------------------------------------------------------------------------
# catalogue loading
# --------------------------------------------------------------------------

def _read_catalogue(tag: str):
    """One translation file, or None. Never raises.

    `tag` is always a member of KNOWN_LOCALES -- never a caller-supplied
    value -- so this cannot be steered into reading a file elsewhere. The
    shape check is belt and braces for the day someone calls it with a
    variable.
    """
    if tag not in KNOWN_LOCALES or tag == DEFAULT_LOCALE:
        return None
    if not re.match(r"^[a-z]{2}(?:-[a-z]{2})?$", tag):
        return None
    path = os.path.join(LOCALE_DIR, tag + ".json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    # Only known keys, only string values. A translation file cannot introduce
    # a message, and cannot smuggle a list or an object into a slot the code
    # will concatenate.
    return {k: v for k, v in data.items()
            if k in MESSAGE_KEYS and isinstance(v, str) and v.strip()}


def load_catalogues(force: bool = False):
    """Read every translation file once, into memory.

    Called lazily on first use rather than at import, for the reason config.py
    reads most of its settings at call time: import order is not a thing this
    codebase relies on. `force` exists for tests and for an operator who has
    just edited a file.
    """
    global _LOADED
    with _LOCK:
        if _LOADED and not force:
            return
        _CATALOGUES.clear()
        _CATALOGUES[DEFAULT_LOCALE] = dict(MESSAGES)
        for tag in KNOWN_LOCALES:
            cat = _read_catalogue(tag)
            if cat:
                _CATALOGUES[tag] = cat
        _LOADED = True


def supported_locales() -> tuple:
    """The tags this deployment can actually answer in, English first.

    Derived from what loaded, not from KNOWN_LOCALES: a language whose file is
    missing or malformed is not offered, because offering it would produce a
    screen of English under a Spanish label.
    """
    load_catalogues()
    return tuple([DEFAULT_LOCALE] + [t for t in KNOWN_LOCALES
                                     if t != DEFAULT_LOCALE and t in _CATALOGUES])


def language_options() -> list:
    """What a language picker needs, decided by the server.

    Served rather than hard-coded in a client for the same reason
    `chat.starter_prompts()` is: a client offering a language this deployment
    cannot answer in is offering a broken choice.
    """
    return [{"tag": tag, "name": LOCALE_NAMES.get(tag, tag),
             "rtl": tag in RTL_LOCALES,
             "default": tag == DEFAULT_LOCALE}
            for tag in supported_locales()]


def catalogue_status() -> dict:
    """How complete each translation is. For an operator, not for a caller.

    A translation that is 60% done is a real state and a silent one: the
    missing keys fall back to English and nothing anywhere says so. This
    counts them.
    """
    load_catalogues()
    out = {}
    for tag in supported_locales():
        cat = _CATALOGUES.get(tag) or {}
        missing = sorted(MESSAGE_KEYS - set(cat))
        out[tag] = {
            "name": LOCALE_NAMES.get(tag, tag),
            "translated": len(MESSAGE_KEYS) - len(missing),
            "total": len(MESSAGE_KEYS),
            "missing_keys": missing,
        }
    return out


# --------------------------------------------------------------------------
# negotiation
# --------------------------------------------------------------------------

def normalise(tag) -> str:
    """A caller's tag reduced to the canonical form, or "" if it is not one.

    Case is folded (`PT-br` and `pt-BR` are one language), the region is kept
    for matching but never invented, and anything failing the shape check
    returns "" rather than raising -- an unreadable preference is a preference
    we do not have, not an error the caller should see.
    """
    if not isinstance(tag, str):
        return ""
    tag = tag.strip()
    if not tag or len(tag) > 35 or not _TAG_RE.match(tag):
        return ""
    parts = tag.split("-")
    out = parts[0].lower()
    for p in parts[1:]:
        out += "-" + (p.upper() if len(p) == 2 else p.lower())
    return out


def _match(tag: str, supported) -> str:
    """The best supported locale for one tag: exact, then base language."""
    if not tag:
        return ""
    if tag in supported:
        return tag
    base = tag.split("-")[0]
    return base if base in supported else ""


def parse_accept_language(header) -> list:
    """The tags in an Accept-Language header, best first. Never raises.

    Bounded twice before anything is parsed -- total length and number of tags
    -- because this is the one string in a request that a client is invited to
    make arbitrarily long, and a preference list is not a place to spend time.
    A malformed q-value sorts as the RFC's default of 1.0 rather than
    rejecting the tag: the caller meant to express a preference, and the worst
    case of misreading a weight is answering in their second language.
    """
    if not isinstance(header, str) or not header.strip():
        return []
    header = header[:MAX_HEADER_CHARS]
    scored = []
    for index, part in enumerate(header.split(",")[:MAX_TAGS]):
        bits = part.split(";")
        tag = normalise(bits[0])
        if not tag:
            continue
        q = 1.0
        for extra in bits[1:]:
            extra = extra.strip()
            if extra.lower().startswith("q="):
                try:
                    q = max(0.0, min(1.0, float(extra[2:])))
                except ValueError:
                    q = 1.0
        if q <= 0:
            continue
        # `index` keeps the header's own order as the tie-break, so equal
        # weights resolve the way the caller wrote them.
        scored.append((-q, index, tag))
    scored.sort()
    return [tag for _, _, tag in scored]


def resolve(explicit=None, accept_language=None) -> str:
    """The locale to answer in. ALWAYS returns a supported tag.

    THE ORDER IS THE DESIGN:

      1. an EXPLICIT choice (`?lang=`, or a stored preference) -- somebody
         went and picked this, which outranks what their browser was
         configured with years ago on a different continent;
      2. Accept-Language, in the caller's own order of preference;
      3. English.

    An unsupported explicit choice does NOT fall through to the header, and
    that is deliberate: `?lang=xx` where xx is unknown means the caller asked
    for something this deployment does not have, and quietly answering in
    their browser's language would hide the fact that their choice did not
    take. It falls to the default, which is the one language every deployment
    has. Either way the request succeeds -- an unsupported language is never
    a 400, because a preference is not a precondition.
    """
    supported = supported_locales()

    # ANY non-empty explicit value means a choice was made, so the header is
    # not consulted at all -- whether that value resolved or not. Only an
    # ABSENT choice falls through, which is the state of somebody who has
    # never opened the picker.
    if isinstance(explicit, str) and explicit.strip():
        return _match(normalise(explicit), supported) or DEFAULT_LOCALE

    for tag in parse_accept_language(accept_language):
        chosen = _match(tag, supported)
        if chosen:
            return chosen
    return DEFAULT_LOCALE


# --------------------------------------------------------------------------
# lookup
# --------------------------------------------------------------------------

def _substitute(template: str, params: dict) -> str:
    """Fill {named} placeholders. No attribute access, no indexing, no specs.

    A placeholder with no parameter is left exactly as written rather than
    blanked: a sentence reading "limit of {limit} invoices" is visibly wrong
    and gets fixed, whereas "limit of  invoices" reads as a deliberate gap and
    survives for years.
    """
    if not params or "{" not in template:
        return template

    def one(m):
        value = params.get(m.group(1))
        return m.group(0) if value is None else str(value)

    return _PLACEHOLDER_RE.sub(one, template)


def t(key: str, locale: str = None, **params) -> str:
    """One message, in `locale`, with `params` substituted. Never raises.

    THE FALLBACK CHAIN, and every step of it is a real state:

        the locale's own translation
          -> English (the translation is incomplete)
            -> the key itself (the key does not exist -- a programming error,
               surfaced as a visible token rather than as an empty string or
               an exception, because a blank sentence on a supplier's screen
               is indistinguishable from a design decision)

    `key` is only ever a literal from this codebase. Nothing here builds one
    from request data, and `params` are substituted INTO the translation --
    the translation is never built from a parameter, so a vendor name
    containing "{client_id}" is a vendor name and not a template.
    """
    load_catalogues()
    tag = locale if locale in _CATALOGUES else DEFAULT_LOCALE
    template = _CATALOGUES.get(tag, {}).get(key)
    if template is None:
        template = MESSAGES.get(key)
    if template is None:
        return key
    return _substitute(template, params)


def has(key: str) -> bool:
    """Whether a key exists at all. For code that must fall back to a generic
    message rather than print a key -- the portal's unmapped-rule path."""
    return key in MESSAGE_KEYS


def is_rtl(locale: str) -> bool:
    return locale in RTL_LOCALES


def describe(locale: str) -> dict:
    """The locale block every localised response carries.

    A response says which language it was rendered in, rather than leaving a
    client to assume it got what it asked for. That matters when a preference
    could not be honoured: a supplier who asked for Italian and is reading
    English should be able to see that, and so should whoever supports them.
    """
    tag = locale if locale in supported_locales() else DEFAULT_LOCALE
    return {"locale": tag, "name": LOCALE_NAMES.get(tag, tag),
            "rtl": tag in RTL_LOCALES, "default": DEFAULT_LOCALE}
