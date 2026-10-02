"""Race-condition tests. They need real row locks, so they only run on PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app.errors import AppError
from app.models import Booking, BookingStatus, Payment, PaymentStatus, User, WebhookEvent
from app.schemas import PaymentCreate, WebhookPayload
from app.services import payment_service
from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL.startswith("postgresql"), reason="needs PostgreSQL (set TEST_DATABASE_URL)"
)

WORKERS = 8


def _run_concurrently(fn):
    barrier = Barrier(WORKERS)

    def task(_):
        barrier.wait()  # release all threads at the same moment
        try:
            return fn()
        except AppError as exc:
            return exc.code

    with ThreadPoolExecutor(WORKERS) as pool:
        return list(pool.map(task, range(WORKERS)))


def test_concurrent_duplicate_webhooks_apply_once(client, alice, booking, session_factory, db):
    ref = client.post("/payments/", json={"booking_id": booking["id"], "simulate_outcome": "PENDING"},
                      headers=alice).json()["payment"]["provider_reference"]
    payload = WebhookPayload(event_id="evt_race", provider_reference=ref, status="SUCCESS")

    def deliver():
        with session_factory() as s:
            return payment_service.process_webhook(s, payload)

    results = _run_concurrently(deliver)

    assert sorted(results) == ["duplicate"] * (WORKERS - 1) + ["processed"]
    assert db.query(WebhookEvent).count() == 1
    assert db.get(Booking, booking["id"]).status == BookingStatus.CONFIRMED


def test_concurrent_payments_charge_once(client, alice, booking, session_factory, db):
    def pay():
        with session_factory() as s:
            user = s.get(User, booking["user_id"])
            payment, _, _ = payment_service.create_payment(
                s, user, PaymentCreate(booking_id=booking["id"], simulate_outcome="SUCCESS"), None
            )
            return payment.status.value

    results = _run_concurrently(pay)

    assert results.count("SUCCESS") == 1
    assert results.count("booking_already_paid") == WORKERS - 1
    assert db.query(Payment).filter_by(status=PaymentStatus.SUCCESS).count() == 1


def test_concurrent_retries_with_same_idempotency_key(client, alice, booking, session_factory, db):
    def pay():
        with session_factory() as s:
            user = s.get(User, booking["user_id"])
            payment, _, _ = payment_service.create_payment(
                s, user, PaymentCreate(booking_id=booking["id"], simulate_outcome="SUCCESS"), "same-key"
            )
            return payment.id

    ids = _run_concurrently(pay)

    assert len(set(ids)) == 1  # every retry got the same payment back
    assert db.query(Payment).count() == 1
