"""Shared data shapes for the invoice pipeline."""
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class LineItem:
    description: str = ""
    quantity: Optional[float] = None
    unit_price: Optional[float] = None
    amount: Optional[float] = None


@dataclass
class ExtractedInvoice:
    vendor_name: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None
    po_references: list = field(default_factory=list)
    line_items: list = field(default_factory=list)
    subtotal: Optional[float] = None
    tax: Optional[float] = None
    total: Optional[float] = None
    currency: str = "USD"
    raw_text: str = ""
    extraction_method: str = "regex"

    def to_dict(self):
        d = asdict(self)
        # raw_text can be long; keep it but the API layer may trim for the UI
        return d


@dataclass
class StageLog:
    name: str
    status: str  # "ok" | "warn" | "fail" | "info"
    detail: str

    def to_dict(self):
        return asdict(self)


@dataclass
class RunResult:
    run_id: Optional[int]
    filename: str
    status: str  # APPROVED | NEEDS_REVIEW | REJECTED
    reasons: list
    extracted: dict
    po_match: dict
    stages: list
    created_at: Optional[str] = None

    def to_dict(self):
        return {
            "run_id": self.run_id,
            "filename": self.filename,
            "status": self.status,
            "reasons": self.reasons,
            "extracted": self.extracted,
            "po_match": self.po_match,
            "stages": self.stages,
            "created_at": self.created_at,
        }
