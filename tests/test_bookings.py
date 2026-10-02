from datetime import datetime, timedelta, timezone

from tests.conftest import future


def _book(client, headers, catalog, **overrides):
    payload = {"centre_id": catalog["centre_id"], "test_id": catalog["test_id"], "appointment_at": future()}
    payload.update(overrides)
    return client.post("/bookings/", json=payload, headers=headers)


def test_booking_starts_pending_with_price_snapshot(client, booking):
    assert booking["status"] == "PENDING"
    assert booking["amount"] == "349.00"
    assert booking["centre"]["name"] == "CarePoint"
    assert booking["test"]["code"] == "CBC"


def test_price_change_does_not_affect_existing_booking(client, admin_headers, alice, catalog, booking):
    client.patch(f"/centres/{catalog['centre_id']}/tests/{catalog['test_id']}/", json={"price": "999.00"},
                 headers=admin_headers)
    assert client.get(f"/bookings/{booking['id']}/", headers=alice).json()["amount"] == "349.00"


def test_client_cannot_set_amount_or_status(client, alice, catalog):
    assert _book(client, alice, catalog, amount="1.00").status_code == 422
    assert _book(client, alice, catalog, status="CONFIRMED").status_code == 422


def test_appointment_time_rules(client, alice, catalog):
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    r = _book(client, alice, catalog, appointment_at=past)
    assert r.status_code == 422 and r.json()["error"]["code"] == "appointment_in_past"

    r = _book(client, alice, catalog, appointment_at=future(days=200))
    assert r.json()["error"]["code"] == "appointment_too_far"

    naive = (datetime.now() + timedelta(days=2)).replace(microsecond=0).isoformat()  # no timezone
    assert _book(client, alice, catalog, appointment_at=naive).status_code == 422


def test_timezone_is_normalised_to_utc(client, alice, catalog):
    ist = timezone(timedelta(hours=5, minutes=30))
    local = (datetime.now(ist) + timedelta(days=2)).replace(hour=9, minute=30, second=0, microsecond=0)
    r = _book(client, alice, catalog, appointment_at=local.isoformat())
    assert r.status_code == 201
    stored = datetime.fromisoformat(r.json()["appointment_at"])
    assert stored == local and stored.utcoffset() == timedelta(0)


def test_unknown_centre_or_unoffered_test(client, alice, catalog):
    assert _book(client, alice, catalog, centre_id=9999).json()["error"]["code"] == "centre_not_found"
    r = _book(client, alice, catalog, test_id=catalog["unoffered_test_id"])
    assert r.status_code == 404 and r.json()["error"]["code"] == "test_not_offered"


def test_inactive_offering_cannot_be_booked(client, admin_headers, alice, catalog):
    client.patch(f"/centres/{catalog['centre_id']}/tests/{catalog['test_id']}/", json={"is_active": False},
                 headers=admin_headers)
    assert _book(client, alice, catalog).json()["error"]["code"] == "test_unavailable"


def test_duplicate_active_booking_rejected_until_cancelled(client, alice, catalog):
    slot = future(days=5)
    first = _book(client, alice, catalog, appointment_at=slot)
    assert first.status_code == 201
    dup = _book(client, alice, catalog, appointment_at=slot)
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "duplicate_booking"

    client.post(f"/bookings/{first.json()['id']}/cancel/", headers=alice)
    assert _book(client, alice, catalog, appointment_at=slot).status_code == 201


def test_users_only_see_their_own_bookings(client, alice, bob, admin_headers, booking):
    assert client.get(f"/bookings/{booking['id']}/", headers=bob).status_code == 404  # not 403: don't leak existence
    assert client.get("/bookings/", headers=bob).json()["total"] == 0
    assert client.get("/bookings/", headers=alice).json()["total"] == 1
    assert client.get(f"/bookings/{booking['id']}/", headers=admin_headers).status_code == 200


def test_booking_requires_auth(client, catalog):
    assert client.post("/bookings/", json={}).status_code == 401


def test_cancel_rules(client, alice, bob, booking):
    url = f"/bookings/{booking['id']}/cancel/"
    assert client.post(url, headers=bob).status_code == 404

    r = client.post(url, headers=alice)
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"

    again = client.post(url, headers=alice)
    assert again.status_code == 409 and again.json()["error"]["code"] == "booking_already_cancelled"


def test_invalid_booking_ids(client, alice):
    assert client.get("/bookings/99999/", headers=alice).status_code == 404
    assert client.get("/bookings/abc/", headers=alice).status_code == 422


def test_list_filter_by_status(client, alice, booking):
    assert client.get("/bookings/", params={"status": "PENDING"}, headers=alice).json()["total"] == 1
    assert client.get("/bookings/", params={"status": "CONFIRMED"}, headers=alice).json()["total"] == 0
    assert client.get("/bookings/", params={"status": "BOGUS"}, headers=alice).status_code == 422
