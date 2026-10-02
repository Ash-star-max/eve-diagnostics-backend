def test_centres_are_public_and_show_prices(client, catalog):
    r = client.get("/centres/")
    assert r.status_code == 200
    assert r.json()["total"] == 1

    detail = client.get(f"/centres/{catalog['centre_id']}/").json()
    assert detail["name"] == "CarePoint"
    assert [(t["test"]["code"], t["price"]) for t in detail["tests"]] == [("CBC", "349.00")]


def test_patient_cannot_manage_catalogue(client, alice, catalog):
    r = client.post("/centres/", json={"name": "X", "address": "Y", "city": "Z", "pincode": "110001"}, headers=alice)
    assert r.status_code == 403
    r = client.patch(f"/centres/{catalog['centre_id']}/tests/{catalog['test_id']}/", json={"price": "1.00"}, headers=alice)
    assert r.status_code == 403


def test_anonymous_cannot_manage_catalogue(client):
    assert client.post("/tests/", json={"code": "X1", "name": "X"}).status_code == 401


def test_offering_validation(client, admin_headers, catalog):
    url = f"/centres/{catalog['centre_id']}/tests/"
    assert client.post(url, json={"test_id": catalog["test_id"], "price": "10.00"}, headers=admin_headers).status_code == 409
    assert client.post(url, json={"test_id": catalog["unoffered_test_id"], "price": "0"}, headers=admin_headers).status_code == 422
    assert client.post(url, json={"test_id": catalog["unoffered_test_id"], "price": "1.999"}, headers=admin_headers).status_code == 422
    assert client.post(url, json={"test_id": 9999, "price": "10.00"}, headers=admin_headers).status_code == 404
    assert client.post("/centres/9999/tests/", json={"test_id": catalog["test_id"], "price": "10"}, headers=admin_headers).status_code == 404


def test_duplicate_centre_and_invalid_pincode(client, admin_headers, catalog):
    dup = {"name": "CarePoint", "address": "Elsewhere", "city": "Noida", "pincode": "201301"}
    assert client.post("/centres/", json=dup, headers=admin_headers).status_code == 409
    bad = {"name": "New", "address": "A", "city": "Noida", "pincode": "12AB"}
    assert client.post("/centres/", json=bad, headers=admin_headers).status_code == 422


def test_filter_and_paginate(client, admin_headers, catalog):
    for i in range(3):
        client.post("/centres/", json={"name": f"Delhi Lab {i}", "address": "A", "city": "Delhi", "pincode": "110001"},
                    headers=admin_headers)

    assert client.get("/centres/", params={"city": "delhi"}).json()["total"] == 3
    by_test = client.get("/centres/", params={"test_id": catalog["test_id"]}).json()
    assert [c["id"] for c in by_test["items"]] == [catalog["centre_id"]]

    page = client.get("/centres/", params={"limit": 2, "offset": 2}).json()
    assert page["total"] == 4 and len(page["items"]) == 2
    assert client.get("/centres/", params={"limit": 500}).status_code == 422


def test_deactivated_offering_hidden_from_listing(client, admin_headers, catalog):
    r = client.patch(f"/centres/{catalog['centre_id']}/tests/{catalog['test_id']}/", json={"is_active": False},
                     headers=admin_headers)
    assert r.status_code == 200
    assert client.get(f"/centres/{catalog['centre_id']}/").json()["tests"] == []
    assert client.get("/centres/", params={"test_id": catalog["test_id"]}).json()["total"] == 0


def test_unknown_centre_404(client):
    r = client.get("/centres/12345/")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "centre_not_found"
