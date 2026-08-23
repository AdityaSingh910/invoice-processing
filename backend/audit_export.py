"""Per-invoice audit report export -- PDF and CSV.

WHAT THIS BUILDS, AND WHAT IT DOES NOT

A human-readable report about ONE run: its identity, its validation result,
where it came from (including email authentication evidence when it arrived
that way), its full activity history, and what happened with any rejection
notice. Everything here is read from data the run/review/email endpoints
already expose to the same caller -- this module writes nothing, and adds no
authorization boundary of its own (main.py gates both export endpoints with
the same `invoice:read` scope that already gates reading the run itself,
which is the whole of the authorization argument: nothing this report shows
is something the caller could not already fetch and read one field at a
time).

NO JSON DUMP. The PDF is built as headed sections, paragraphs and tables via
reportlab's platypus layer -- already a runtime dependency
(`sample_invoices/generate_invoices.py` uses it to build the demo invoices
this application ingests, so this adds no new package to the supply chain).

REUSES `portal.client_state()` for the same reason `notifications.py` does:
one vendor-safe rejection-reason vocabulary, not two.
"""
import csv
import io
import re
from datetime import datetime, timezone

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

import notifications
import portal
import storage

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


def safe_filename_stub(run: dict) -> str:
    """`invoice_<safe-token>_audit_report` -- the base name an export's
    Content-Disposition uses. Sanitised against everything: an invoice
    number is document content, chosen by whoever sent the PDF this run was
    read from, so it is exactly as trustworthy as any other attacker-supplied
    string and is reduced to letters, digits, underscore and hyphen only --
    airtight against path traversal or a header-breaking character, the same
    treatment `email_ingest.safe_attachment_filename` gives an attachment
    name for the same reason.
    """
    audit = run.get("audit") or {}
    raw = ((audit.get("invoice") or {}).get("invoice_number")
          or (run.get("extracted") or {}).get("invoice_number")
          or f"run-{run.get('id')}")
    safe = _UNSAFE.sub("-", str(raw)).strip("-")[:80]
    return f"invoice_{safe or ('run-' + str(run.get('id')))}_audit_report"


def _gather(run_id: int) -> dict:
    """Everything the report needs, fetched once. Returns None if the run
    does not exist -- the caller (main.py) turns that into a 404."""
    run = storage.get_run(run_id)
    if not run:
        return None

    invoice_activity = [{**a, "stream": "invoice"} for a in storage.list_activity(run_id)]
    email_row = storage.email_for_run(run_id)
    email_full, email_activity = None, []
    if email_row:
        email_full = storage.get_email_message(email_row["id"])
        email_activity = [{**a, "stream": "email"}
                          for a in storage.list_email_activity(email_row["id"])]

    history = sorted(invoice_activity + email_activity, key=lambda a: (a["created_at"], a["id"]))

    reasons = []
    if run.get("status") == "REJECTED":
        reasons = notifications.rejection_reasons(run)

    return {
        "run": run,
        "email": email_full,
        "history": history,
        "rejection_reasons": reasons,
        "last_send": notifications.last_successful_send(run_id),
        "send_history": notifications.rejection_email_history(run_id),
    }


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------
_CSV_FIELDS = [
    "invoice_id", "invoice_number", "vendor", "vendor_email", "invoice_status",
    "processing_status", "rejection_reason", "security_classification",
    "received_at", "processed_at", "reviewer",
    "rejection_email_status", "rejection_email_recipient", "rejection_email_sent_at",
    "audit_stream", "audit_event", "audit_timestamp", "audit_actor", "audit_result",
]


def _csv_cell(value):
    """One cell, neutralised against spreadsheet formula injection -- the same
    primitive `logs.csv_safe` already established for every other export in
    this codebase (§7d.8), imported rather than reimplemented."""
    import logs
    return logs.csv_safe(value)


def build_csv(run_id: int) -> str:
    data = _gather(run_id)
    if data is None:
        return None
    run, email = data["run"], data["email"]
    audit = run.get("audit") or {}
    invoice = audit.get("invoice") or {}
    extracted = run.get("extracted") or {}

    last_send = data["last_send"]
    fixed = {
        "invoice_id": run["id"],
        "invoice_number": invoice.get("invoice_number") or extracted.get("invoice_number") or "",
        "vendor": invoice.get("vendor") or extracted.get("vendor_name") or "",
        "vendor_email": (email or {}).get("from_address") or "",
        "invoice_status": run.get("status") or "",
        "processing_status": (extracted.get("extraction_method")
                              or (audit.get("extraction") or {}).get("method") or ""),
        "rejection_reason": "; ".join(data["rejection_reasons"]),
        "security_classification": (email or {}).get("classification") or "",
        "received_at": run.get("created_at") or "",
        "processed_at": run.get("reviewed_at") or "",
        "reviewer": run.get("reviewed_by") or "",
        "rejection_email_status": "SENT" if last_send else (
            "FAILED" if data["send_history"] else "NOT_SENT"),
        "rejection_email_recipient": (last_send.get("metadata") or {}).get("recipient")
                                     if last_send else "",
        "rejection_email_sent_at": last_send["created_at"] if last_send else "",
    }

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_CSV_FIELDS)
    rows = data["history"] or [None]
    for event in rows:
        row = dict(fixed)
        if event is not None:
            row.update({
                "audit_stream": event["stream"],
                "audit_event": event["event_type"],
                "audit_timestamp": event["created_at"],
                "audit_actor": event.get("actor") or "",
                "audit_result": event.get("note") or "",
            })
        else:
            row.update({"audit_stream": "", "audit_event": "", "audit_timestamp": "",
                       "audit_actor": "", "audit_result": ""})
        writer.writerow([_csv_cell(row[f]) for f in _CSV_FIELDS])
    return buf.getvalue()


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------
_STYLES = getSampleStyleSheet()
_H1 = ParagraphStyle("H1", parent=_STYLES["Heading1"], fontSize=16, spaceAfter=4)
_H2 = ParagraphStyle("H2", parent=_STYLES["Heading2"], fontSize=12, spaceBefore=14, spaceAfter=6,
                     textColor=colors.HexColor("#1a2b4c"))
_BODY = ParagraphStyle("Body", parent=_STYLES["Normal"], fontSize=9.5, leading=13)
_META = ParagraphStyle("Meta", parent=_STYLES["Normal"], fontSize=8.5, leading=12,
                       textColor=colors.HexColor("#555555"))


def _kv_table(rows):
    data = [[Paragraph(f"<b>{k}</b>", _BODY), Paragraph(str(v) if v not in (None, "") else "—", _BODY)]
           for k, v in rows]
    t = Table(data, colWidths=[1.8 * inch, 4.4 * inch])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#e5e5e5")),
    ]))
    return t


def build_pdf(run_id: int) -> bytes:
    data = _gather(run_id)
    if data is None:
        return None
    run, email = data["run"], data["email"]
    audit = run.get("audit") or {}
    invoice = audit.get("invoice") or {}
    extracted = run.get("extracted") or {}

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=LETTER,
                            topMargin=0.6 * inch, bottomMargin=0.6 * inch,
                            leftMargin=0.7 * inch, rightMargin=0.7 * inch,
                            title=f"Audit report — {run.get('id')}")
    story = [
        Paragraph("Invoice Audit Report", _H1),
        Paragraph(f"Generated {datetime.now(timezone.utc).strftime('%d %B %Y, %H:%M UTC')} "
                 f"&middot; Run #{run['id']}", _META),
        Spacer(1, 10),
    ]

    story.append(Paragraph("Invoice Information", _H2))
    story.append(_kv_table([
        ("Invoice number", invoice.get("invoice_number") or extracted.get("invoice_number")),
        ("Vendor", invoice.get("vendor") or extracted.get("vendor_name")),
        ("Vendor email", (email or {}).get("from_address")),
        ("Invoice date", extracted.get("invoice_date")),
        ("Amount", f"{invoice.get('total', extracted.get('total'))} "
                   f"{invoice.get('currency', extracted.get('currency')) or ''}".strip()),
        ("Automated decision", run.get("automated_decision")),
        ("Human decision", run.get("human_decision")),
        ("Final status", run.get("final_decision") or run.get("status")),
        ("Received", run.get("created_at")),
    ]))

    story.append(Paragraph("Validation", _H2))
    findings = run.get("reasons") or []
    if findings:
        rows = [[Paragraph("<b>Level</b>", _BODY), Paragraph("<b>Finding</b>", _BODY)]]
        for r in findings:
            level = (r.get("level") if isinstance(r, dict) else "info") or "info"
            text = r.get("text") if isinstance(r, dict) else str(r)
            rows.append([Paragraph(level.upper(), _BODY), Paragraph(text, _BODY)])
        t = Table(rows, colWidths=[0.9 * inch, 5.3 * inch])
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0f0f0")),
            ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#e5e5e5")),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(t)
    else:
        story.append(Paragraph("No validation findings recorded.", _BODY))

    if data["rejection_reasons"]:
        story.append(Paragraph("Rejection reason(s)", _H2))
        for reason in data["rejection_reasons"]:
            story.append(Paragraph(f"&bull; {reason}", _BODY))

    if email:
        story.append(Paragraph("Email Information", _H2))
        story.append(_kv_table([
            ("Original sender", email.get("from_address")),
            ("Subject", email.get("subject")),
            ("Received", email.get("received_at")),
            ("Attachments", email.get("attachment_count")),
            ("Has PDF attachment", "Yes" if email.get("has_pdf_attachment") else "No"),
        ]))
        story.append(Paragraph("Security", _H2))
        story.append(_kv_table([
            ("Classification", email.get("classification")),
            ("SPF", email.get("spf_result")),
            ("DKIM", email.get("dkim_result")),
            ("DMARC", email.get("dmarc_result")),
            ("Trusted sender", "Yes" if email.get("trusted_sender") else "No"),
        ]))

    story.append(Paragraph("Audit History", _H2))
    if data["history"]:
        rows = [[Paragraph("<b>When</b>", _BODY), Paragraph("<b>Event</b>", _BODY),
                Paragraph("<b>Actor</b>", _BODY), Paragraph("<b>Outcome</b>", _BODY)]]
        for e in data["history"]:
            rows.append([
                Paragraph(str(e.get("created_at") or ""), _BODY),
                Paragraph(e["event_type"].replace("_", " ").title(), _BODY),
                Paragraph(e.get("actor") or "system", _BODY),
                Paragraph((e.get("note") or "")[:200], _BODY),
            ])
        t = Table(rows, colWidths=[1.5 * inch, 1.6 * inch, 1.0 * inch, 2.1 * inch])
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0f0f0")),
            ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#e5e5e5")),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ]))
        story.append(t)
    else:
        story.append(Paragraph("No activity recorded.", _BODY))

    doc.build(story)
    return buf.getvalue()
