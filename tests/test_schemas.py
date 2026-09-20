import pytest
from pydantic import ValidationError

from app.schemas import InquiryCreate
from app.services.inquiry_service import request_hash


def test_message_is_trimmed_and_email_lowercased():
    m = InquiryCreate(message="  Need 200 units  ", customer_email="Sarah@ACME.example")
    assert m.message == "Need 200 units"
    assert m.customer_email == "sarah@acme.example"


@pytest.mark.parametrize("bad", ["", "   ", "x" * 5001])
def test_invalid_message_rejected(bad):
    with pytest.raises(ValidationError):
        InquiryCreate(message=bad)


def test_unknown_fields_rejected():
    with pytest.raises(ValidationError):
        InquiryCreate(message="hi", discount=20)


def test_request_hash_is_stable_and_body_sensitive():
    a = InquiryCreate(message="Need 200 units")
    assert request_hash(a) == request_hash(InquiryCreate(message="Need 200 units"))
    assert request_hash(a) != request_hash(InquiryCreate(message="Need 300 units"))
