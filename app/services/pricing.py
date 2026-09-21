"""Money maths in one place, rounded exactly like the database CHECK constraints expect."""
import hashlib
import json
import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

CENTS = Decimal("0.01")


def money(value: Decimal) -> Decimal:
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Amounts:
    quantity: int
    unit_price: Decimal
    discount_pct: Decimal
    subtotal: Decimal
    discount_amount: Decimal
    total: Decimal
    currency: str

    def as_dict(self) -> dict:
        return {"quantity": self.quantity, "unit_price": str(self.unit_price),
                "discount_pct": str(self.discount_pct), "subtotal": str(self.subtotal),
                "discount_amount": str(self.discount_amount), "total": str(self.total),
                "currency": self.currency}


def compute_amounts(quantity: int, unit_price: Decimal, discount_pct: Decimal, currency: str) -> Amounts:
    subtotal = money(Decimal(quantity) * unit_price)
    discount_amount = money(subtotal * discount_pct / Decimal(100))
    return Amounts(quantity=quantity, unit_price=money(unit_price), discount_pct=money(discount_pct),
                   subtotal=subtotal, discount_amount=discount_amount,
                   total=subtotal - discount_amount, currency=currency)


def terms_hash(*, inquiry_id: uuid.UUID, product_id: uuid.UUID, quantity: int,
               unit_price: Decimal, discount_pct: Decimal, currency: str) -> str:
    """Fingerprint of the exact commercial terms. Quote/order/invoice must carry the same value."""
    canonical = json.dumps({
        "inquiry_id": str(inquiry_id), "product_id": str(product_id), "quantity": quantity,
        "unit_price": str(money(unit_price)), "discount_pct": str(money(discount_pct)),
        "currency": currency,
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
