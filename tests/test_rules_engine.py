"""Pure unit tests for the discount policy. No database, no AI: the rules are fully deterministic."""
from decimal import Decimal

import pytest

from app.rules import engine

D = Decimal


def d(discount, quantity=100, product_found=True, total=D("1000")):
    return engine.decide(product_found=product_found, quantity=quantity,
                         discount_pct=D(str(discount)), order_total=total)


@pytest.mark.parametrize("pct,expected", [
    (0, engine.AUTO_APPROVE),
    (5, engine.AUTO_APPROVE),            # boundary: 5% is still automatic
    ("5.01", engine.SALESPERSON_APPROVAL),
    (10, engine.SALESPERSON_APPROVAL),
    (15, engine.SALESPERSON_APPROVAL),   # boundary: 15% is still salesperson
    ("15.01", engine.MANAGER_APPROVAL),
    (20, engine.MANAGER_APPROVAL),
    (40, engine.MANAGER_APPROVAL),       # boundary: 40% is the policy ceiling
    ("40.01", engine.REJECT),
    (90, engine.REJECT),
])
def test_discount_thresholds(pct, expected):
    assert d(pct).outcome == expected


def test_decision_is_deterministic():
    assert d(20).outcome == d(20).outcome == engine.MANAGER_APPROVAL


def test_missing_product_or_quantity_goes_to_human_review():
    assert d(5, product_found=False).outcome == engine.HUMAN_REVIEW
    assert d(5, quantity=None).outcome == engine.HUMAN_REVIEW


def test_oversized_quantity_goes_to_human_review():
    assert d(2, quantity=engine.MAX_QUANTITY).outcome == engine.AUTO_APPROVE
    assert d(2, quantity=engine.MAX_QUANTITY + 1).outcome == engine.HUMAN_REVIEW


def test_high_value_order_escalates_to_manager():
    small = d(2, total=engine.MANAGER_REVIEW_TOTAL)
    big = d(2, total=engine.MANAGER_REVIEW_TOTAL + 1)
    assert small.outcome == engine.AUTO_APPROVE
    assert big.outcome == engine.MANAGER_APPROVAL
    assert any("escalated to manager" in r for r in big.reasons)


def test_every_decision_explains_itself():
    for pct in (0, 10, 20, 99):
        assert d(pct).reasons, "a decision must always carry its reason"


def test_role_authority_limits():
    assert engine.may_grant("system", D("5")) and not engine.may_grant("system", D("6"))
    assert engine.may_grant("salesperson", D("15")) and not engine.may_grant("salesperson", D("16"))
    assert engine.may_grant("manager", D("40")) and not engine.may_grant("manager", D("41"))
