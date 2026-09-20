"""API request/response contracts. Validation happens here, before anything touches the database."""
import uuid
from datetime import datetime
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
