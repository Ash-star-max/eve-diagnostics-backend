"""Booking business rules."""
import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.errors import Conflict, NotFound, Unprocessable
from app.models import Booking, BookingStatus, CentreTest, DiagnosticCentre, User, UserRole
from app.schemas import BookingCreate
from app.utils import utcnow

logger = logging.getLogger(__name__)

MAX_ADVANCE_BOOKING = timedelta(days=90)


def _with_relations(stmt):
    return stmt.options(selectinload(Booking.centre), selectinload(Booking.test))


def create_booking(db: Session, user: User, data: BookingCreate) -> Booking:
    appointment_at = data.appointment_at.astimezone(utcnow().tzinfo)
    now = utcnow()
    if appointment_at <= now:
        raise Unprocessable("Appointment must be in the future", code="appointment_in_past")
    if appointment_at > now + MAX_ADVANCE_BOOKING:
        raise Unprocessable("Appointments can be booked at most 90 days ahead", code="appointment_too_far")

    if db.get(DiagnosticCentre, data.centre_id) is None:
        raise NotFound("Diagnostic centre not found", code="centre_not_found")

    offering = db.scalar(
        select(CentreTest).where(CentreTest.centre_id == data.centre_id, CentreTest.test_id == data.test_id)
    )
    if offering is None:
        raise NotFound("This centre does not offer the requested test", code="test_not_offered")
    if not offering.is_active:
        raise Unprocessable("This test is currently unavailable at this centre", code="test_unavailable")

    booking = Booking(
        user_id=user.id,
        centre_id=data.centre_id,
        test_id=data.test_id,
        appointment_at=appointment_at,
        amount=offering.price,  # price snapshot; never taken from the client
        status=BookingStatus.PENDING,
    )
    db.add(booking)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise Conflict(
            "You already have an active booking for this test at this time",
            code="duplicate_booking",
        )
    logger.info("booking_created", extra={"booking_id": booking.id, "user_id": user.id})
    return get_booking(db, user, booking.id)


def get_booking(db: Session, user: User, booking_id: int, *, for_update: bool = False) -> Booking:
    """Return the booking if the user may see it.

    Someone else's booking gets the same 404 as a missing one, so ids can't be
    probed to learn which bookings exist.
    """
    stmt = _with_relations(select(Booking).where(Booking.id == booking_id))
    if for_update:
        stmt = stmt.with_for_update(of=Booking)
    booking = db.scalar(stmt)
    if booking is None or (booking.user_id != user.id and user.role != UserRole.ADMIN):
        raise NotFound("Booking not found", code="booking_not_found")
    return booking


def list_bookings_stmt(user: User, status: BookingStatus | None):
    stmt = _with_relations(select(Booking)).order_by(Booking.appointment_at.desc(), Booking.id.desc())
    if user.role != UserRole.ADMIN:
        stmt = stmt.where(Booking.user_id == user.id)
    if status is not None:
        stmt = stmt.where(Booking.status == status)
    return stmt


def cancel_booking(db: Session, user: User, booking_id: int) -> Booking:
    booking = get_booking(db, user, booking_id, for_update=True)
    if booking.status == BookingStatus.CANCELLED:
        raise Conflict("Booking is already cancelled", code="booking_already_cancelled")

    previous = booking.status
    booking.status = BookingStatus.CANCELLED
    db.commit()
    logger.info(
        "booking_cancelled",
        extra={
            "booking_id": booking.id,
            "previous_status": previous.value,
            # A confirmed booking was paid for; a real system would start a refund here.
            "refund_required": previous == BookingStatus.CONFIRMED,
        },
    )
    return booking
