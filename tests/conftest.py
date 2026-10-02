"""Test fixtures.

By default tests run against in-memory SQLite. To run against PostgreSQL
(recommended before submitting, since partial indexes and row locks are
exercised for real), set TEST_DATABASE_URL, e.g.:

    TEST_DATABASE_URL=postgresql+psycopg://eve:eve@localhost:5432/eve_test pytest
"""
import json
import os
from datetime import datetime, timedelta, timezone

# Configure the app before it is imported.
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["JWT_SECRET"] = "test-jwt-secret"
os.environ["WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["BCRYPT_ROUNDS"] = "4"  # fast hashing in tests only

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, create_engine, get_db
from app.main import app
from app.models import User, UserRole
from app.rate_limit import login_limiter
from app.security import hash_password, sign_webhook

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "sqlite://")
WEBHOOK_SECRET = "test-webhook-secret"
PASSWORD = "Passw0rd!"


@pytest.fixture(scope="session")
def engine():
    if TEST_DATABASE_URL.startswith("sqlite"):
        eng = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    else:
        eng = create_engine(TEST_DATABASE_URL)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine):
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def db(session_factory):
    with session_factory() as session:
        yield session


@pytest.fixture
def client(session_factory):
    def override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    login_limiter.reset()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# ------------------------------------------------------------------ helpers
def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def signup_and_login(client: TestClient, email: str, password: str = PASSWORD) -> dict:
    r = client.post("/auth/signup/", json={"email": email, "password": password, "full_name": email.split("@")[0]})
    assert r.status_code == 201, r.text
    r = client.post("/auth/login/", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return auth_header(r.json()["access_token"])


def future(days: int = 3, hour: int = 9) -> str:
    dt = (datetime.now(timezone.utc) + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return dt.isoformat()


def send_webhook(client: TestClient, payload: dict, secret: str = WEBHOOK_SECRET):
    body = json.dumps(payload).encode()
    return client.post(
        "/payments/webhook/",
        content=body,
        headers={"Content-Type": "application/json", "X-Webhook-Signature": sign_webhook(body, secret)},
    )


@pytest.fixture
def admin_headers(client, db) -> dict:
    db.add(User(email="admin@example.com", full_name="Admin", password_hash=hash_password(PASSWORD), role=UserRole.ADMIN))
    db.commit()
    r = client.post("/auth/login/", json={"email": "admin@example.com", "password": PASSWORD})
    return auth_header(r.json()["access_token"])


@pytest.fixture
def alice(client) -> dict:
    return signup_and_login(client, "alice@example.com")


@pytest.fixture
def bob(client) -> dict:
    return signup_and_login(client, "bob@example.com")


@pytest.fixture
def catalog(client, admin_headers) -> dict:
    """One centre offering CBC at 349.00, and a second test it doesn't offer."""
    centre = client.post(
        "/centres/",
        json={"name": "CarePoint", "address": "Sector 18", "city": "Noida", "pincode": "201301"},
        headers=admin_headers,
    ).json()
    cbc = client.post("/tests/", json={"code": "CBC", "name": "Complete Blood Count"}, headers=admin_headers).json()
    tsh = client.post("/tests/", json={"code": "TSH", "name": "Thyroid"}, headers=admin_headers).json()
    r = client.post(f"/centres/{centre['id']}/tests/", json={"test_id": cbc["id"], "price": "349.00"}, headers=admin_headers)
    assert r.status_code == 201, r.text
    return {"centre_id": centre["id"], "test_id": cbc["id"], "unoffered_test_id": tsh["id"]}


@pytest.fixture
def booking(client, alice, catalog) -> dict:
    r = client.post(
        "/bookings/",
        json={"centre_id": catalog["centre_id"], "test_id": catalog["test_id"], "appointment_at": future()},
        headers=alice,
    )
    assert r.status_code == 201, r.text
    return r.json()
