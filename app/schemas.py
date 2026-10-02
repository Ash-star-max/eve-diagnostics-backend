"""Request/response models (the API contract)."""
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.models import BookingStatus, PaymentStatus, UserRole
from app.utils import as_utc

T = TypeVar("T")

UTCDateTime = Annotated[datetime, AfterValidator(as_utc)]
Money = Annotated[Decimal, Field(gt=0, max_digits=10, decimal_places=2)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class StrictModel(BaseModel):
    """Inputs reject unknown fields, so e.g. a client can't sneak in `amount`."""

    model_config = ConfigDict(extra="forbid")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------- auth
class SignupRequest(StrictModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
    full_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]

    @field_validator("password")
    @classmethod
    def password_strength(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 72:  # bcrypt's hard limit
            raise ValueError("password must be at most 72 bytes")
        if not any(c.isalpha() for c in value) or not any(c.isdigit() for c in value):
            raise ValueError("password must contain at least one letter and one digit")
        return value


class LoginRequest(StrictModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class UserOut(ORMModel):
    id: int
    email: str
    full_name: str
    role: UserRole
    created_at: UTCDateTime


# ---------------------------------------------------------------- catalogue
class CentreCreate(StrictModel):
    name: Name
    address: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    city: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    pincode: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\d{6}$")]


class CentreOut(ORMModel):
    id: int
    name: str
    address: str
    city: str
    pincode: str


class DiagnosticTestCreate(StrictModel):
    code: Annotated[str, StringConstraints(strip_whitespace=True, to_upper=True, pattern=r"^[A-Z0-9_-]{2,30}$")]
    name: Name
    description: str | None = Field(default=None, max_length=2000)


class DiagnosticTestOut(ORMModel):
    id: int
    code: str
    name: str
    description: str | None


class OfferingCreate(StrictModel):
    test_id: int = Field(gt=0)
    price: Money


class OfferingUpdate(StrictModel):
    price: Money | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def at_least_one_field(self):
        if self.price is None and self.is_active is None:
            raise ValueError("provide at least one of: price, is_active")
        return self


class OfferingOut(ORMModel):
    test: DiagnosticTestOut
    price: Decimal
    is_active: bool


class CentreDetail(CentreOut):
    tests: list[OfferingOut]


# ---------------------------------------------------------------- bookings
class BookingCreate(StrictModel):
    centre_id: int = Field(gt=0)
    test_id: int = Field(gt=0)
    appointment_at: AwareDatetime = Field(
        description="ISO-8601 with timezone, e.g. 2026-10-10T09:30:00+05:30"
    )


class BookingOut(ORMModel):
    id: int
    user_id: int
    centre: CentreOut
    test: DiagnosticTestOut
    appointment_at: UTCDateTime
    amount: Decimal
    status: BookingStatus
    created_at: UTCDateTime
    updated_at: UTCDateTime


# ---------------------------------------------------------------- payments
class PaymentCreate(StrictModel):
    booking_id: int = Field(gt=0)
    simulate_outcome: Literal["SUCCESS", "FAILED", "PENDING"] | None = Field(
        default=None,
        description=(
            "Force the mock provider's answer. SUCCESS/FAILED resolve immediately; "
            "PENDING leaves the payment awaiting a webhook. If omitted, the outcome "
            "is random (PAYMENT_SUCCESS_RATE)."
        ),
    )


class PaymentOut(ORMModel):
    id: int
    booking_id: int
    amount: Decimal
    status: PaymentStatus
    provider_reference: str
    failure_reason: str | None
    created_at: UTCDateTime
    updated_at: UTCDateTime


class PaymentResult(BaseModel):
    payment: PaymentOut
    booking_status: BookingStatus


class WebhookPayload(BaseModel):
    # Providers add fields over time; ignoring unknown ones keeps us compatible.
    model_config = ConfigDict(extra="ignore")

    event_id: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    provider_reference: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    status: Literal["SUCCESS", "FAILED"]
    amount: Money | None = None
    failure_reason: str | None = Field(default=None, max_length=255)


class WebhookAck(BaseModel):
    event_id: str
    result: Literal["processed", "duplicate", "noop", "ignored"]
