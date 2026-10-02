from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.deps import get_current_user
from app.errors import Unauthorized, Unprocessable, clean_validation_errors
from app.models import User
from app.schemas import PaymentCreate, PaymentOut, PaymentResult, WebhookAck, WebhookPayload
from app.security import verify_webhook_signature
from app.services import payment_service

router = APIRouter(prefix="/payments", tags=["payments"])


@router.post(
    "/",
    response_model=PaymentResult,
    status_code=201,
    summary="Pay for a booking (mock provider)",
    responses={200: {"description": "Idempotent replay of an earlier request with the same Idempotency-Key"}},
)
def create_payment(
    body: PaymentCreate,
    response: Response,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        min_length=1,
        max_length=100,
        description="Optional. Retrying with the same key returns the original payment instead of charging again.",
    ),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PaymentResult:
    payment, booking, created = payment_service.create_payment(db, user, body, idempotency_key)
    if not created:
        response.status_code = 200
        response.headers["Idempotent-Replayed"] = "true"
    return PaymentResult(payment=PaymentOut.model_validate(payment), booking_status=booking.status)


@router.get("/{payment_id}/", response_model=PaymentOut, summary="Get one of your payments")
def get_payment(payment_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> PaymentOut:
    return PaymentOut.model_validate(payment_service.get_payment(db, user, payment_id))


@router.post(
    "/webhook/",
    response_model=WebhookAck,
    summary="Payment-status webhook from the provider (idempotent)",
    description=(
        "Authenticated with an HMAC-SHA256 signature of the raw body in the "
        "`X-Webhook-Signature: sha256=<hex>` header. Redelivering the same `event_id` "
        "is acknowledged with `result: duplicate` and changes nothing."
    ),
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": WebhookPayload.model_json_schema()}},
        }
    },
)
async def payment_webhook(
    request: Request,
    x_webhook_signature: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> WebhookAck:
    # The signature covers the exact bytes sent, so read the raw body first.
    raw_body = await request.body()
    if not verify_webhook_signature(raw_body, x_webhook_signature, get_settings().webhook_secret):
        raise Unauthorized("Invalid webhook signature", code="invalid_signature")

    try:
        payload = WebhookPayload.model_validate_json(raw_body)
    except ValidationError as exc:
        raise Unprocessable("Invalid webhook payload", code="validation_error", details=clean_validation_errors(exc.errors()))

    # DB work is synchronous; keep it off the event loop.
    result = await run_in_threadpool(payment_service.process_webhook, db, payload)
    return WebhookAck(event_id=payload.event_id, result=result)
