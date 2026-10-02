from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import Pagination, get_current_user, pagination
from app.models import BookingStatus, User
from app.schemas import BookingCreate, BookingOut, Page
from app.services import booking_service
from app.services.pagination import paginate

router = APIRouter(prefix="/bookings", tags=["bookings"])


@router.post("/", response_model=BookingOut, status_code=201, summary="Book a test (status starts as PENDING)")
def create_booking(
    body: BookingCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> BookingOut:
    return BookingOut.model_validate(booking_service.create_booking(db, user, body))


@router.get("/", response_model=Page[BookingOut], summary="Your bookings (admins see all)")
def list_bookings(
    status: BookingStatus | None = Query(None),
    page: Pagination = Depends(pagination),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    items, total = paginate(db, booking_service.list_bookings_stmt(user, status), page)
    return Page(
        items=[BookingOut.model_validate(b) for b in items], total=total, limit=page.limit, offset=page.offset
    )


@router.get("/{booking_id}/", response_model=BookingOut, summary="Get one booking")
def get_booking(booking_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> BookingOut:
    return BookingOut.model_validate(booking_service.get_booking(db, user, booking_id))


@router.post("/{booking_id}/cancel/", response_model=BookingOut, summary="Cancel a booking")
def cancel_booking(
    booking_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> BookingOut:
    return BookingOut.model_validate(booking_service.cancel_booking(db, user, booking_id))
