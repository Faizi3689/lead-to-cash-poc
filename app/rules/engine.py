"""Deterministic business rules. No AI, no randomness: same input -> same decision, always.

Discount authority (the core policy from the assignment):
    <= 5%            -> automatic approval (no human)
    > 5%  and <= 15% -> salesperson approval
    > 15%            -> manager approval
    > 40%            -> automatic rejection (outside company policy)

Extra guard rails:
    order total above 50,000 -> manager approval even when the discount is small
    quantity above 10,000    -> human review (unusual order size)
    missing product/quantity -> human review (never guess)
"""
from dataclasses import dataclass, field
from decimal import Decimal

RULE_VERSION = "discount-rules-v1"

AUTO_APPROVE_MAX_PCT = Decimal("5")
SALESPERSON_MAX_PCT = Decimal("15")
POLICY_MAX_PCT = Decimal("40")
MANAGER_REVIEW_TOTAL = Decimal("50000")
MAX_QUANTITY = 10_000

AUTO_APPROVE = "auto_approve"
SALESPERSON_APPROVAL = "salesperson_approval"
MANAGER_APPROVAL = "manager_approval"
HUMAN_REVIEW = "human_review"
REJECT = "reject"

ROLE_FOR_OUTCOME = {AUTO_APPROVE: "system", SALESPERSON_APPROVAL: "salesperson", MANAGER_APPROVAL: "manager"}
# What each role is allowed to grant, used again when a decision comes back in.
ROLE_MAX_PCT = {"system": AUTO_APPROVE_MAX_PCT, "salesperson": SALESPERSON_MAX_PCT, "manager": POLICY_MAX_PCT}


@dataclass
class Decision:
    outcome: str
    reasons: list[str] = field(default_factory=list)

    @property
    def required_role(self) -> str | None:
        return ROLE_FOR_OUTCOME.get(self.outcome)


def decide(*, product_found: bool, quantity: int | None, discount_pct: Decimal,
           order_total: Decimal | None) -> Decision:
    reasons: list[str] = []

    if not product_found or quantity is None:
        missing = [n for n, ok in (("product", product_found), ("quantity", quantity is not None)) if not ok]
        return Decision(HUMAN_REVIEW, [f"cannot price the request: missing {', '.join(missing)}"])

    if quantity > MAX_QUANTITY:
        return Decision(HUMAN_REVIEW, [f"quantity {quantity} exceeds the {MAX_QUANTITY} unit limit per order"])

    if discount_pct > POLICY_MAX_PCT:
        return Decision(REJECT, [f"requested discount {discount_pct}% exceeds the policy ceiling "
                                 f"of {POLICY_MAX_PCT}%"])

    if discount_pct <= AUTO_APPROVE_MAX_PCT:
        outcome = AUTO_APPROVE
        reasons.append(f"discount {discount_pct}% is within the {AUTO_APPROVE_MAX_PCT}% auto-approval limit")
    elif discount_pct <= SALESPERSON_MAX_PCT:
        outcome = SALESPERSON_APPROVAL
        reasons.append(f"discount {discount_pct}% is above {AUTO_APPROVE_MAX_PCT}% and within "
                       f"{SALESPERSON_MAX_PCT}%: salesperson approval required")
    else:
        outcome = MANAGER_APPROVAL
        reasons.append(f"discount {discount_pct}% is above {SALESPERSON_MAX_PCT}%: manager approval required")

    if order_total is not None and order_total > MANAGER_REVIEW_TOTAL and outcome != MANAGER_APPROVAL:
        outcome = MANAGER_APPROVAL
        reasons.append(f"order total {order_total} is above {MANAGER_REVIEW_TOTAL}: escalated to manager")

    return Decision(outcome, reasons)


def may_grant(role: str, discount_pct: Decimal) -> bool:
    """Can this role grant this discount? Used to reject over-reach at decision time."""
    return discount_pct <= ROLE_MAX_PCT.get(role, Decimal(0))
