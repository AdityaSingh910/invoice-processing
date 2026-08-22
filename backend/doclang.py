"""What language a VENDOR'S DOCUMENT is in, and how to read one (Phase L).

WHAT THIS MODULE IS

The reading half of multilingual support. `i18n.py` decides what language this
application SPEAKS to a person; this decides what language a supplier's PDF
was WRITTEN in, so the extractor can find "Rechnungsnummer" where it would
otherwise only look for "Invoice #".

The two never touch. Nothing here reads an Accept-Language header, and nothing
in i18n reads a document -- because if they shared a notion of "the current
language", the locale a supplier picked in their browser could change how
their own invoice was parsed. A preference must not be able to become an input
to extraction.

WHAT DETECTION IS ALLOWED TO AFFECT, AND WHAT IT IS NOT

    It can only ever ADD a place to look. It can never change a decision.

Detection is a heuristic, and this codebase does not let heuristics near
verdicts. Concretely:

  * The regex extractor tries the ENGLISH patterns first, always, and appends
    the detected language's patterns after them. So an English invoice reads
    exactly as it did before this module existed -- byte for byte -- and a
    German one gains patterns it previously had none of. A wrong detection
    costs a pattern that does not match; it cannot cost a field that did.
  * `rules.decide()` is not passed a language and has no branch on one. The
    same extracted numbers produce the same verdict whatever this module said.
  * The prompt-injection guard is NOT gated on detection (see
    `extraction._INJECTION_PATTERNS`). A security control that only ran when a
    heuristic agreed would be evaded by writing the document in two languages.

THE THIRD STATE, AGAIN

Phase F insisted that `pass` / `fail` / `unavailable` are three states and
that collapsing the last two is how honest senders get flagged. The same
discipline applies here, and `detect()` reports all three:

    a language      we recognised it, and we have a field vocabulary for it
    a SCRIPT only   we can see this is Greek or Japanese; we have no
                    vocabulary for it, which is a gap in what we can read and
                    not a fault in the document
    UNDETERMINED    too little text, or nothing scored high enough to separate
                    one language from the next

"We could not tell" and "it is English" are different facts, and only the
second is a claim.

WHY THE VOCABULARIES ARE CODE AND NOT A DATA FILE

`data/email_domain_policy.json` and `data/trusted_email_senders.json` are data
because they are DEPLOYMENT-SPECIFIC: which domains you trade with is your
business and changes without a release. The German word for "invoice" is not
deployment-specific -- it is a property of the language, the same for every
installation, and the regex route must keep working on a machine with no data
files and no network at all. So it lives here, and adding a language is one
entry in each table below.
"""
import calendar
import re
import unicodedata

# Languages with a full field vocabulary -- the ones the regex fallback can
# actually read an invoice in. Deliberately the same seven i18n.py can speak,
# so a supplier who is offered Portuguese is not then handed an extractor that
# cannot read a Portuguese invoice.
LANGUAGES = ("en", "es", "fr", "de", "pt", "it", "nl")

# What `detect()` returns when nothing separated one language from another.
# A tag, not None, so every consumer has a value to record and to print.
UNDETERMINED = "und"

# English names, for a stage line an operator reads. NOT for a supplier's
# screen -- that is i18n's job and it has its own translations.
LANGUAGE_NAMES = {
    "en": "English", "es": "Spanish", "fr": "French", "de": "German",
    "pt": "Portuguese", "it": "Italian", "nl": "Dutch",
    UNDETERMINED: "undetermined",
}

# Languages that write 1.234,56 rather than 1,234.56. All six non-English
# entries, which is why the flag is derived rather than listed twice.
DECIMAL_COMMA_LANGUAGES = frozenset(LANGUAGES) - {"en"}

# Languages whose numeric dates are day-first without ambiguity (15/03/2026 is
# 15 March). English is deliberately absent: en-GB is day-first and en-US is
# month-first, the document does not say which, and guessing would silently
# move an invoice date by up to eleven months. An English date is therefore
# left exactly as printed -- which is also what this pipeline did before this
# module existed.
DAY_FIRST_LANGUAGES = frozenset(LANGUAGES) - {"en"}


# --------------------------------------------------------------------------
# scripts
#
# Detected from codepoint ranges, which is cheap, exact and needs no data. The
# point is not to identify the language -- it is to be able to say "this is
# Japanese and we have no vocabulary for it" instead of "undetermined", which
# sends an operator looking for a problem with the document.
# --------------------------------------------------------------------------
_SCRIPT_RANGES = [
    ("Cyrillic", (0x0400, 0x04FF)),
    ("Greek", (0x0370, 0x03FF)),
    ("Hebrew", (0x0590, 0x05FF)),
    ("Arabic", (0x0600, 0x06FF)),
    ("Devanagari", (0x0900, 0x097F)),
    ("Thai", (0x0E00, 0x0E7F)),
    ("Hiragana", (0x3040, 0x309F)),
    ("Katakana", (0x30A0, 0x30FF)),
    ("Han", (0x4E00, 0x9FFF)),
    ("Hangul", (0xAC00, 0xD7AF)),
]

# Below this share of the letters, a stray character is a stray character --
# a Greek mu in a unit, a Han character in a part number -- and not evidence
# that the document is written in that script.
_SCRIPT_SHARE = 0.15

# Enough text to say anything at all. Under this the answer is UNDETERMINED,
# because a five-word document scores nothing reliably and a confident wrong
# answer is worse than no answer.
MIN_TEXT_CHARS = 60

# How far ahead the winner must be. Both bounds matter: the absolute floor
# stops three coincidental matches deciding a language, and the margin stops
# Spanish and Portuguese -- which share a great deal of invoice vocabulary --
# being separated by one word.
_MIN_SCORE = 4
_MIN_MARGIN = 2


def _fold(text: str) -> str:
    """Lower-cased and stripped of diacritics, for matching only.

    "Quantité" and "quantite" are the same word to a scoring table, and a
    scanned document loses accents routinely. Never used for anything that is
    displayed or stored -- the document's own text is what gets shown.
    """
    text = unicodedata.normalize("NFD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def script_of(text: str) -> str:
    """The dominant non-Latin script in `text`, or "Latin".

    Counted over letters only, so page furniture and numbers do not dilute it.
    """
    counts = {}
    letters = 0
    for ch in (text or "")[:20000]:
        if not ch.isalpha():
            continue
        letters += 1
        code = ord(ch)
        for name, (lo, hi) in _SCRIPT_RANGES:
            if lo <= code <= hi:
                counts[name] = counts.get(name, 0) + 1
                break
    if not letters or not counts:
        return "Latin"
    name, n = max(counts.items(), key=lambda kv: kv[1])
    return name if (n / letters) >= _SCRIPT_SHARE else "Latin"


# --------------------------------------------------------------------------
# the scoring vocabulary
#
# High-signal words that appear on an invoice IN that language. Scored as
# DISTINCT terms present, never as occurrences, so a document repeating
# "Total" forty times does not out-vote one that quietly says
# "Rechnungsnummer", "Mehrwertsteuer" and "Zahlungsziel" once each.
#
# Terms shared across several of these languages ("total", "cliente", "data")
# are deliberately thin on the ground here: a term that scores for three
# languages at once separates none of them, and the margin rule above is what
# turns that into an honest UNDETERMINED rather than a coin toss.
# --------------------------------------------------------------------------
_SCORING_TERMS = {
    "en": ["invoice", "bill to", "ship to", "due date", "quantity", "description",
           "subtotal", "purchase order", "amount due", "payment terms",
           "unit price", "sales tax", "remit", "balance due", "invoice date",
           "invoice number", "customer", "thank you for your business"],
    "es": ["factura", "fecha", "importe", "cantidad", "precio unitario",
           "base imponible", "impuesto", "iva", "cliente", "direccion",
           "vencimiento", "forma de pago", "pedido", "descripcion",
           "numero de factura", "total a pagar", "descuento", "domicilio"],
    "fr": ["facture", "date", "montant", "quantite", "prix unitaire", "tva",
           "client", "adresse", "echeance", "reglement", "bon de commande",
           "designation", "remise", "net a payer", "total ttc", "total ht",
           "numero de facture", "conditions de paiement"],
    "de": ["rechnung", "rechnungsnummer", "rechnungsdatum", "betrag", "menge",
           "einzelpreis", "gesamtbetrag", "mehrwertsteuer", "mwst", "ust",
           "netto", "brutto", "kunde", "zahlungsziel", "bestellnummer",
           "bezeichnung", "lieferung", "zahlbar", "summe"],
    "pt": ["fatura", "factura", "valor", "quantidade", "preco unitario",
           "cliente", "morada", "vencimento", "pagamento", "encomenda",
           "descricao", "numero da fatura", "total a pagar", "desconto",
           "data de emissao", "contribuinte", "iva"],
    "it": ["fattura", "importo", "quantita", "prezzo unitario", "imponibile",
           "cliente", "indirizzo", "scadenza", "pagamento", "ordine",
           "descrizione", "numero fattura", "totale documento", "sconto",
           "data fattura", "aliquota", "iva"],
    "nl": ["factuur", "factuurnummer", "factuurdatum", "bedrag", "aantal",
           "stukprijs", "totaal", "btw", "klant", "adres", "vervaldatum",
           "betaling", "bestelnummer", "omschrijving", "subtotaal",
           "levering", "te betalen"],
}

_COMPILED_TERMS = {
    lang: [(term, re.compile(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])"))
           for term in terms]
    for lang, terms in _SCORING_TERMS.items()
}


def score(text: str) -> dict:
    """How many distinct vocabulary terms each language contributed."""
    folded = _fold(text or "")[:40000]
    return {lang: sum(1 for _, rx in terms if rx.search(folded))
            for lang, terms in _COMPILED_TERMS.items()}


def detect(text: str) -> dict:
    """What language a document is in. Never raises.

    Returns a dict, always with the same keys, because a consumer that has to
    branch on shape before it can read a result is a consumer that will one
    day forget to:

        language    one of LANGUAGES, or UNDETERMINED
        supported   whether a field vocabulary exists for it
        script      the dominant script ("Latin" for all seven languages)
        confidence  0.0-1.0, and honest: it is derived from how far ahead the
                    winner was, so a bare win reports a low number rather than
                    a high one
        scores      the whole table, so a disagreement is diagnosable rather
                    than mysterious
    """
    text = text if isinstance(text, str) else ""
    detected_script = script_of(text)
    base = {"language": UNDETERMINED, "supported": False,
            "script": detected_script, "confidence": 0.0, "scores": {}}

    if detected_script != "Latin":
        # We can say what it is written IN without pretending to know which
        # language it is. That is a more useful answer than UNDETERMINED and a
        # more honest one than a guess.
        return base
    if len(text.strip()) < MIN_TEXT_CHARS:
        return base

    scores = score(text)
    base["scores"] = scores
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_lang, top = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0

    if top < _MIN_SCORE or (top - second) < _MIN_MARGIN:
        return base

    base["language"] = top_lang
    base["supported"] = True
    # Confidence is the margin as a share of the winner's own score: a 9-to-2
    # win reads as strong, a 5-to-3 win as weak, and neither is rounded up to
    # look decisive.
    base["confidence"] = round(min(1.0, (top - second) / float(top)), 2)
    return base


def name_of(language: str) -> str:
    return LANGUAGE_NAMES.get(language, language or UNDETERMINED)


# --------------------------------------------------------------------------
# field label vocabularies
#
# Regex FRAGMENTS, not whole patterns. `extraction.py` owns the shape of an
# extraction pattern (the money expression, the anchoring, the separators) and
# assembles these into it -- so there is one definition of what a labelled
# amount looks like, and this file only says what the label is called in each
# language.
#
# Every fragment is matched case-insensitively and against text that still has
# its accents, so accented forms are written out where they occur.
# --------------------------------------------------------------------------
_LABELS = {
    "es": {
        "invoice_number": [r"(?:n[uú]m(?:ero)?\.?[ \t]*(?:de[ \t]*)?)?factura",
                           r"factura[ \t]*(?:n[uú]m(?:ero)?\.?|n[.ºo]|nro\.?)"],
        "date": [r"fecha(?:[ \t]*de[ \t]*(?:factura|emisi[oó]n))?"],
        "total": [r"total[ \t]*a[ \t]*pagar", r"importe[ \t]*total",
                  r"total[ \t]*factura", r"total"],
        "subtotal": [r"base[ \t]*imponible", r"subtotal", r"importe[ \t]*neto"],
        "tax": [r"iva(?:[ \t]*\([^)]*\))?", r"impuestos?", r"igic"],
        "po": [r"(?:orden|pedido)[ \t]*(?:de[ \t]*compra)?"],
        "skip": [r"factura", r"fecha", r"cliente", r"direcci[oó]n", r"tel[eé]fono",
                 r"p[aá]gina", r"vencimiento", r"n[.ºo]"],
    },
    "fr": {
        "invoice_number": [r"facture[ \t]*(?:n[°ºo]?\.?|num[eé]ro)?",
                           r"n[°ºo]?[ \t]*de[ \t]*facture"],
        "date": [r"date(?:[ \t]*(?:de[ \t]*)?(?:facture|facturation|[eé]mission))?"],
        "total": [r"net[ \t]*[aà][ \t]*payer", r"total[ \t]*ttc",
                  r"montant[ \t]*(?:total|d[uû])", r"total"],
        "subtotal": [r"total[ \t]*ht", r"sous[- \t]*total", r"montant[ \t]*ht"],
        "tax": [r"t\.?v\.?a\.?(?:[ \t]*\([^)]*\))?", r"taxes?"],
        "po": [r"bon[ \t]*de[ \t]*commande", r"commande"],
        "skip": [r"facture", r"date", r"client", r"adresse", r"t[eé]l",
                 r"page", r"[eé]ch[eé]ance", r"n[°ºo]"],
    },
    "de": {
        "invoice_number": [r"rechnungs(?:nummer|nr\.?)", r"rechnung[ \t]*nr\.?"],
        "date": [r"rechnungsdatum", r"datum", r"belegdatum"],
        "total": [r"gesamtbetrag", r"rechnungsbetrag", r"endbetrag",
                  r"zahlbar", r"brutto(?:betrag)?", r"summe"],
        "subtotal": [r"nettobetrag", r"netto", r"zwischensumme"],
        "tax": [r"mehrwertsteuer", r"mwst\.?(?:[ \t]*\([^)]*\))?",
                r"ust\.?(?:[ \t]*\([^)]*\))?", r"umsatzsteuer"],
        "po": [r"bestell(?:nummer|nr\.?)", r"bestellung"],
        "skip": [r"rechnung", r"datum", r"kunde", r"anschrift", r"adresse",
                 r"tel", r"seite", r"lieferung", r"nr\."],
    },
    "pt": {
        "invoice_number": [r"fat(?:ura)?[ \t]*n[.ºo]?", r"factura[ \t]*n[.ºo]?",
                           r"n[uú]mero[ \t]*da[ \t]*fat(?:ura)?"],
        "date": [r"data(?:[ \t]*(?:de[ \t]*)?(?:emiss[aã]o|fatura|factura))?"],
        "total": [r"total[ \t]*a[ \t]*pagar", r"valor[ \t]*total", r"total"],
        "subtotal": [r"subtotal", r"valor[ \t]*(?:l[ií]quido|sem[ \t]*iva)"],
        "tax": [r"iva(?:[ \t]*\([^)]*\))?", r"impostos?"],
        "po": [r"(?:nota[ \t]*de[ \t]*)?encomenda", r"ordem[ \t]*de[ \t]*compra"],
        "skip": [r"fat(?:ura)?", r"factura", r"data", r"cliente", r"morada",
                 r"telefone", r"p[aá]gina", r"vencimento", r"n[.ºo]"],
    },
    "it": {
        "invoice_number": [r"fattura[ \t]*n[.°ºro]*", r"numero[ \t]*fattura"],
        "date": [r"data(?:[ \t]*(?:fattura|documento|emissione))?"],
        "total": [r"totale[ \t]*documento", r"totale[ \t]*fattura",
                  r"importo[ \t]*totale", r"totale"],
        "subtotal": [r"imponibile", r"totale[ \t]*imponibile", r"subtotale"],
        "tax": [r"i\.?v\.?a\.?(?:[ \t]*\([^)]*\))?", r"imposta"],
        "po": [r"ordine(?:[ \t]*di[ \t]*acquisto)?"],
        "skip": [r"fattura", r"data", r"cliente", r"indirizzo", r"tel",
                 r"pagina", r"scadenza", r"n[.°º]"],
    },
    "nl": {
        "invoice_number": [r"factuur(?:nummer|nr\.?)", r"factuur[ \t]*nr\.?"],
        "date": [r"factuurdatum", r"datum"],
        "total": [r"totaal[ \t]*te[ \t]*betalen", r"totaalbedrag",
                  r"te[ \t]*betalen", r"totaal"],
        "subtotal": [r"subtotaal", r"netto(?:bedrag)?"],
        "tax": [r"b\.?t\.?w\.?(?:[ \t]*\([^)]*\))?", r"omzetbelasting"],
        "po": [r"bestel(?:nummer|nr\.?)", r"inkooporder"],
        "skip": [r"factuur", r"datum", r"klant", r"adres", r"tel",
                 r"pagina", r"vervaldatum", r"nr\."],
    },
}


def labels(language: str, field: str) -> list:
    """Label fragments for one field in one language, or [].

    Returns [] for English and for UNDETERMINED, and that is the whole safety
    argument for this half of the module: `extraction.py` already has English
    patterns and appends these AFTER them, so an English or unrecognised
    document is offered nothing extra and behaves exactly as it always did.
    """
    return list((_LABELS.get(language) or {}).get(field) or [])


def uses_decimal_comma(language: str) -> bool:
    """Whether "1.234" in this language means one thousand two hundred and
    thirty-four rather than 1.234."""
    return language in DECIMAL_COMMA_LANGUAGES


# --------------------------------------------------------------------------
# dates
#
# The rule, and it is the same one the rest of this codebase applies to an
# ambiguous vendor or an unmatched PO: WHEN IT CANNOT BE RESOLVED, IT IS LEFT
# ALONE. `normalise_date` returns the original string unchanged rather than a
# best guess and never returns None for a value that was present -- because
# `rules.looks_like_an_invoice` tests that field for presence, and a
# normaliser that could empty it would be able to change a verdict. Turning a
# date into ISO is presentation; losing one is not.
# --------------------------------------------------------------------------
_MONTHS = {
    "en": ["january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december"],
    "es": ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
           "agosto", "septiembre", "octubre", "noviembre", "diciembre"],
    "fr": ["janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet",
           "aout", "septembre", "octobre", "novembre", "decembre"],
    "de": ["januar", "februar", "marz", "april", "mai", "juni", "juli",
           "august", "september", "oktober", "november", "dezember"],
    "pt": ["janeiro", "fevereiro", "marco", "abril", "maio", "junho", "julho",
           "agosto", "setembro", "outubro", "novembro", "dezembro"],
    "it": ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
           "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"],
    "nl": ["januari", "februari", "maart", "april", "mei", "juni", "juli",
           "augustus", "september", "oktober", "november", "december"],
}

_ISO_RE = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})\s*$")
_NUMERIC_RE = re.compile(r"^\s*(\d{1,2})[./\-](\d{1,2})[./\-](\d{2,4})\s*$")
_DAY_MONTH_YEAR_RE = re.compile(r"^\s*(\d{1,2})\.?\s*(?:de\s+|d[eu]\s+|)([a-z]{3,12})\.?"
                                r"\s*(?:de\s+|del\s+|)(\d{2,4})\s*$")


def _valid(year: int, month: int, day: int) -> bool:
    return (1 <= month <= 12
            and 1 <= day <= calendar.monthrange(year, month)[1]
            and 1900 <= year <= 2200)


def _year(raw: int) -> int:
    """Two-digit years. 26 is 2026, 98 is 1998 -- the usual pivot, applied
    once here rather than in three places."""
    if raw >= 100:
        return raw
    return 2000 + raw if raw < 70 else 1900 + raw


def normalise_date(raw, language: str):
    """(value, normalised) -- an ISO date, or the original string untouched.

    `normalised` says which happened, so a caller can record that a value was
    rewritten rather than leaving an auditor to compare it against the
    document by eye.

    Refuses in exactly the cases where refusing is the honest answer:

      * English, always. `03/04/2026` is 3 April in London and 4 March in
        Chicago and the document does not say which, so a normaliser would be
        picking one at random and stating it as fact.
      * UNDETERMINED, for the same reason with less information.
      * A month number above 12 in the second position -- the document is not
        day-first after all, and the safe reading is "we were wrong about the
        language", not "swap the fields".
      * Anything that is not a real calendar date (31 February, month 19).
    """
    if not isinstance(raw, str) or not raw.strip():
        return raw, False
    value = raw.strip()

    if _ISO_RE.match(value):
        return value, False          # already ISO; nothing to do and nothing to claim
    if False:
        return raw, False

    m = _NUMERIC_RE.match(value)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), _year(int(m.group(3)))
        if _valid(year, month, day):
            return "%04d-%02d-%02d" % (year, month, day), True
        return raw, False

    folded = _fold(value)
    m = _DAY_MONTH_YEAR_RE.match(folded)
    if m:
        names = _MONTHS.get(language) or []
        word = m.group(2)
        month = 0
        for index, name in enumerate(names, start=1):
            # A three-letter prefix is enough and is how invoices abbreviate
            # ("15 sept. 2026", "3 gen 2026"). Matched against the folded form
            # so an accented month name resolves too.
            if name.startswith(word) or word.startswith(name[:3]):
                month = index
                break
        if month:
            day, year = int(m.group(1)), _year(int(m.group(3)))
            if _valid(year, month, day):
                return "%04d-%02d-%02d" % (year, month, day), True
    return raw, False
