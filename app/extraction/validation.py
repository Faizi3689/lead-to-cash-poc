"""Never trust model output: parse -> schema-validate -> ground against the source text."""
import json
import re
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ExtractedInquiry(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    product_name: str | None = Field(default=None, max_length=200)
    quantity: int | None = Field(default=None, gt=0, le=1_000_000)
    requested_discount_pct: Decimal | None = Field(default=None, ge=0, le=100, decimal_places=2)
    delivery_timeframe: str | None = Field(default=None, max_length=100)
    appointment_requested: bool = False
    appointment_preference: str | None = Field(default=None, max_length=100)
    intent: Literal["quote_request", "question", "complaint", "other"]
    confidence: float = Field(ge=0, le=1)
    missing_fields: list[str] = Field(default_factory=list, max_length=10)


class ParseResult(BaseModel):
    ok: bool
    data: ExtractedInquiry | None = None
    parsed_json: dict | None = None
    errors: list[str] = []


def parse_and_validate(raw: str) -> ParseResult:
    text = raw.strip()
    # Tolerate ```json fences, nothing else.
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        return ParseResult(ok=False, errors=[f"output is not valid JSON ({exc.msg})"])
    if not isinstance(obj, dict):
        return ParseResult(ok=False, errors=["output must be a JSON object"])
    try:
        return ParseResult(ok=True, data=ExtractedInquiry.model_validate(obj), parsed_json=obj)
    except ValidationError as exc:
        errors = [f"{'.'.join(str(p) for p in e['loc']) or 'object'}: {e['msg']}" for e in exc.errors()]
        return ParseResult(ok=False, parsed_json=obj, errors=errors)


def _number_in_text(value: Decimal | int, text: str) -> bool:
    """True if the number literally appears in the text (handles 1,000 and 20.0 vs 20)."""
    normalised = text.replace(",", "")
    candidates = {str(value)}
    if isinstance(value, Decimal):
        candidates |= {format(value.normalize(), "f"), str(int(value)) if value == value.to_integral() else ""}
    return any(c and re.search(rf"(?<![\d.]){re.escape(c)}(?![\d])", normalised) for c in candidates)


def grounding_issues(data: ExtractedInquiry, source_text: str) -> list[str]:
    """Numbers the model reports must be present in what the customer actually wrote."""
    issues = []
    if data.quantity is not None and not _number_in_text(data.quantity, source_text):
        issues.append(f"quantity {data.quantity} does not appear in the message")
    if data.requested_discount_pct is not None and not _number_in_text(data.requested_discount_pct, source_text):
        issues.append(f"discount {data.requested_discount_pct}% does not appear in the message")
    return issues
