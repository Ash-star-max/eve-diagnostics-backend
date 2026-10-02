# EVE Diagnostics — Booking & Payments API

A backend service for booking diagnostic tests at diagnostic centres and paying for them through a **simulated** payment provider, including an **idempotent, signed payment webhook**.

Built with **FastAPI · SQLAlchemy 2 · PostgreSQL · Pydantic v2 · JWT**, with Docker, OpenAPI docs, structured logging, pagination, rate limiting and a test suite that includes real PostgreSQL concurrency tests.

---

## Contents
1. [Quick start](#quick-start)
2. [Running tests](#running-tests)
3. [API overview](#api-overview)
4. [Walkthrough with example requests](#walkthrough-with-example-requests)
5. [Database design](#database-design)
6. [Payments, webhook & idempotency](#payments-webhook--idempotency)
7. [Edge cases handled](#edge-cases-handled)
8. [Assumptions](#assumptions)
9. [Project structure](#project-structure)
10. [What I would improve with more time](#what-i-would-improve-with-more-time)

---

## Quick start

### Option A — Docker (recommended, uses PostgreSQL)

```bash
docker compose up --build
```

- API: http://localhost:8000
- Interactive docs (Swagger): http://localhost:8000/docs · ReDoc: http://localhost:8000/redoc
- Sample data is seeded automatically (6 tests, 3 centres in Noida / New Delhi / Gurugram).
- An admin is created: `admin@evehealth.dev` / `Admin12345` (override with `ADMIN_EMAIL`, `ADMIN_PASSWORD`).

### Option B — Local Python

Requires Python 3.11+.

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # edit DATABASE_URL to point at your PostgreSQL
python -m app.seed              # creates tables + sample data (safe to re-run)
uvicorn app.main:app --reload
```

No PostgreSQL handy? Remove `DATABASE_URL` from `.env` and the app falls back to a local SQLite file, so it runs with zero setup.

### Configuration

All settings are environment variables (see `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./eve.db` | SQLAlchemy URL, e.g. `postgresql+psycopg://eve:eve@localhost:5432/eve` |
| `JWT_SECRET` | dev value | HMAC key for access tokens — **must** be changed outside development |
| `JWT_EXPIRE_MINUTES` | `60` | Access-token lifetime |
| `WEBHOOK_SECRET` | `whsec_dev_secret` | Shared secret for webhook signatures |
| `PAYMENT_SUCCESS_RATE` | `0.8` | Approval probability when the client doesn't force an outcome |
| `LOGIN_RATE_LIMIT` / `LOGIN_RATE_WINDOW_SECONDS` | `5` / `60` | Login attempts allowed per IP per window |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | unset | Bootstrap admin created by `python -m app.seed` |

---

## Running tests

```bash
pytest                                   # fast, in-memory SQLite (52 tests, ~5s)
```

The concurrency tests need real row locks and partial indexes, so they run only against PostgreSQL:

```bash
# with docker compose running (it creates an eve_test database):
docker compose exec api pytest

# or against any local PostgreSQL:
TEST_DATABASE_URL=postgresql+psycopg://eve:eve@localhost:5432/eve_test pytest   # 55 tests
```

CI (`.github/workflows/tests.yml`) runs the suite on both SQLite and PostgreSQL on every push.

**What's covered:** signup/login validation, token expiry and forgery, rate limiting, admin-only catalogue management, booking rules (past/naive datetimes, timezone normalisation, unoffered/inactive tests, duplicate slots, price snapshotting), authorisation (users can't see or change others' bookings/payments), the payment state machine (success, failure and retry, double payment, cancelled bookings, in-flight payments), idempotency keys, and the webhook (signature checks, duplicates, out-of-order events, late success, unknown references, amount mismatch, malformed bodies). On PostgreSQL, three tests fire 8 simultaneous requests to prove that duplicate webhooks apply once, parallel payments charge once, and parallel retries with one idempotency key create a single payment.

---

## API overview

Full, interactive documentation is at `/docs`. Click **Authorize** and paste the `access_token` from `/auth/login/`.

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/auth/signup/` | — | Create a patient account |
| POST | `/auth/login/` | — | Get a JWT (rate limited) |
| GET | `/auth/me/` | user | Current user |
| GET | `/centres/` | — | List centres · filters `city`, `test_id` · paginated |
| GET | `/centres/{id}/` | — | Centre with its available tests and prices |
| POST | `/centres/` | admin | Create a centre |
| POST | `/centres/{id}/tests/` | admin | Offer a test at a centre with a price |
| PATCH | `/centres/{id}/tests/{test_id}/` | admin | Change price or activate/deactivate |
| GET | `/tests/` | — | Test catalogue · search `q` · paginated |
| POST | `/tests/` | admin | Add a test to the catalogue |
| POST | `/bookings/` | user | Book a test → `PENDING` |
| GET | `/bookings/` | user | Your bookings (admins see all) · filter `status` · paginated |
| GET | `/bookings/{id}/` | user | One booking |
| POST | `/bookings/{id}/cancel/` | user | Cancel a booking |
| POST | `/payments/` | user | Pay for a booking via the mock provider |
| GET | `/payments/{id}/` | user | One of your payments |
| POST | `/payments/webhook/` | HMAC signature | Provider status update (idempotent) |
| GET | `/health` | — | Liveness |

**Pagination:** `?limit=20&offset=0` (limit ≤ 100). Responses look like `{"items": [...], "total": 42, "limit": 20, "offset": 0}`.

**Errors** always have one shape, with a stable machine-readable `code`:

```json
{ "error": { "code": "booking_already_paid", "message": "Booking is already paid" } }
```

Validation errors (`422`) also include `details` with the field location and reason.

**Money** is returned as a decimal string (`"349.00"`) to avoid floating-point errors. **Datetimes** are ISO-8601 in UTC (`Z`).

---

## Walkthrough with example requests

```bash
B=http://localhost:8000
```

**1. Sign up and log in**
```bash
curl -X POST $B/auth/signup/ -H 'Content-Type: application/json' \
  -d '{"email":"ashish@example.com","password":"Secret123","full_name":"Ashish"}'

curl -X POST $B/auth/login/ -H 'Content-Type: application/json' \
  -d '{"email":"ashish@example.com","password":"Secret123"}'
# {"access_token":"eyJ...","token_type":"bearer","expires_in":3600}

TOKEN=eyJ...
```

**2. Browse centres and tests**
```bash
curl "$B/centres/?city=noida"
curl "$B/centres/1/"
# {"id":1,"name":"CarePoint Diagnostics",...,"tests":[{"test":{"id":1,"code":"CBC",...},"price":"349.00","is_active":true}, ...]}
```

**3. Book a test** (the time must include a timezone; it's stored in UTC)
```bash
curl -X POST $B/bookings/ -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"centre_id":1,"test_id":1,"appointment_at":"2026-10-10T09:30:00+05:30"}'
# {"id":1,...,"appointment_at":"2026-10-10T04:00:00Z","amount":"349.00","status":"PENDING"}
```

**4. Pay** — `simulate_outcome` is optional (`SUCCESS`, `FAILED` or `PENDING`). Without it, the mock provider approves 80% of the time.
```bash
curl -X POST $B/payments/ -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: 5f1c2a' \
  -d '{"booking_id":1,"simulate_outcome":"SUCCESS"}'
# 201 {"payment":{"id":1,"status":"SUCCESS","amount":"349.00","provider_reference":"pay_55b3...",...},"booking_status":"CONFIRMED"}
```
Sending the same request with the same `Idempotency-Key` again returns the **same** payment with `200` and an `Idempotent-Replayed: true` header — nothing is charged twice.

**5. Webhook** — the included script plays the provider and signs the request:
```bash
# leave a payment awaiting confirmation…
curl -X POST $B/payments/ -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"booking_id":2,"simulate_outcome":"PENDING"}'

# …then confirm it, twice
python scripts/send_webhook.py pay_<reference> SUCCESS --event-id evt_1
# 200 {"event_id":"evt_1","result":"processed"}
python scripts/send_webhook.py pay_<reference> SUCCESS --event-id evt_1
# 200 {"event_id":"evt_1","result":"duplicate"}
```

To call the webhook by hand, sign the exact raw body:
```bash
BODY='{"event_id":"evt_2","provider_reference":"pay_<reference>","status":"FAILED","failure_reason":"Card declined"}'
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "whsec_dev_secret" -hex | sed 's/^.* //')"
curl -X POST $B/payments/webhook/ -H 'Content-Type: application/json' -H "X-Webhook-Signature: $SIG" -d "$BODY"
```

**6. Cancel**
```bash
curl -X POST $B/bookings/1/cancel/ -H "Authorization: Bearer $TOKEN"
```

---

## Database design

```mermaid
erDiagram
    users ||--o{ bookings : makes
    users ||--o{ payments : makes
    diagnostic_centres ||--o{ centre_tests : offers
    diagnostic_tests ||--o{ centre_tests : "offered as"
    centre_tests ||--o{ bookings : "booked via (centre_id, test_id)"
    bookings ||--o{ payments : "paid by"
    payments ||..o{ webhook_events : "provider_reference"

    users {
        int id PK
        varchar email UK
        varchar full_name
        varchar password_hash
        varchar role
    }
    diagnostic_centres {
        int id PK
        varchar name
        varchar address
        varchar city
        varchar pincode
    }
    diagnostic_tests {
        int id PK
        varchar code UK
        varchar name
        text description
    }
    centre_tests {
        int id PK
        int centre_id FK
        int test_id FK
        numeric price
        bool is_active
    }
    bookings {
        int id PK
        int user_id FK
        int centre_id FK
        int test_id FK
        timestamptz appointment_at
        numeric amount
        varchar status
    }
    payments {
        int id PK
        int booking_id FK
        int user_id FK
        numeric amount
        varchar status
        varchar provider_reference UK
        varchar idempotency_key
        varchar failure_reason
    }
    webhook_events {
        int id PK
        varchar event_id UK
        varchar provider_reference
        varchar status
        json payload
        varchar result
        timestamptz received_at
        timestamptz processed_at
    }
```

All tables except the append-only `webhook_events` also have `created_at` / `updated_at`.

**Key decisions**

- **Tests are a catalogue; prices live on `centre_tests`.** The same test (e.g. CBC) costs different amounts at different centres, so price belongs to the centre–test pair, not to either side.
- **Bookings snapshot the price** in `bookings.amount`. If an admin changes a price later, existing bookings and payments are unaffected. The client never sends an amount — not for bookings, not for payments — so it can't be tampered with.
- **Composite foreign key** `bookings(centre_id, test_id) → centre_tests(centre_id, test_id)`: the database itself guarantees you can only book a test the centre actually offers. Offerings are deactivated (`is_active=false`) rather than deleted, because bookings reference them.
- **`NUMERIC(10,2)` for money**, never float; `CHECK (price > 0)` / `CHECK (amount > 0)`.
- **`timestamptz`, stored in UTC.** The API rejects datetimes without a timezone instead of guessing.
- **Statuses are `VARCHAR` + `CHECK`** rather than native PostgreSQL enums, so adding a status later is a simple migration.
- **Business rules enforced by the database, not just the code** (so they hold even under race conditions):
  - `UNIQUE (webhook_events.event_id)` — the webhook idempotency guard.
  - Partial unique index on `payments(booking_id) WHERE status = 'SUCCESS'` — a booking can never have two successful payments.
  - `UNIQUE (payments.user_id, idempotency_key)` — one payment per client idempotency key.
  - Partial unique index on `bookings(user_id, centre_id, test_id, appointment_at) WHERE status IN ('PENDING','CONFIRMED','FAILED')` — no accidental double booking (e.g. a double-clicked button). Cancelling frees the slot.
- **Indexes** match the access paths: `bookings(user_id, status)` for "my bookings", `centres(city)`, `payments(booking_id)`, `webhook_events(provider_reference)`.

---

## Payments, webhook & idempotency

### State machines

```mermaid
stateDiagram-v2
    direction LR
    [*] --> PENDING: booking created
    PENDING --> CONFIRMED: payment SUCCESS
    PENDING --> FAILED: payment FAILED
    FAILED --> CONFIRMED: retry / late SUCCESS
    PENDING --> CANCELLED: user cancels
    FAILED --> CANCELLED: user cancels
    CONFIRMED --> CANCELLED: user cancels (refund)
```

| Payment transition | Allowed? | Why |
|---|---|---|
| PENDING → SUCCESS / FAILED | yes | normal resolution |
| FAILED → SUCCESS | yes | providers can report a timed-out payment as captured later |
| SUCCESS → anything | **no** (`ignored`) | money was captured; a late `FAILED` must not undo a confirmed booking |
| anything → PENDING | **no** | can't go backwards |

A `CANCELLED` booking is never revived by a late `SUCCESS`; the payment is recorded as captured and a `refund_required` warning is logged.

All transitions go through one function, `payment_service.apply_payment_outcome`, used by **both** the payment endpoint and the webhook, so the rules can't drift apart.

### How the mock provider works

`POST /payments/` creates a payment row with a unique `provider_reference` and asks the mock provider for a verdict:
- `SUCCESS` / `FAILED` are applied immediately (what the assignment asks for);
- `PENDING` models a real asynchronous gateway: the payment waits for the webhook. While a payment is pending, another payment attempt for the same booking is rejected (`payment_in_progress`).

### Why the webhook is idempotent

`POST /payments/webhook/` processes each event **exactly once**:

1. **Authenticate:** verify `X-Webhook-Signature: sha256=<hex>` = HMAC-SHA256 of the raw body with `WEBHOOK_SECRET`, using a constant-time comparison. Unsigned or wrongly signed requests get `401` and change nothing.
2. **Record the event and apply it in one transaction.** We insert into `webhook_events` first. `event_id` is `UNIQUE`, so a redelivery fails that insert → we roll back and reply `200 {"result": "duplicate"}`. This works even if two copies arrive at the same instant, because the database (not an "if exists" check in code) decides who wins.
3. **Lock the rows** (`SELECT … FOR UPDATE`, booking first, then payment) and apply the transition through the state machine. Out-of-order events are safe: e.g. `FAILED` after `SUCCESS` is answered `200 {"result":"ignored"}`.
4. **Commit both or neither.** If anything fails midway (unknown payment reference, amount mismatch, crash), the event isn't recorded, so the provider's automatic retry is processed normally rather than being wrongly treated as a duplicate.

We respond `200` for every event we've handled (`processed`, `duplicate`, `noop`, `ignored`) so the provider stops retrying, and non-2xx only for invalid requests or conditions a retry might fix.

### Concurrency

Paying locks the booking row, so two parallel payment requests for one booking are serialised: exactly one charges, the rest get `409 booking_already_paid`. The idempotency-key lookup happens *after* taking that lock, so parallel retries with the same key all receive the original payment. Every path locks booking → payment in the same order to avoid deadlocks. These guarantees are verified by `tests/test_concurrency.py` against PostgreSQL.

---

## Edge cases handled

| Case | Behaviour |
|---|---|
| Invalid body / unknown fields / wrong types | `422 validation_error` with field details. Extra fields are rejected, so clients can't send `amount`, `status` or `role` |
| Weak password, invalid email, blank name | `422` |
| Duplicate email (any letter case) | `409 email_taken` |
| Wrong password vs unknown email | identical `401` response and similar timing (no account enumeration) |
| Brute-force login | `429` with `Retry-After` after 5 attempts/minute per IP |
| Missing, malformed, expired or forged JWT | `401` (`invalid_token` / `token_expired`) |
| Patient calling admin endpoints | `403` |
| Viewing/cancelling/paying someone else's booking | `404` — same as a non-existent id, so ids can't be probed |
| Invalid / non-existent booking id | `404 booking_not_found` (`422` if not an integer) |
| Appointment in the past, >90 days ahead, or without timezone | `422` |
| Test not offered / deactivated at that centre | `404 test_not_offered` / `422 test_unavailable` |
| Same user booking the same slot twice | `409 duplicate_booking` |
| Paying a confirmed / cancelled booking | `409 booking_already_paid` / `409 booking_cancelled` |
| Payment failed | booking → `FAILED`; user may retry payment on it |
| Retrying a payment request (network glitch) | same `Idempotency-Key` → original payment, `200` |
| Reusing an idempotency key for a different booking | `409 idempotency_key_reused` |
| Same webhook delivered N times (even concurrently) | applied once; rest `200 duplicate` |
| Old `event_id` replayed with a different status | `duplicate`, state unchanged (logged as suspicious) |
| `FAILED` webhook after `SUCCESS` | `ignored`, booking stays `CONFIRMED` |
| Webhook for unknown payment | `404`, not recorded, so a later retry can succeed |
| Webhook amount ≠ payment amount | `422 amount_mismatch`, nothing changed |
| Late `SUCCESS` for a cancelled booking | payment recorded, booking stays `CANCELLED`, `refund_required` logged |
| Price changed after booking | booking keeps its original amount |
| Unexpected server error | `500 internal_error` with no internals leaked; full trace in logs |

---

## Assumptions

1. **Roles:** public signup creates `PATIENT`s. `ADMIN`s manage centres, tests and prices and are created by the seed script — letting anyone sign up as admin would be a security hole. Browsing centres and tests is public; everything about bookings and payments requires login.
2. **Booking states:** a booking is `PENDING` until paid. A failed payment moves it to `FAILED`, and the user can retry payment on the **same** booking (only `CANCELLED` frees the slot for a new booking). A confirmed booking can be cancelled; the refund itself is out of scope and is logged as `refund_required`.
3. **One payment row = one attempt.** Retries create new payment rows, giving a full audit trail; at most one can be `SUCCESS`.
4. **Amount is always derived server-side** from the booking.
5. **Slot capacity is not modelled.** The assignment doesn't mention opening hours or capacity, so any future time within 90 days is bookable; only the same user double-booking the same slot is prevented.
6. **Webhook authentication** uses a shared-secret HMAC, the common pattern among real gateways. Each event carries a unique `event_id` generated by the provider.
7. **Location** is modelled as address + city + 6-digit Indian PIN code (no geo-coordinates).
8. **Single currency (INR).**
9. **Access tokens only** (no refresh tokens or logout/revocation), 60-minute expiry.

---

## Project structure

```
app/
├── main.py              # app factory, request-id/logging middleware, routers
├── config.py            # settings from environment
├── database.py          # engine, session, naming conventions
├── models.py            # ORM models + DB-level constraints
├── schemas.py           # request/response models (API contract)
├── security.py          # bcrypt, JWT, webhook HMAC
├── deps.py              # auth & pagination dependencies
├── errors.py            # domain errors → consistent JSON errors
├── rate_limit.py        # sliding-window login limiter
├── logging_config.py    # JSON logs with request_id
├── seed.py              # idempotent sample data + admin
├── routers/             # thin HTTP layer: auth, centres, catalog, bookings, payments
└── services/            # business logic: booking_service, payment_service (state machine + webhook)
tests/                   # pytest suite (SQLite by default; PostgreSQL incl. concurrency)
scripts/send_webhook.py  # sign & send webhooks like a provider would
```

Routers only translate HTTP ↔ Python; all rules live in `services/`, which keeps them easy to test and to reuse (e.g. from a background worker).

---

## What I would improve with more time

- **Alembic migrations** instead of `create_all`, so schema changes are versioned and reviewable.
- **Background processing (Celery/RQ):** store the webhook quickly, process it asynchronously with retries, exponential backoff and a dead-letter queue. Plus a periodic job that reconciles payments stuck in `PENDING` by asking the provider.
- **Refund flow:** a `REFUND_PENDING → REFUNDED` payment state for cancelled-after-payment and double-capture cases, instead of only logging them.
- **Slot management:** centre opening hours and per-slot capacity, with locking to prevent overbooking.
- **Webhook replay window:** a signed timestamp, rejecting events older than a few minutes; secret rotation with two active secrets.
- **Auth:** refresh tokens, token revocation, email verification, password reset.
- **Redis** for rate-limit counters shared across instances, and caching the public centre/test listings (invalidated on admin updates).
- **Observability:** metrics (payment success rate, webhook lag), OpenTelemetry tracing, alerts on `refund_required` log events.
- **Cursor-based pagination** for large booking histories.
- Run under Gunicorn with multiple Uvicorn workers, and add a readiness probe that checks DB connectivity.
