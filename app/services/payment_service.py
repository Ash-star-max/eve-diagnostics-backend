"""Mock payment processing and idempotent webhook handling.

Lock ordering: whenever both rows are locked, the booking is locked first and
then the payment. Every code path follows this order, which avoids deadlocks
between a payment request and a webhook for the same booking.
"""
import logging
import random
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import Conflict, NotFound, Unprocessable
from app.models import Booking, BookingStatus, Payment, PaymentStatus, User, WebhookEvent
from app.schemas import PaymentCreate, WebhookPayload
from app.utils import as_utc, utcnow

logger = logging.getLogger(__name__)

PROCESSED = "processed"   # state changed
NOOP = "noop"             # already in the requested state
IGNORED = "ignored"       # transition not allowed (e.g. FAILED after SUCCESS)
DUPLICATE = "duplicate"   # this exact event_id was seen before

SIMULATED_DECLINE_REASON = "Simulated decline by mock provider"


# ------------------------------------------------------------------ state machine
def apply_payment_outcome(
    db: Session,
    payment: Payment,
    booking: Booking,
    new_status: PaymentStatus,
    failure_reason: str | None = None,
) -> str:
    """Move a payment (and its booking) to `new_status`, if that's a legal move.

    Payment:  PENDING -> SUCCESS | FAILED,  FAILED -> SUCCESS (late capture).
              SUCCESS is terminal.
    Booking:  follows the payment, except a CANCELLED booking is never revived.

    Shared by the synchronous payment endpoint and the webhook, so both paths
    obey the same rules.
    """
    log_ctx = {"payment_id": payment.id, "booking_id": booking.id,
               "from_status": payment.status.value, "to_status": new_status.value}

    if payment.status == new_status:
        return NOOP
    if payment.status == PaymentStatus.SUCCESS or new_status == PaymentStatus.PENDING:
        logger.warning("payment_transition_ignored", extra=log_ctx)
        return IGNORED

    if new_status == PaymentStatus.SUCCESS:
        other_success = db.scalar(
            select(Payment.id).where(
                Payment.booking_id == booking.id,
                Payment.status == PaymentStatus.SUCCESS,
                Payment.id != payment.id,
            )
        )
        if other_success is not None:
            # Money captured twice for one booking: must be refunded, never
            # recorded as a second successful payment.
            logger.error("duplicate_capture_refund_required", extra=log_ctx)
            return IGNORED

        payment.status = PaymentStatus.SUCCESS
        payment.failure_reason = None
        if booking.status == BookingStatus.CANCELLED:
            logger.warning("payment_succeeded_for_cancelled_booking_refund_required", extra=log_ctx)
        else:
            booking.status = BookingStatus.CONFIRMED
    else:
        payment.status = PaymentStatus.FAILED
        payment.failure_reason = failure_reason or "Payment failed"
        if booking.status == BookingStatus.PENDING:
            booking.status = BookingStatus.FAILED

    logger.info("payment_status_changed", extra=log_ctx)
    return PROCESSED


# ------------------------------------------------------------------ POST /payments/
def _simulate_provider(requested: str | None) -> PaymentStatus:
    if requested is not None:
        return PaymentStatus(requested)
    rate = get_settings().payment_success_rate
    return PaymentStatus.SUCCESS if random.random() < rate else PaymentStatus.FAILED


def _lock_booking(db: Session, booking_id: int) -> Booking | None:
    return db.scalar(
        select(Booking)
        .where(Booking.id == booking_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _find_by_idempotency_key(db: Session, user_id: int, key: str) -> Payment | None:
    return db.scalar(select(Payment).where(Payment.user_id == user_id, Payment.idempotency_key == key))


def _replay(existing: Payment, booking_id: int) -> Payment:
    if existing.booking_id != booking_id:
        raise Conflict(
            "This Idempotency-Key was already used for a different booking",
            code="idempotency_key_reused",
        )
    logger.info("payment_idempotent_replay", extra={"payment_id": existing.id})
    return existing


def create_payment(
    db: Session, user: User, data: PaymentCreate, idempotency_key: str | None
) -> tuple[Payment, Booking, bool]:
    """Returns (payment, booking, created). created=False means a replay."""
    # Locking the booking serialises concurrent payment attempts for it, so two
    # parallel requests can't both charge the same booking.
    booking = _lock_booking(db, data.booking_id)
    if booking is None or booking.user_id != user.id:
        raise NotFound("Booking not found", code="booking_not_found")

    # Checked after taking the lock so a concurrent request with the same key
    # sees the first one's committed payment and replays it.
    if idempotency_key:
        existing = _find_by_idempotency_key(db, user.id, idempotency_key)
        if existing is not None:
            return _replay(existing, booking.id), booking, False

    if booking.status == BookingStatus.CONFIRMED:
        raise Conflict("Booking is already paid", code="booking_already_paid")
    if booking.status == BookingStatus.CANCELLED:
        raise Conflict("Cannot pay for a cancelled booking", code="booking_cancelled")
    if as_utc(booking.appointment_at) <= utcnow():
        raise Unprocessable("Cannot pay for an appointment in the past", code="appointment_in_past")

    in_flight = db.scalar(
        select(Payment.id).where(Payment.booking_id == booking.id, Payment.status == PaymentStatus.PENDING)
    )
    if in_flight is not None:
        raise Conflict(
            "A payment for this booking is already awaiting confirmation",
            code="payment_in_progress",
        )

    payment = Payment(
        booking_id=booking.id,
        user_id=user.id,
        amount=booking.amount,  # always the booking's amount, never client input
        status=PaymentStatus.PENDING,
        provider_reference=f"pay_{uuid.uuid4().hex}",
        idempotency_key=idempotency_key,
    )
    db.add(payment)
    db.flush()

    outcome = _simulate_provider(data.simulate_outcome)
    if outcome != PaymentStatus.PENDING:
        reason = SIMULATED_DECLINE_REASON if outcome == PaymentStatus.FAILED else None
        apply_payment_outcome(db, payment, booking, outcome, reason)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if idempotency_key and (existing := _find_by_idempotency_key(db, user.id, idempotency_key)):
            return _replay(existing, data.booking_id), booking, False
        raise
    logger.info(
        "payment_created",
        extra={"payment_id": payment.id, "booking_id": booking.id, "status": payment.status.value},
    )
    return payment, booking, True


def get_payment(db: Session, user: User, payment_id: int) -> Payment:
    payment = db.get(Payment, payment_id)
    if payment is None or payment.user_id != user.id:
        raise NotFound("Payment not found", code="payment_not_found")
    return payment


# ------------------------------------------------------------------ POST /payments/webhook/
def process_webhook(db: Session, payload: WebhookPayload) -> str:
    """Apply a provider event exactly once.

    The event row and the state change are committed in ONE transaction. The
    UNIQUE(event_id) constraint is the idempotency guard: a redelivery (even a
    concurrent one) fails the insert and is acknowledged as a duplicate. If
    processing fails, everything rolls back and the provider's retry is
    processed normally.
    """
    event = WebhookEvent(
        event_id=payload.event_id,
        provider_reference=payload.provider_reference,
        status=payload.status,
        payload=payload.model_dump(mode="json"),
    )
    db.add(event)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return _acknowledge_duplicate(db, payload)

    payment_id, booking_id = db.execute(
        select(Payment.id, Payment.booking_id).where(Payment.provider_reference == payload.provider_reference)
    ).first() or (None, None)
    if payment_id is None:
        db.rollback()  # don't record the event, so a later retry can succeed
        raise NotFound("Unknown payment reference", code="payment_not_found")

    booking = _lock_booking(db, booking_id)
    payment = db.scalar(
        select(Payment).where(Payment.id == payment_id).with_for_update().execution_options(populate_existing=True)
    )

    if payload.amount is not None and payload.amount != payment.amount:
        db.rollback()
        logger.error(
            "webhook_amount_mismatch",
            extra={"event_id": payload.event_id, "payment_id": payment.id,
                   "expected": str(payment.amount), "received": str(payload.amount)},
        )
        raise Unprocessable("Amount does not match the payment", code="amount_mismatch")

    result = apply_payment_outcome(db, payment, booking, PaymentStatus(payload.status), payload.failure_reason)
    event.result = result
    event.processed_at = utcnow()
    db.commit()
    logger.info("webhook_processed", extra={"event_id": payload.event_id, "result": result})
    return result


def _acknowledge_duplicate(db: Session, payload: WebhookPayload) -> str:
    existing = db.scalar(select(WebhookEvent).where(WebhookEvent.event_id == payload.event_id))
    if existing is not None and existing.payload != payload.model_dump(mode="json"):
        logger.warning("webhook_event_id_reused_with_different_payload", extra={"event_id": payload.event_id})
    logger.info("webhook_duplicate_ignored", extra={"event_id": payload.event_id})
    return DUPLICATE
