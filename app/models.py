"""Database models.

Design notes
------------
* Money is NUMERIC(10, 2), never float.
* A booking snapshots `amount` at creation, so later price changes never
  alter an existing booking or its payment.
* `bookings(centre_id, test_id)` has a composite FK to `centre_tests`, so the
  database itself guarantees a booking is only for a test that centre offers.
* Partial unique indexes enforce two business rules at the DB level:
    - at most one SUCCESS payment per booking;
    - no duplicate active booking for the same user / centre / test / slot.
* `webhook_events.event_id` is UNIQUE: this is what makes the webhook
  idempotent, even under concurrent deliveries.
"""
from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.utils import utcnow


class UserRole(str, enum.Enum):
    PATIENT = "PATIENT"
    ADMIN = "ADMIN"


class BookingStatus(str, enum.Enum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PaymentStatus(str, enum.Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


def _enum(enum_cls: type[enum.Enum], name: str) -> Enum:
    # VARCHAR + CHECK constraint: portable, and adding a value later is a
    # simple migration (unlike native PostgreSQL ENUM types).
    return Enum(enum_cls, name=name, native_enum=False, length=20, create_constraint=True)


Money = Numeric(10, 2)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(_enum(UserRole, "user_role"), default=UserRole.PATIENT, nullable=False)

    bookings: Mapped[list[Booking]] = relationship(back_populates="user")


class DiagnosticCentre(TimestampMixin, Base):
    __tablename__ = "diagnostic_centres"
    __table_args__ = (UniqueConstraint("name", "city", name="uq_diagnostic_centres_name_city"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    address: Mapped[str] = mapped_column(String(500), nullable=False)
    city: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    pincode: Mapped[str] = mapped_column(String(10), nullable=False)

    offerings: Mapped[list[CentreTest]] = relationship(back_populates="centre", cascade="all, delete-orphan")


class DiagnosticTest(TimestampMixin, Base):
    """A test in the global catalogue (e.g. CBC). Prices live on CentreTest."""

    __tablename__ = "diagnostic_tests"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)


class CentreTest(TimestampMixin, Base):
    """A test offered by a centre, at that centre's price."""

    __tablename__ = "centre_tests"
    __table_args__ = (
        UniqueConstraint("centre_id", "test_id", name="uq_centre_tests_centre_test"),
        CheckConstraint("price > 0", name="price_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    centre_id: Mapped[int] = mapped_column(ForeignKey("diagnostic_centres.id", ondelete="CASCADE"), nullable=False)
    test_id: Mapped[int] = mapped_column(ForeignKey("diagnostic_tests.id", ondelete="RESTRICT"), nullable=False, index=True)
    price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    # Offerings are deactivated, never deleted, because bookings reference them.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    centre: Mapped[DiagnosticCentre] = relationship(back_populates="offerings")
    test: Mapped[DiagnosticTest] = relationship()


_ACTIVE_BOOKING = text("status IN ('PENDING', 'CONFIRMED', 'FAILED')")


class Booking(TimestampMixin, Base):
    __tablename__ = "bookings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["centre_id", "test_id"],
            ["centre_tests.centre_id", "centre_tests.test_id"],
            name="fk_bookings_offering",
        ),
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_bookings_user_status", "user_id", "status"),
        # Only CANCELLED frees the slot. A FAILED booking is retried by paying
        # again, not by creating a second booking.
        Index(
            "uq_bookings_active_slot",
            "user_id", "centre_id", "test_id", "appointment_at",
            unique=True,
            postgresql_where=_ACTIVE_BOOKING,
            sqlite_where=_ACTIVE_BOOKING,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    centre_id: Mapped[int] = mapped_column(ForeignKey("diagnostic_centres.id", ondelete="RESTRICT"), nullable=False)
    test_id: Mapped[int] = mapped_column(ForeignKey("diagnostic_tests.id", ondelete="RESTRICT"), nullable=False)
    appointment_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    status: Mapped[BookingStatus] = mapped_column(
        _enum(BookingStatus, "booking_status"), default=BookingStatus.PENDING, nullable=False
    )

    user: Mapped[User] = relationship(back_populates="bookings")
    centre: Mapped[DiagnosticCentre] = relationship()
    test: Mapped[DiagnosticTest] = relationship()
    payments: Mapped[list[Payment]] = relationship(back_populates="booking", order_by="Payment.id")


_SUCCESSFUL_PAYMENT = text("status = 'SUCCESS'")


class Payment(TimestampMixin, Base):
    __tablename__ = "payments"
    __table_args__ = (
        # NULL keys are distinct, so requests without a key are unaffected.
        UniqueConstraint("user_id", "idempotency_key", name="uq_payments_user_idempotency_key"),
        CheckConstraint("amount > 0", name="amount_positive"),
        Index(
            "uq_payments_one_success_per_booking",
            "booking_id",
            unique=True,
            postgresql_where=_SUCCESSFUL_PAYMENT,
            sqlite_where=_SUCCESSFUL_PAYMENT,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id", ondelete="RESTRICT"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(
        _enum(PaymentStatus, "payment_status"), default=PaymentStatus.PENDING, nullable=False
    )
    # The id the (mock) provider knows this payment by; webhooks reference it.
    provider_reference: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(100))
    failure_reason: Mapped[str | None] = mapped_column(String(255))

    booking: Mapped[Booking] = relationship(back_populates="payments")


class WebhookEvent(Base):
    """Log of every distinct webhook event processed (the idempotency ledger)."""

    __tablename__ = "webhook_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    provider_reference: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    result: Mapped[str | None] = mapped_column(String(20))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
