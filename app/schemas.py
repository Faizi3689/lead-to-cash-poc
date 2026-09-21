"""API request/response contracts. Validation happens here, before anything touches the database."""
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class InquiryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    message: str = Field(min_length=1, max_length=5000, description="Raw customer message")
    customer_name: str | None = Field(default=None, max_length=200)
    customer_email: EmailStr | None = None
    source: Literal["webhook", "email", "form", "api"] = "webhook"

    @field_validator("customer_email")
    @classmethod
    def _lower_email(cls, v: str | None) -> str | None:
        return v.lower() if v else v


class InquiryOut(BaseModel):
    inquiry_id: uuid.UUID
    status: str
    duplicate: bool = False
    customer_id: uuid.UUID | None = None
    received_at: datetime


class ErrorOut(BaseModel):
    error: str
    detail: str | list | dict | None = None
    request_id: str | None = None


class ProductOut(BaseModel):
    product_id: uuid.UUID
    sku: str
    name: str
    unit_price: str
    currency: str


class ExtractionOut(BaseModel):
    inquiry_id: uuid.UUID
    status: str
    outcome: Literal["extracted", "needs_review"]
    replay: bool = False
    attempts: int
    confidence: float | None = None
    product: ProductOut | None = None
    extracted: dict | None = None
    review_reasons: list[str] = []
    exception_ids: list[uuid.UUID] = []


class AmountsOut(BaseModel):
    quantity: int
    unit_price: str
    discount_pct: str
    subtotal: str
    discount_amount: str
    total: str
    currency: str


class ApprovalOut(BaseModel):
    approval_id: uuid.UUID
    inquiry_id: uuid.UUID
    status: str
    required_role: str
    quantity: int
    unit_price: str
    requested_discount_pct: str
    approved_discount_pct: str | None = None
    terms_hash: str | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    expires_at: datetime | None = None


class DecisionOut(BaseModel):
    inquiry_id: uuid.UUID
    status: str
    outcome: Literal["auto_approve", "salesperson_approval", "manager_approval", "human_review", "reject"]
    rule_version: str
    reasons: list[str] = []
    replay: bool = False
    approval: ApprovalOut | None = None
    approval_token: str | None = Field(default=None,
                                       description="Shown once; needed to decide the approval")
    amounts: AmountsOut | None = None
    exception_ids: list[uuid.UUID] = []


class ApprovalDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    action: Literal["approve", "reject", "modify"]
    token: str = Field(min_length=10, max_length=200)
    decided_by: str = Field(min_length=2, max_length=100, description="Who is deciding (name or email)")
    approved_discount_pct: Decimal | None = Field(default=None, ge=0, le=100, decimal_places=2)
    comment: str | None = Field(default=None, max_length=1000)


class ApprovalDecisionOut(BaseModel):
    approval: ApprovalOut
    inquiry_status: str
    replay: bool = False
    amounts: AmountsOut | None = None


class ExpireOut(BaseModel):
    expired: list[uuid.UUID] = []
