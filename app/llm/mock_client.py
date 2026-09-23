"""Deterministic offline 'LLM' for tests and demos. It returns JSON TEXT, so the exact same
parsing + validation pipeline runs as with OpenAI.

Failure simulation (put the marker anywhere in the customer message):
  [[mock:timeout]]         -> raises LLMUnavailable (timeout)
  [[mock:invalid_json]]    -> returns text that is not JSON
  [[mock:low_confidence]]  -> confidence 0.40
  [[mock:hallucinate]]     -> invents a 50% discount that is not in the message
"""
import json
import re

from app.llm.base import LLMResponse, LLMUnavailable

_QTY = re.compile(r"(\d[\d,]*)\s*(?:units?|pcs|pieces|cases|boxes|bottles|cartons)\b", re.I)
_DISCOUNT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_PRODUCT = re.compile(r"\bproduct\s+([a-z0-9-]+)", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_NAME = re.compile(r"\b(?:I am|I'm|this is|regards,|thanks,)\s+([A-Z][a-z]+(?: [A-Z][a-z]+)?)", re.I)
_APPOINTMENT = re.compile(r"\b(talk|call|meet|meeting|discuss|speak)\b", re.I)
_WHEN = re.compile(r"\b(today|tomorrow|next week|monday|tuesday|wednesday|thursday|friday)\b", re.I)
_DELIVERY = re.compile(r"\b(next (?:week|month|quarter)|asap|this month)\b", re.I)


_INV_NO = re.compile(r"invoice\s*(?:no\.?|number|#)\s*[:#]?\s*([A-Z0-9][A-Z0-9-]*)", re.I)
_INV_FROM = re.compile(r"^(?:from|supplier|vendor|bill from)\s*:\s*(.+)$", re.I | re.M)
_INV_ORDER = re.compile(r"(?:order|po)\s*(?:no\.?|number|ref(?:erence)?|#)?\s*[:#]?\s*((?:SO|PO)-?\d+)", re.I)
_INV_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_INV_CUR = re.compile(r"\b(USD|SGD|PKR|EUR|GBP)\b")
_AMOUNT = r"([\d,]+\.\d{2})"
_INV_SUB = re.compile(r"sub\s*-?total\s*:\s*" + _AMOUNT, re.I)
_INV_DPCT = re.compile(r"discount\s*\(?\s*(\d+(?:\.\d+)?)\s*%", re.I)
_INV_DAMT = re.compile(r"discount[^\n:]*:\s*-?\s*" + _AMOUNT, re.I)
_INV_TAX = re.compile(r"(?:tax|gst|vat)[^\n:]*:\s*" + _AMOUNT, re.I)
_INV_TOTAL = re.compile(r"(?<!sub)(?<!sub-)(?<!sub )\btotal(?:\s*due)?\s*:\s*" + _AMOUNT, re.I)


def _num(match):
    return float(match.group(1).replace(",", "")) if match else None


def _mock_invoice(text: str) -> dict:
    number, total = _INV_NO.search(text), _INV_TOTAL.search(text)
    supplier = _INV_FROM.search(text)
    return {
        "invoice_number": number.group(1) if number else None,
        "counterparty_name": supplier.group(1).strip() if supplier else None,
        "order_reference": (m.group(1).upper() if (m := _INV_ORDER.search(text)) else None),
        "invoice_date": (m.group(1) if (m := _INV_DATE.search(text)) else None),
        "currency": (m.group(1) if (m := _INV_CUR.search(text)) else None),
        "subtotal": _num(_INV_SUB.search(text)),
        "discount_pct": _num(_INV_DPCT.search(text)),
        "discount_amount": _num(_INV_DAMT.search(text)),
        "tax_amount": _num(_INV_TAX.search(text)),
        "total": _num(total),
        "confidence": 0.9 if (number and total) else 0.5,
    }


class MockLLMClient:
    provider = "mock"
    model = "mock-extractor-v1"

    def complete_json(self, messages: list[dict[str, str]]) -> LLMResponse:
        message = messages[-1]["content"]
        if messages[0]["content"].startswith("INVOICE_EXTRACTION"):
            user = next(m["content"] for m in messages if m["role"] == "user")
            inv_text = (re.search(r"<<<\n(.*)\n>>>", user, re.S) or [None, user])[1]
            if "[[mock:timeout]]" in inv_text:
                raise LLMUnavailable("mock timeout", kind="timeout")
            return LLMResponse(text=json.dumps(_mock_invoice(inv_text)), model=self.model, latency_ms=5)
        # Only look at the customer text inside the delimiters of the first user turn.
        first_user = next(m["content"] for m in messages if m["role"] == "user")
        match = re.search(r"<<<\n(.*)\n>>>", first_user, re.S)
        text = match.group(1) if match else message

        if "[[mock:timeout]]" in text:
            raise LLMUnavailable("mock timeout", kind="timeout")
        if "[[mock:invalid_json]]" in text:
            return LLMResponse(text="Sure! Here is the data: product X, 200 units", model=self.model, latency_ms=5)

        qty = _QTY.search(text)
        disc = _DISCOUNT.search(text)
        prod = _PRODUCT.search(text)
        talk = _APPOINTMENT.search(text)
        # Timing words that follow "talk/call/meet" describe the appointment, not the delivery.
        when = (_WHEN.search(text, talk.start()) or _WHEN.search(text)) if talk else None
        delivery = _DELIVERY.search(text)

        email, name = _EMAIL.search(text), _NAME.search(text)
        data = {
            "contact_name": name.group(1).strip() if name else None,
            "contact_email": email.group(0).lower() if email else None,
            "product_name": f"Product {prod.group(1).upper()}" if prod else None,
            "quantity": int(qty.group(1).replace(",", "")) if qty else None,
            "requested_discount_pct": float(disc.group(1)) if disc else None,
            "delivery_timeframe": delivery.group(1).lower() if delivery else None,
            "appointment_requested": bool(talk),
            "appointment_preference": when.group(1).lower() if when else None,
            "intent": "quote_request" if (prod or qty) else "other",
            "confidence": 0.9 if (prod and qty) else 0.5,
            "missing_fields": [f for f, v in (("product_name", prod), ("quantity", qty)) if not v],
        }
        if "[[mock:low_confidence]]" in text:
            data["confidence"] = 0.4
        if "[[mock:hallucinate]]" in text:
            data["requested_discount_pct"] = 50
        return LLMResponse(text=json.dumps(data), model=self.model, latency_ms=5)
