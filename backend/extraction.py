"""Turns an arbitrary invoice PDF into an ExtractedInvoice.

Designed to handle invoices this process has never seen before, so there are
three extraction routes and they degrade in a defined order:

  1. LLM over embedded text   -- best for clean PDFs from unknown vendors.
  2. LLM over page images     -- handles scanned/image-only PDFs (replaces OCR).
  3. Regex heuristics         -- always available, no API key, no network.

Routes 1 and 2 call Google Gemini (google-genai, Google AI Studio) and need
GEMINI_API_KEY. Route 3 needs nothing.

Whatever the route, the output schema is identical, so matching and rules
never need to know which one ran. When nothing can be read, the extractor
returns empty fields rather than guessing -- the rules layer then routes the
invoice to human review.
"""
import io
import json
import os
import re
from typing import List, Optional, Tuple

import pdfplumber

import config
from schemas import ExtractedInvoice, LineItem

MONEY = r"([\-\(]?\s*[\$€£₹]?\s*[\d][\d,\s]*(?:\.\d{1,2})?\)?)"

CURRENCY_SIGNS = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR"}
CURRENCY_CODES = ["USD", "EUR", "GBP", "INR", "AUD", "CAD", "SGD", "JPY", "CHF", "SEK", "AED"]


class PdfUnreadable(Exception):
    """Raised when the file cannot be opened as a PDF at all."""


# --------------------------------------------------------------------------
# text / image acquisition
# --------------------------------------------------------------------------

def _open_pdf(pdf_bytes: bytes):
    try:
        return pdfplumber.open(io.BytesIO(pdf_bytes))
    except Exception as exc:
        msg = str(exc).lower()
        if "password" in msg or "encrypt" in msg:
            raise PdfUnreadable("PDF is password-protected or encrypted.") from exc
        raise PdfUnreadable("File could not be opened as a PDF (%s)." % exc.__class__.__name__) from exc


def extract_text(pdf_bytes: bytes) -> Tuple[str, int, bool]:
    """Returns (text, page_count, has_text_layer)."""
    parts = []
    with _open_pdf(pdf_bytes) as pdf:
        page_count = len(pdf.pages)
        for page in pdf.pages[: config.MAX_PAGES_TEXT]:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                parts.append("")
    text = "\n".join(parts).strip()
    return text, page_count, len(text) >= 25


def render_pages_png(pdf_bytes: bytes, max_pages: int) -> List[bytes]:
    """Rasterise the first N pages to PNG. Pure-python (pypdfium2), so there is
    no poppler/tesseract system dependency to install."""
    import pypdfium2 as pdfium

    images = []
    doc = pdfium.PdfDocument(io.BytesIO(pdf_bytes))
    try:
        for i in range(min(len(doc), max_pages)):
            bitmap = doc[i].render(scale=2)
            pil = bitmap.to_pil().convert("RGB")
            # keep the longest edge sane so requests stay small
            if max(pil.size) > 1800:
                ratio = 1800 / max(pil.size)
                pil = pil.resize((int(pil.width * ratio), int(pil.height * ratio)))
            buf = io.BytesIO()
            pil.save(buf, format="PNG", optimize=True)
            images.append(buf.getvalue())
    finally:
        doc.close()
    return images


# --------------------------------------------------------------------------
# LLM extraction
# --------------------------------------------------------------------------

SCHEMA_PROMPT = """You extract structured data from vendor invoices.

Return ONLY minified JSON, no prose and no code fences, with exactly these keys:
{"vendor_name": string|null, "invoice_number": string|null, "invoice_date": string|null,
 "po_references": [string], "line_items": [{"description": string, "quantity": number|null,
 "unit_price": number|null, "amount": number|null}], "subtotal": number|null,
 "tax": number|null, "total": number|null, "currency": string}

Rules:
- vendor_name is the company ISSUING the invoice (the payee), not the customer being billed.
- invoice_date in ISO YYYY-MM-DD when the date is unambiguous, otherwise copy it verbatim.
- total is the final amount payable including tax.
- po_references: any purchase order identifiers referenced anywhere on the document.
- currency: 3-letter ISO code, inferred from symbols or text. Default "USD" only if there is no signal.
- Numbers must be plain JSON numbers: no currency symbols, no thousands separators.
- Use null for anything genuinely not present. NEVER invent or infer a missing value."""


def _parse_llm_json(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        raw = raw[start : end + 1]
    return json.loads(raw)


def _invoice_from_payload(data: dict, raw_text: str, method: str) -> ExtractedInvoice:
    def num(v):
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            return _to_float(v)
        return None

    items = []
    for li in data.get("line_items") or []:
        if not isinstance(li, dict):
            continue
        items.append({
            "description": str(li.get("description") or "").strip(),
            "quantity": num(li.get("quantity")),
            "unit_price": num(li.get("unit_price")),
            "amount": num(li.get("amount")),
        })

    refs = [str(r).strip() for r in (data.get("po_references") or []) if str(r).strip()]
    cur = (data.get("currency") or "USD").strip().upper()[:3] or "USD"

    return ExtractedInvoice(
        vendor_name=(data.get("vendor_name") or None),
        invoice_number=(str(data["invoice_number"]).strip() if data.get("invoice_number") else None),
        invoice_date=(str(data["invoice_date"]).strip() if data.get("invoice_date") else None),
        po_references=refs,
        line_items=items,
        subtotal=num(data.get("subtotal")),
        tax=num(data.get("tax")),
        total=num(data.get("total")),
        currency=cur,
        raw_text=raw_text,
        extraction_method=method,
    )


def _client():
    """A Gemini client bound to the key from the environment / .env.

    Imported lazily so the module still imports, and the regex route still runs,
    on a machine where google-genai was never installed.
    """
    from google import genai
    return genai.Client(api_key=config.api_key())


def _json_config():
    """Ask the API for JSON directly rather than hoping prose comes back clean.

    `_parse_llm_json` is still applied to whatever returns -- response_mime_type
    is a strong constraint, not a guarantee, and the fallback costs nothing.
    """
    from google.genai import types
    return types.GenerateContentConfig(response_mime_type="application/json")


def llm_extract_text(text: str, prompt: str = SCHEMA_PROMPT) -> ExtractedInvoice:
    """Route 1: read the fields out of an embedded text layer."""
    # Hold the client in a local. `_client().models.generate_content(...)` leaves
    # the Client itself unreferenced, and google-genai closes its HTTP transport
    # when the Client is collected -- which can happen before the call completes,
    # surfacing as "Cannot send a request, as the client has been closed."
    client = _client()
    resp = client.models.generate_content(
        model=config.EXTRACTION_MODEL,
        contents=[prompt, "Invoice text:\n\n" + text[:60000]],
        config=_json_config(),
    )
    return _invoice_from_payload(_parse_llm_json(resp.text), text, "llm (text)")


def llm_extract_vision(png_bytes, prompt: str = SCHEMA_PROMPT) -> ExtractedInvoice:
    """Route 2: read the fields off rasterised page images.

    `png_bytes` takes either one PNG or a list of them, so the caller can keep
    sending the first `MAX_PAGES_VISION` pages of a multi-page scan rather than
    silently losing everything after page one.
    """
    from google.genai import types

    pages = [png_bytes] if isinstance(png_bytes, (bytes, bytearray)) else list(png_bytes)

    contents = [prompt]
    for png in pages:
        contents.append(types.Part.from_bytes(data=png, mime_type="image/png"))
    contents.append("Extract the invoice fields from these page image(s).")

    client = _client()   # local reference -- see llm_extract_text
    resp = client.models.generate_content(
        model=config.EXTRACTION_MODEL,
        contents=contents,
        config=_json_config(),
    )
    inv = _invoice_from_payload(_parse_llm_json(resp.text), "", "llm (vision)")
    inv.raw_text = "[no embedded text layer - fields read from page images]"
    return inv


# --------------------------------------------------------------------------
# regex fallback
# --------------------------------------------------------------------------

def _to_float(s) -> Optional[float]:
    if s is None:
        return None
    s = str(s).strip()
    neg = s.startswith("(") and s.endswith(")") or s.startswith("-")
    s = re.sub(r"[^\d.,]", "", s)
    if not s:
        return None
    # 1.234,56 (EU) vs 1,234.56 (US)
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        frag = s.split(",")[-1]
        s = s.replace(",", ".") if len(frag) == 2 and s.count(",") == 1 else s.replace(",", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _first(text: str, patterns, group=1) -> Optional[str]:
    for pat in patterns:
        m = re.search(pat, text, re.I | re.M)
        if m and m.group(group):
            val = m.group(group).strip(" :.-\t")
            if val:
                return val
    return None


def _detect_currency(text: str) -> str:
    for code in CURRENCY_CODES:
        if re.search(r"\b%s\b" % code, text):
            return code
    for sign, code in CURRENCY_SIGNS.items():
        if sign in text:
            return code
    return "USD"


def _guess_vendor(text: str) -> Optional[str]:
    """The issuing company is usually in the letterhead. Walk the first lines and
    take the first that looks like a company name rather than a label/address."""
    skip = re.compile(
        r"^(invoice|tax invoice|bill|statement|receipt|page\b|date\b|due\b|to\b|from\b|"
        r"bill to|ship to|sold to|customer|account|purchase order|po\b|vat|gst|tel|phone|"
        r"email|www\.|http)", re.I)
    for line in text.splitlines()[:14]:
        line = line.strip()
        if len(line) < 3 or len(line) > 70:
            continue
        if skip.match(line):
            continue
        if re.match(r"^[\d\W]+$", line):        # numbers/punctuation only
            continue
        if re.search(r"\d{4,}", line):          # looks like an ID or postcode line
            continue
        letters = sum(c.isalpha() for c in line)
        if letters >= 3:
            return line
    return None


def regex_extract(text: str) -> ExtractedInvoice:
    inv = ExtractedInvoice(raw_text=text, extraction_method="regex")

    inv.invoice_number = _first(text, [
        r"^[ \t]*(?:tax[ \t]*)?invoice[ \t]*(?:#|no\.?|nu?mb?e?r|id)[ \t]*[:\-#]?[ \t]*([A-Za-z0-9][\w\-\/]*)",
        r"^[ \t]*(?:#|no\.?)[ \t]*[:\-]?[ \t]*(INV[\w\-\/]+)",
        r"\b(INV[-–—_/]?\d[\w\-\/]*)\b",
        r"^[ \t]*invoice[ \t]*[:\-][ \t]*([A-Za-z0-9][\w\-\/]*)",
    ])

    inv.invoice_date = _first(text, [
        r"^[ \t]*(?:invoice|bill|document)?[ \t]*date(?:[ \t]*of[ \t]*issue)?[ \t]*[:\-]?[ \t]*"
        r"(\d{1,4}[\/\-\.]\d{1,2}[\/\-\.]\d{1,4})",
        r"^[ \t]*(?:invoice|bill|document)?[ \t]*date(?:[ \t]*of[ \t]*issue)?[ \t]*[:\-]?[ \t]*"
        r"(\d{1,2}[ \t]+[A-Za-z]{3,9},?[ \t]+\d{2,4})",
        r"^[ \t]*(?:invoice|bill|document)?[ \t]*date(?:[ \t]*of[ \t]*issue)?[ \t]*[:\-]?[ \t]*"
        r"([A-Za-z]{3,9}[ \t]+\d{1,2},?[ \t]+\d{2,4})",
        r"^[ \t]*date[ \t]*[:\-][ \t]*(.+?)[ \t]*$",
    ])

    refs = re.findall(
        r"(?:P\.?O\.?|purchase[ \t]*order)[ \t#:\-]*((?:PO[-–—_]?)?\d[\w\-\/]*)", text, re.I)
    refs += re.findall(r"\b(PO[-–—_]?\d{3,}[\w\-\/]*)\b", text, re.I)
    seen, cleaned = set(), []
    for r in refs:
        r = r.strip(" :.-")
        if r and r.lower() not in seen:
            seen.add(r.lower())
            cleaned.append(r)
    # "PO Number: PO-1002" yields both "1002" and "PO-1002". Drop any reference
    # that is contained in a longer one so only the most specific form survives --
    # a bare "1002" could otherwise collide with an unrelated PO.
    inv.po_references = [
        r for r in cleaned
        if not any(other != r and r.lower() in other.lower() for other in cleaned)
    ]

    inv.vendor_name = _guess_vendor(text)

    inv.subtotal = _to_float(_first(text, [
        r"^[ \t]*sub[ \t]*-?[ \t]*total[ \t]*[:\-]?[ \t]*" + MONEY,
        r"^[ \t]*net[ \t]*(?:amount|total)[ \t]*[:\-]?[ \t]*" + MONEY,
    ]))
    inv.tax = _to_float(_first(text, [
        r"^[ \t]*(?:sales[ \t]*)?tax(?:[ \t]*\([^)]*\))?[ \t]*[:\-]?[ \t]*" + MONEY,
        r"^[ \t]*(?:VAT|GST|IGST|CGST)(?:[ \t]*\([^)]*\))?[ \t]*[:\-]?[ \t]*" + MONEY,
    ]))
    inv.total = _to_float(_first(text, [
        r"^[ \t]*(?:total[ \t]*(?:amount[ \t]*)?due|amount[ \t]*due|balance[ \t]*due|"
        r"grand[ \t]*total|total[ \t]*payable|invoice[ \t]*total)[ \t]*[:\-]?[ \t]*" + MONEY,
        r"^[ \t]*total[ \t]*[:\-]?[ \t]*" + MONEY,
    ]))
    # A "total" that merely repeated the subtotal is not a usable total.
    if inv.total is None and inv.subtotal is not None and inv.tax is not None:
        inv.total = round(inv.subtotal + inv.tax, 2)

    inv.currency = _detect_currency(text)

    items = []
    for line in text.splitlines():
        m = re.match(r"^(.{3,70}?)\s{1,}(\d+(?:[.,]\d+)?)\s+" + MONEY + r"\s+" + MONEY + r"\s*$",
                     line.strip())
        if m:
            items.append(LineItem(
                description=m.group(1).strip(),
                quantity=_to_float(m.group(2)),
                unit_price=_to_float(m.group(3)),
                amount=_to_float(m.group(4)),
            ).__dict__)
    inv.line_items = items
    return inv


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

def extract_invoice(pdf_bytes: bytes, pre: Optional[Tuple[str, int, bool]] = None
                    ) -> Tuple[ExtractedInvoice, dict]:
    """Runs the best available extraction route.

    Returns (invoice, info) where info describes what actually happened so the
    pipeline can report it honestly in the run view.

    `pre` is an already-computed (text, page_count, has_text_layer) tuple. The
    pipeline reads the text layer in its own stage so it can time and report that
    separately, and passes the result here rather than re-opening the PDF.
    """
    info = {"page_count": 0, "has_text_layer": False, "route": None,
            "vision_used": False, "notes": [], "error": None}

    text, page_count, has_text = pre if pre is not None else extract_text(pdf_bytes)
    info["page_count"] = page_count
    info["has_text_layer"] = has_text

    use_llm = config.has_api_key()

    # Route 1: text present
    if has_text:
        if use_llm:
            try:
                inv = llm_extract_text(text)
                info["route"] = "llm-text"
                return inv, info
            except Exception as exc:
                info["notes"].append("LLM text extraction failed (%s); used regex instead."
                                     % exc.__class__.__name__)
        inv = regex_extract(text)
        info["route"] = "regex"
        return inv, info

    # Route 2: no text layer -> vision
    if use_llm:
        try:
            images = render_pages_png(pdf_bytes, config.MAX_PAGES_VISION)
            if images:
                inv = llm_extract_vision(images)
                info["route"] = "llm-vision"
                info["vision_used"] = True
                if page_count > config.MAX_PAGES_VISION:
                    info["notes"].append("Only the first %d of %d pages were read."
                                         % (config.MAX_PAGES_VISION, page_count))
                return inv, info
        except Exception as exc:
            info["notes"].append("Vision extraction failed (%s)." % exc.__class__.__name__)

    # Route 3: nothing readable -- return empty rather than guess
    info["route"] = "none"
    info["notes"].append(
        "No embedded text and no vision extraction available. Set GEMINI_API_KEY "
        "to read scanned invoices." if not use_llm else
        "No embedded text and vision extraction did not return usable fields.")
    inv = ExtractedInvoice(raw_text="", extraction_method="none")
    return inv, info
