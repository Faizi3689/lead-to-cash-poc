from decimal import Decimal

from app.services.pricing import compute_amounts, terms_hash
import uuid

D = Decimal


def test_amounts_match_the_assignment_example():
    a = compute_amounts(200, D("50.00"), D("12"), "USD")
    assert (str(a.subtotal), str(a.discount_amount), str(a.total)) == ("10000.00", "1200.00", "8800.00")


def test_rounding_is_half_up_like_postgres():
    a = compute_amounts(3, D("10.00"), D("12.5"), "USD")   # 30.00 * 12.5% = 3.75
    assert str(a.discount_amount) == "3.75"
    b = compute_amounts(1, D("10.05"), D("33.33"), "USD")  # 3.349... -> 3.35
    assert str(b.discount_amount) == "3.35"


def test_terms_hash_changes_with_every_term():
    base = dict(inquiry_id=uuid.uuid4(), product_id=uuid.uuid4(), quantity=200,
                unit_price=D("50.00"), discount_pct=D("12"), currency="USD")
    h = terms_hash(**base)
    assert h == terms_hash(**base)                                  # stable
    assert h != terms_hash(**{**base, "discount_pct": D("20")})     # the key protection
    assert h != terms_hash(**{**base, "quantity": 201})
    assert h != terms_hash(**{**base, "unit_price": D("51.00")})
