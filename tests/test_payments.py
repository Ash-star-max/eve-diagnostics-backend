from app.models import Payment


def _pay(client, headers, booking_id, outcome="SUCCESS", key=None):
    extra = {"Idempotency-Key": key} if key else {}
    body = {"booking_id": booking_id}
    if outcome is not None:
        body["simulate_outcome"] = outcome
    return client.post("/payments/", json=body, headers={**headers, **extra})


def test_successful_payment_confirms_booking(client, alice, booking):
    r = _pay(client, alice, booking["id"])
    assert r.status_code == 201
    body = r.json()
    assert body["payment"]["status"] == "SUCCESS"
    assert body["payment"]["amount"] == "349.00"
    assert body["payment"]["provider_reference"].startswith("pay_")
    assert body["booking_status"] == "CONFIRMED"
    assert client.get(f"/bookings/{booking['id']}/", headers=alice).json()["status"] == "CONFIRMED"


def test_failed_payment_marks_booking_failed_and_can_be_retried(client, alice, booking):
    failed = _pay(client, alice, booking["id"], "FAILED").json()
    assert failed["payment"]["status"] == "FAILED"
    assert failed["payment"]["failure_reason"]
    assert failed["booking_status"] == "FAILED"

    retry = _pay(client, alice, booking["id"], "SUCCESS").json()
    assert retry["booking_status"] == "CONFIRMED"
    assert retry["payment"]["id"] != failed["payment"]["id"]


def test_random_outcome_when_not_forced(client, alice, booking):
    r = _pay(client, alice, booking["id"], outcome=None)
    assert r.status_code == 201
    assert r.json()["payment"]["status"] in {"SUCCESS", "FAILED"}


def test_cannot_pay_twice(client, alice, booking):
    _pay(client, alice, booking["id"])
    r = _pay(client, alice, booking["id"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "booking_already_paid"


def test_cannot_pay_cancelled_booking(client, alice, booking):
    client.post(f"/bookings/{booking['id']}/cancel/", headers=alice)
    r = _pay(client, alice, booking["id"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "booking_cancelled"


def test_cannot_pay_someone_elses_or_missing_booking(client, bob, booking):
    assert _pay(client, bob, booking["id"]).status_code == 404
    assert _pay(client, bob, 99999).json()["error"]["code"] == "booking_not_found"


def test_payment_validation(client, alice, booking):
    assert client.post("/payments/", json={"booking_id": booking["id"], "amount": "1.00"}, headers=alice).status_code == 422
    assert client.post("/payments/", json={"booking_id": 0}, headers=alice).status_code == 422
    assert client.post("/payments/", json={"booking_id": booking["id"], "simulate_outcome": "MAYBE"},
                       headers=alice).status_code == 422
    assert client.post("/payments/", json={"booking_id": booking["id"]}).status_code == 401


def test_pending_payment_blocks_a_second_attempt(client, alice, booking):
    r = _pay(client, alice, booking["id"], "PENDING")
    assert r.json()["payment"]["status"] == "PENDING" and r.json()["booking_status"] == "PENDING"
    again = _pay(client, alice, booking["id"], "SUCCESS")
    assert again.status_code == 409 and again.json()["error"]["code"] == "payment_in_progress"


def test_idempotency_key_replays_instead_of_charging_again(client, alice, booking, db):
    first = _pay(client, alice, booking["id"], "SUCCESS", key="key-123")
    second = _pay(client, alice, booking["id"], "SUCCESS", key="key-123")
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.headers["Idempotent-Replayed"] == "true"
    assert first.json()["payment"]["id"] == second.json()["payment"]["id"]
    assert db.query(Payment).count() == 1


def test_idempotency_key_reused_for_other_booking(client, alice, catalog, booking):
    from tests.conftest import future

    other = client.post("/bookings/", headers=alice, json={
        "centre_id": catalog["centre_id"], "test_id": catalog["test_id"], "appointment_at": future(days=6)}).json()
    _pay(client, alice, booking["id"], key="key-xyz")
    r = _pay(client, alice, other["id"], key="key-xyz")
    assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_key_reused"


def test_get_payment_is_owner_only(client, alice, bob, booking):
    payment_id = _pay(client, alice, booking["id"]).json()["payment"]["id"]
    assert client.get(f"/payments/{payment_id}/", headers=alice).status_code == 200
    assert client.get(f"/payments/{payment_id}/", headers=bob).status_code == 404
