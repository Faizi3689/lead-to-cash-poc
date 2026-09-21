"""Unit tests for the pure financial checks (no database)."""
from decimal import Decimal

from app.rules import financial_checks as c

CLEAN = {"invoice_number": "SUP-1", "counterparty_name": "Northwind", "invoice_date": "2026-09-20",
         "currency": "USD", "subtotal": Decimal("10000.00"), "discount_pct": Decimal("12"),
         "discount_amount": Decimal("1200.00"), "tax_amount": Decimal("0"), "total": Decimal("8800.00"),
         "line_items": [{"quantity": 200, "unit_price": "50.00"}]}
ORDER = {"order_number": "SO-000001", "discount_pct": Decimal("12.00"), "total": Decimal("8800.00")}


def types(findings):
    return sorted(f.type for f in findings)


def test_clean_invoice_has_no_findings():
    assert c.missing_fields(CLEAN, c.INVOICE_REQUIRED) == []
    assert c.invoice_arithmetic(CLEAN) == []
    assert c.against_order(CLEAN, ORDER, None) == []


def test_missing_fields():
    f = c.missing_fields({**CLEAN, "invoice_number": None, "total": ""}, c.INVOICE_REQUIRED)
    assert types(f) == ["missing_field"] and f[0].details["fields"] == ["invoice_number", "total"]  # sorted


def test_arithmetic_errors():
    assert types(c.invoice_arithmetic({**CLEAN, "total": Decimal("8900.00")})) == ["amount_mismatch"]
    assert types(c.invoice_arithmetic({**CLEAN, "discount_amount": Decimal("1000.00")})) == \
        ["amount_mismatch", "amount_mismatch"]        # discount amount AND total no longer add up
    assert types(c.invoice_arithmetic({**CLEAN, "line_items": [{"quantity": 199, "unit_price": "50.00"}]})) == \
        ["amount_mismatch"]
    assert c.invoice_arithmetic({**CLEAN, "total": Decimal("8800.01")}) == []   # within 1 cent


def test_unauthorised_discount_against_approval():
    inv = {**CLEAN, "discount_pct": Decimal("20"), "discount_amount": Decimal("2000.00"),
           "total": Decimal("8000.00")}
    f = c.against_order(inv, ORDER, None)
    assert types(f) == ["amount_mismatch", "unauthorized_discount"]
    ua = next(x for x in f if x.type == "unauthorized_discount")
    assert ua.severity == "critical" and ua.details["approved_discount_pct"] == "12.00"


def test_lower_discount_than_approved_is_not_unauthorised():
    inv = {**CLEAN, "discount_pct": Decimal("10"), "discount_amount": Decimal("1000.00"), "total": Decimal("9000.00")}
    assert "unauthorized_discount" not in types(c.against_order(inv, ORDER, None))


def test_discount_without_any_approval():
    assert types(c.against_order({**CLEAN, "discount_pct": Decimal("8")}, None, None)) == ["unauthorized_discount"]
    assert c.against_order({**CLEAN, "discount_pct": Decimal("5")}, None, None) == []


def test_implied_discount_from_amount():
    inv = {**CLEAN, "discount_pct": None, "discount_amount": Decimal("2000.00")}
    assert c.effective_discount_pct(inv) == Decimal("20.00")


def test_unknown_order_reference():
    small = {**CLEAN, "discount_pct": Decimal("5")}
    assert types(c.against_order(small, None, "SO-999999")) == ["missing_field"]
    # with a 12% discount and no order found, the discount is unauthorised as well
    assert types(c.against_order(CLEAN, None, "SO-999999")) == ["missing_field", "unauthorized_discount"]


def test_thresholds_and_receipts():
    assert c.over_threshold(Decimal("1000.00"), 1000, "expense", "manager") == []
    assert types(c.over_threshold(Decimal("1000.01"), 1000, "expense", "manager")) == ["over_threshold"]
    assert types(c.expense_receipt({"amount": Decimal("30"), "receipt_ref": None}, 25)) == ["missing_field"]
    assert c.expense_receipt({"amount": Decimal("20"), "receipt_ref": None}, 25) == []


def test_only_judgement_calls_are_overridable():
    assert c.OVERRIDABLE == {"duplicate_invoice", "over_threshold", "low_confidence"}
    assert "unauthorized_discount" not in c.OVERRIDABLE and "amount_mismatch" not in c.OVERRIDABLE
