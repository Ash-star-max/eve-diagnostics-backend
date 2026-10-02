from datetime import timedelta

import jwt

from app.utils import utcnow
from tests.conftest import PASSWORD, auth_header


def test_signup_returns_user_without_password(client):
    r = client.post("/auth/signup/", json={"email": "New@Example.com", "password": PASSWORD, "full_name": "  New User "})
    assert r.status_code == 201
    body = r.json()
    assert body["email"] == "new@example.com"
    assert body["full_name"] == "New User"
    assert body["role"] == "PATIENT"
    assert "password" not in body and "password_hash" not in body


def test_signup_duplicate_email_is_case_insensitive(client):
    client.post("/auth/signup/", json={"email": "a@example.com", "password": PASSWORD, "full_name": "A"})
    r = client.post("/auth/signup/", json={"email": "A@EXAMPLE.com", "password": PASSWORD, "full_name": "A"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "email_taken"


def test_signup_validation(client):
    cases = [
        {"email": "not-an-email", "password": PASSWORD, "full_name": "X"},
        {"email": "x@example.com", "password": "short1", "full_name": "X"},
        {"email": "x@example.com", "password": "nodigitshere", "full_name": "X"},
        {"email": "x@example.com", "password": PASSWORD, "full_name": "   "},
        {"email": "x@example.com", "password": PASSWORD, "full_name": "X", "role": "ADMIN"},  # can't self-promote
    ]
    for payload in cases:
        r = client.post("/auth/signup/", json=payload)
        assert r.status_code == 422, payload
        assert r.json()["error"]["code"] == "validation_error"


def test_login_success_and_me(client, alice):
    r = client.get("/auth/me/", headers=alice)
    assert r.status_code == 200
    assert r.json()["email"] == "alice@example.com"


def test_login_wrong_password_and_unknown_email_look_identical(client, alice):
    wrong = client.post("/auth/login/", json={"email": "alice@example.com", "password": "Wrong1234"})
    unknown = client.post("/auth/login/", json={"email": "ghost@example.com", "password": "Wrong1234"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_protected_route_requires_valid_token(client):
    assert client.get("/auth/me/").status_code == 401
    assert client.get("/auth/me/", headers=auth_header("garbage")).json()["error"]["code"] == "invalid_token"


def test_expired_and_forged_tokens_rejected(client, alice):
    expired = jwt.encode(
        {"sub": "1", "type": "access", "exp": utcnow() - timedelta(minutes=1)}, "test-jwt-secret", algorithm="HS256"
    )
    r = client.get("/auth/me/", headers=auth_header(expired))
    assert r.status_code == 401 and r.json()["error"]["code"] == "token_expired"

    forged = jwt.encode({"sub": "1", "type": "access", "exp": utcnow() + timedelta(hours=1)}, "wrong-secret", algorithm="HS256")
    assert client.get("/auth/me/", headers=auth_header(forged)).status_code == 401


def test_login_is_rate_limited(client, alice):
    for _ in range(5):
        client.post("/auth/login/", json={"email": "alice@example.com", "password": "Wrong1234"})
    r = client.post("/auth/login/", json={"email": "alice@example.com", "password": PASSWORD})
    assert r.status_code == 429
    assert "Retry-After" in r.headers
