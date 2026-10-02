import json

from app.models import Payment, WebhookEvent
from tests.conftest import send_webhook


def _pending_payment(client, headers, booking_id) -> dict:
    r = client.post("/payments/", json={"booking_id": booking_id, "simulate_outcome": "PENDING"}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["payment"]


def _booking_status(client, headers, booking_id) -> str:
    return client.get(f"/bookings/{booking_id}/", headers=headers).json()["status"]


def test_webhook_success_confirms_booking(client, alice, booking):
    payment = _pending_payment(client, alice, booking["id"])
    r = send_webhook(client, {"event_id": "evt_1", "provider_reference": payment["provider_reference"],
                              "status": "SUCCESS", "amount": "349.00"})
    assert r.status_code == 200
    assert r.json() == {"event_id": "evt_1", "result": "processed"}
    assert _booking_status(client, alice, booking["id"]) == "CONFIRMED"


def test_same_event_delivered_many_times_is_applied_once(client, alice, booking, db):
    payment = _pending_payment(client, alice, booking["id"])
    event = {"event_id": "evt_dup", "provider_reference": payment["provider_reference"], "status": "SUCCESS"}

    results = [send_webhook(client, event).json()["result"] for _ in range(3)]

    assert results == ["processed", "duplicate", "duplicate"]
    assert db.query(WebhookEvent).count() == 1
    assert db.query(Payment).count() == 1
    assert _booking_status(client, alice, booking["id"]) == "CONFIRMED"


def test_replayed_event_id_cannot_flip_state(client, alice, booking):
    """An attacker/bug re-sending an old event_id with a different status changes nothing."""
    payment = _pending_payment(client, alice, booking["id"])
    ref = payment["provider_reference"]
    send_webhook(client, {"event_id": "evt_a", "provider_reference": ref, "status": "SUCCESS"})
    r = send_webhook(client, {"event_id": "evt_a", "provider_reference": ref, "status": "FAILED"})
    assert r.json()["result"] == "duplicate"
    assert _booking_status(client, alice, booking["id"]) == "CONFIRMED"


def test_out_of_order_failure_after_success_is_ignored(client, alice, booking):
    ref = _pending_payment(client, alice, booking["id"])["provider_reference"]
    send_webhook(client, {"event_id": "evt_ok", "provider_reference": ref, "status": "SUCCESS"})
    r = send_webhook(client, {"event_id": "evt_late_fail", "provider_reference": ref, "status": "FAILED"})
    assert r.json()["result"] == "ignored"
    assert _booking_status(client, alice, booking["id"]) == "CONFIRMED"
    assert client.get(f"/bookings/{booking['id']}/", headers=alice).json()["status"] == "CONFIRMED"


def test_late_success_after_failure_confirms(client, alice, booking):
    ref = _pending_payment(client, alice, booking["id"])["provider_reference"]
    send_webhook(client, {"event_id": "evt_f", "provider_reference": ref, "status": "FAILED", "failure_reason": "timeout"})
    assert _booking_status(client, alice, booking["id"]) == "FAILED"
    send_webhook(client, {"event_id": "evt_s", "provider_reference": ref, "status": "SUCCESS"})
    assert _booking_status(client, alice, booking["id"]) == "CONFIRMED"


def test_same_status_with_new_event_id_is_noop(client, alice, booking):
    ref = client.post("/payments/", json={"booking_id": booking["id"], "simulate_outcome": "SUCCESS"},
                      headers=alice).json()["payment"]["provider_reference"]
    # The synchronous call already settled it; the provider's confirmation is a no-op.
    r = send_webhook(client, {"event_id": "evt_confirm", "provider_reference": ref, "status": "SUCCESS"})
    assert r.json()["result"] == "noop"


def test_success_for_cancelled_booking_does_not_revive_it(client, alice, booking):
    ref = _pending_payment(client, alice, booking["id"])["provider_reference"]
    client.post(f"/bookings/{booking['id']}/cancel/", headers=alice)
    r = send_webhook(client, {"event_id": "evt_c", "provider_reference": ref, "status": "SUCCESS"})
    assert r.json()["result"] == "processed"  # payment recorded as captured (needs refund)
    assert _booking_status(client, alice, booking["id"]) == "CANCELLED"


def test_signature_is_required(client, alice, booking):
    ref = _pending_payment(client, alice, booking["id"])["provider_reference"]
    event = {"event_id": "evt_x", "provider_reference": ref, "status": "SUCCESS"}

    assert send_webhook(client, event, secret="wrong-secret").status_code == 401
    unsigned = client.post("/payments/webhook/", json=event)
    assert unsigned.status_code == 401 and unsigned.json()["error"]["code"] == "invalid_signature"
    assert _booking_status(client, alice, booking["id"]) == "PENDING"


def test_unknown_reference_is_not_recorded_so_retry_can_succeed(client, db):
    r = send_webhook(client, {"event_id": "evt_u", "provider_reference": "pay_nope", "status": "SUCCESS"})
    assert r.status_code == 404
    assert db.query(WebhookEvent).count() == 0


def test_amount_mismatch_rejected(client, alice, booking):
    ref = _pending_payment(client, alice, booking["id"])["provider_reference"]
    r = send_webhook(client, {"event_id": "evt_m", "provider_reference": ref, "status": "SUCCESS", "amount": "1.00"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "amount_mismatch"
    assert _booking_status(client, alice, booking["id"]) == "PENDING"


def test_malformed_payloads(client):
    for body in ({"event_id": "e", "provider_reference": "p", "status": "MAYBE"}, {"status": "SUCCESS"}):
        assert send_webhook(client, body).status_code == 422

    raw = b"{not json"
    from app.security import sign_webhook
    r = client.post("/payments/webhook/", content=raw,
                    headers={"X-Webhook-Signature": sign_webhook(raw, "test-webhook-secret")})
    assert r.status_code == 422


def test_unknown_extra_fields_are_tolerated(client, alice, booking):
    ref = _pending_payment(client, alice, booking["id"])["provider_reference"]
    event = {"event_id": "evt_e", "provider_reference": ref, "status": "SUCCESS", "provider_metadata": {"x": 1}}
    assert send_webhook(client, event).status_code == 200
