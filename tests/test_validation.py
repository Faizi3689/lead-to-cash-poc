from decimal import Decimal

from app.extraction.validation import ExtractedInquiry, grounding_issues, parse_and_validate

GOOD = ('{"product_name":"Product X","quantity":200,"requested_discount_pct":20,"delivery_timeframe":"next month",'
        '"appointment_requested":true,"appointment_preference":"tomorrow","intent":"quote_request",'
        '"confidence":0.9,"missing_fields":[]}')
MSG = "We need 200 units of Product X next month, can you give us 20% discount? I'd like to talk tomorrow."


def test_valid_output_parses():
    r = parse_and_validate(GOOD)
    assert r.ok and r.data.quantity == 200 and r.data.requested_discount_pct == Decimal("20")


def test_code_fences_tolerated():
    assert parse_and_validate(f"```json\n{GOOD}\n```").ok


def test_non_json_rejected():
    r = parse_and_validate("Sure! Here you go: 200 units")
    assert not r.ok and "not valid JSON" in r.errors[0]


def test_schema_violations_rejected():
    bad = GOOD.replace('"requested_discount_pct":20', '"requested_discount_pct":150')
    r = parse_and_validate(bad)
    assert not r.ok and any("requested_discount_pct" in e for e in r.errors)
    assert not parse_and_validate(GOOD.replace('"quantity":200', '"quantity":-5')).ok
    assert not parse_and_validate(GOOD.replace('"confidence":0.9', '"confidence":0.9,"extra":1')).ok
    assert not parse_and_validate('[1,2,3]').ok


def test_grounding_accepts_numbers_in_message():
    data = parse_and_validate(GOOD).data
    assert grounding_issues(data, MSG) == []


def test_grounding_flags_invented_numbers():
    data = ExtractedInquiry(product_name="Product X", quantity=200, requested_discount_pct=Decimal("50"),
                            intent="quote_request", confidence=0.95)
    issues = grounding_issues(data, MSG)
    assert len(issues) == 1 and "50" in issues[0]


def test_grounding_handles_thousands_separator_and_decimals():
    data = ExtractedInquiry(quantity=1500, requested_discount_pct=Decimal("12.5"),
                            intent="quote_request", confidence=0.9)
    assert grounding_issues(data, "Need 1,500 units at 12.5% off") == []
    assert grounding_issues(data, "Need 15000 units at 12.55% off") != []
