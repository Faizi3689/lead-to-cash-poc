"""Deterministic invoice / expense checks. Pure functions: data in, findings out.

Duplicate detection needs the database, so the service passes in the candidate matches it found.
"""
from dataclasses import dataclass, field
from decimal import Decimal

from app.rules.engine import AUTO_APPROVE_MAX_PCT
from app.services.pricing import money

TOLERANCE = Decimal("0.01")

INVOICE_REQUIRED = ("invoice_number", "counterparty_name", "invoice_date", "currency", "subtotal", "total")
EXPENSE_REQUIRED = ("employee_name", "category", "amount", "currency", "expense_date")

# Findings a human may ACCEPT (override). The others must be corrected or the document voided.
#   duplicate_invoice : "I checked, it is a different invoice"
#   over_threshold    : "finance signs off on the amount"
#   low_confidence    : "I checked the AI-read values against the document"
OVERRIDABLE = {"duplicate_invoice", "over_threshold", "low_confidence"}

# Exception types owned by the deterministic validator (opened AND cleared by revalidation).
VALIDATOR_TYPES = {"missing_field", "amount_mismatch", "unauthorized_discount", "duplicate_invoice",
                   "over_threshold"}
# Raised during AI capture; cleared only when a human corrects or confirms the data.
AI_CAPTURE_TYPES = {"low_confidence", "llm_unavailable", "invalid_ai_output"}


@dataclass
class Finding:
    type: str
    severity: str
    message: str
    details: dict = field(default_factory=dict)


def _d(v) -> Decimal | None:
    return None if v is None else Decimal(str(v))


def missing_fields(values: dict, required: tuple[str, ...]) -> list[Finding]:
    missing = sorted(f for f in required if values.get(f) in (None, ""))
    if not missing:
        return []
    return [Finding("missing_field", "high", f"missing required fields: {', '.join(missing)}",
                    {"fields": missing})]


def invoice_arithmetic(inv: dict) -> list[Finding]:
    out = []
    subtotal, total = _d(inv.get("subtotal")), _d(inv.get("total"))
    pct, disc, tax = _d(inv.get("discount_pct")), _d(inv.get("discount_amount")), _d(inv.get("tax_amount"))

    if subtotal is not None and pct is not None and disc is not None:
        expected = money(subtotal * pct / 100)
        if abs(expected - disc) > TOLERANCE:
            out.append(Finding("amount_mismatch", "high",
                               f"discount amount {disc} does not equal {pct}% of {subtotal} ({expected})",
                               {"check": "discount_amount", "expected": str(expected), "actual": str(disc)}))
    if subtotal is not None and total is not None:
        expected = subtotal - (disc or 0) + (tax or 0)
        if abs(expected - total) > TOLERANCE:
            out.append(Finding("amount_mismatch", "high",
                               f"total {total} does not add up (subtotal - discount + tax = {expected})",
                               {"check": "total", "expected": str(expected), "actual": str(total)}))
    lines = [li for li in inv.get("line_items") or [] if li.get("quantity") is not None
             and li.get("unit_price") is not None]
    if subtotal is not None and lines:
        line_sum = money(sum(Decimal(str(li["quantity"])) * Decimal(str(li["unit_price"])) for li in lines))
        if abs(line_sum - subtotal) > TOLERANCE:
            out.append(Finding("amount_mismatch", "high",
                               f"line items add up to {line_sum}, but subtotal is {subtotal}",
                               {"check": "line_items", "expected": str(line_sum), "actual": str(subtotal)}))
    return out


def effective_discount_pct(inv: dict) -> Decimal | None:
    pct = _d(inv.get("discount_pct"))
    if pct is not None:
        return pct
    subtotal, disc = _d(inv.get("subtotal")), _d(inv.get("discount_amount"))
    if subtotal and disc:
        return money(disc / subtotal * 100)
    return None


def against_order(inv: dict, order: dict | None, order_reference: str | None) -> list[Finding]:
    """Compare the invoice with what was approved (the order carries the approved terms)."""
    out = []
    pct = effective_discount_pct(inv)
    if order is None:
        if order_reference:
            out.append(Finding("missing_field", "high", f"order reference '{order_reference}' matches no order",
                               {"fields": ["order_reference"], "order_reference": order_reference}))
        if pct is not None and pct > AUTO_APPROVE_MAX_PCT:
            out.append(Finding("unauthorized_discount", "critical",
                               f"discount {pct}% has no approval on record (only up to "
                               f"{AUTO_APPROVE_MAX_PCT}% is automatic)",
                               {"invoice_discount_pct": str(pct), "approved_discount_pct": None}))
        return out

    approved = Decimal(str(order["discount_pct"]))
    if pct is not None and pct > approved:
        out.append(Finding("unauthorized_discount", "critical",
                           f"invoice discount {pct}% exceeds the approved {approved}% "
                           f"(order {order['order_number']})",
                           {"invoice_discount_pct": str(pct), "approved_discount_pct": str(approved),
                            "order_number": order["order_number"]}))
    total = _d(inv.get("total"))
    if total is not None and abs(total - Decimal(str(order["total"]))) > TOLERANCE:
        out.append(Finding("amount_mismatch", "high",
                           f"invoice total {total} differs from order {order['order_number']} total {order['total']}",
                           {"check": "order_total", "expected": str(order["total"]), "actual": str(total)}))
    return out


def duplicates(exact: list[dict], suspected: list[dict]) -> list[Finding]:
    if exact:
        return [Finding("duplicate_invoice", "high",
                        f"same supplier and invoice number as {exact[0]['ref']}",
                        {"kind": "exact", "matches": [m["id"] for m in exact]})]
    if suspected:
        return [Finding("duplicate_invoice", "medium",
                        f"same supplier, amount and date as {suspected[0]['ref']} (possible re-submission)",
                        {"kind": "suspected", "matches": [m["id"] for m in suspected]})]
    return []


def over_threshold(amount, threshold, what: str, approver: str) -> list[Finding]:
    a = _d(amount)
    if a is not None and a > Decimal(str(threshold)):
        return [Finding("over_threshold", "medium", f"{what} {a} is above the {threshold} limit: "
                        f"{approver} sign-off required", {"amount": str(a), "threshold": str(threshold),
                                                          "approver": approver})]
    return []


def expense_receipt(exp: dict, receipt_required_above) -> list[Finding]:
    a = _d(exp.get("amount"))
    if a is not None and a > Decimal(str(receipt_required_above)) and not exp.get("receipt_ref"):
        return [Finding("missing_field", "high", f"receipt required for expenses above {receipt_required_above}",
                        {"fields": ["receipt_ref"]})]
    return []
