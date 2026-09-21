"""API request/response contracts. Validation happens here, before anything touches the database."""
import uuid
from datetime import date, datetime
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


class QuoteOut(BaseModel):
    quote_id: uuid.UUID
    quote_number: str
    inquiry_id: uuid.UUID
    approval_id: uuid.UUID
    status: str
    quantity: int
    unit_price: str
    discount_pct: str
    subtotal: str
    discount_amount: str
    total: str
    currency: str
    terms_hash: str
    valid_until: str | None = None
    replay: bool = False


class OrderOut(BaseModel):
    order_id: uuid.UUID
    order_number: str
    quote_id: uuid.UUID
    inquiry_id: uuid.UUID
    status: str
    quantity: int
    discount_pct: str
    total: str
    currency: str
    terms_hash: str
    replay: bool = False


class InvoiceOut(BaseModel):
    invoice_id: uuid.UUID
    invoice_number: str | None
    order_id: uuid.UUID | None
    direction: str
    status: str
    counterparty_name: str | None
    invoice_date: str | None
    due_date: str | None
    subtotal: str | None
    discount_pct: str | None
    discount_amount: str | None
    tax_amount: str | None
    total: str | None
    currency: str | None
    line_items: list = []
    terms_hash: str | None = None
    replay: bool = False


class AppointmentOut(BaseModel):
    appointment_id: uuid.UUID
    inquiry_id: uuid.UUID
    status: str
    requested_text: str | None
    scheduled_start: datetime
    scheduled_end: datetime
    timezone: str
    external_ref: str | None
    replay: bool = False


class LineItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str | None = Field(default=None, max_length=300)
    sku: str | None = Field(default=None, max_length=50)
    quantity: Decimal | None = Field(default=None, ge=0)
    unit_price: Decimal | None = Field(default=None, ge=0)
    line_total: Decimal | None = None


class InvoiceIn(BaseModel):
    """Every field is optional on purpose: missing data is detected and flagged, not rejected."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    invoice_number: str | None = Field(default=None, max_length=100)
    counterparty_name: str | None = Field(default=None, max_length=200)
    order_reference: str | None = Field(default=None, max_length=100)
    invoice_date: date | None = None
    due_date: date | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    subtotal: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    discount_pct: Decimal | None = Field(default=None, ge=0, le=100, decimal_places=2)
    discount_amount: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    tax_amount: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    total: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    line_items: list[LineItemIn] | None = None

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: str | None) -> str | None:
        return v.upper() if v else v


class InvoiceTextIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    raw_text: str = Field(min_length=10, max_length=20000)


class ExpenseIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expense_ref: str | None = Field(default=None, max_length=100)
    employee_name: str | None = Field(default=None, max_length=200)
    category: str | None = Field(default=None, max_length=100)
    amount: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    expense_date: date | None = None
    description: str | None = Field(default=None, max_length=1000)
    receipt_ref: str | None = Field(default=None, max_length=300)

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: str | None) -> str | None:
        return v.upper() if v else v


class CorrectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    changes: dict = Field(description="Fields to change, e.g. {\"discount_pct\": \"12.00\"}")
    corrected_by: str = Field(min_length=2, max_length=100)
    reason: str = Field(min_length=3, max_length=1000)


class ActorIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    by: str = Field(min_length=2, max_length=100)
    reason: str | None = Field(default=None, max_length=1000)


class ExceptionOut(BaseModel):
    exception_id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    exception_type: str
    severity: str
    status: str
    message: str | None = None
    details: dict = {}
    assigned_role: str | None = None
    overridable: bool = False
    resolved_by: str | None = None
    resolution: dict | None = None
    created_at: datetime


class ValidationOut(BaseModel):
    entity_type: str
    entity_id: uuid.UUID
    status: str
    replay: bool = False
    open_exceptions: list[ExceptionOut] = []
    document: dict = {}
    ai: dict | None = None
