from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.deps import Pagination, pagination, require_admin
from app.errors import Conflict, NotFound
from app.models import CentreTest, DiagnosticCentre, DiagnosticTest
from app.schemas import (
    CentreCreate,
    CentreDetail,
    CentreOut,
    OfferingCreate,
    OfferingOut,
    OfferingUpdate,
    Page,
)
from app.services.pagination import paginate

router = APIRouter(prefix="/centres", tags=["diagnostic centres"])


def _get_centre(db: Session, centre_id: int) -> DiagnosticCentre:
    centre = db.get(DiagnosticCentre, centre_id)
    if centre is None:
        raise NotFound("Diagnostic centre not found", code="centre_not_found")
    return centre


def _get_offering(db: Session, centre_id: int, test_id: int) -> CentreTest:
    offering = db.scalar(
        select(CentreTest)
        .options(selectinload(CentreTest.test))
        .where(CentreTest.centre_id == centre_id, CentreTest.test_id == test_id)
    )
    if offering is None:
        raise NotFound("This centre does not offer that test", code="test_not_offered")
    return offering


@router.get("/", response_model=Page[CentreOut], summary="List centres (public)")
def list_centres(
    city: str | None = Query(None, max_length=100),
    test_id: int | None = Query(None, gt=0, description="Only centres currently offering this test"),
    page: Pagination = Depends(pagination),
    db: Session = Depends(get_db),
):
    stmt = select(DiagnosticCentre).order_by(DiagnosticCentre.name, DiagnosticCentre.id)
    if city:
        stmt = stmt.where(DiagnosticCentre.city.ilike(city.strip()))
    if test_id is not None:
        stmt = stmt.join(CentreTest).where(CentreTest.test_id == test_id, CentreTest.is_active.is_(True))
    items, total = paginate(db, stmt, page)
    return Page(items=items, total=total, limit=page.limit, offset=page.offset)


@router.get("/{centre_id}/", response_model=CentreDetail, summary="Centre with its available tests and prices")
def get_centre(centre_id: int, db: Session = Depends(get_db)) -> CentreDetail:
    centre = _get_centre(db, centre_id)
    offerings = db.scalars(
        select(CentreTest)
        .options(selectinload(CentreTest.test))
        .join(DiagnosticTest)
        .where(CentreTest.centre_id == centre_id, CentreTest.is_active.is_(True))
        .order_by(DiagnosticTest.name)
    ).all()
    return CentreDetail(
        **CentreOut.model_validate(centre).model_dump(),
        tests=[OfferingOut.model_validate(o) for o in offerings],
    )


@router.post(
    "/",
    response_model=CentreOut,
    status_code=201,
    dependencies=[Depends(require_admin)],
    summary="Create a centre (admin)",
)
def create_centre(body: CentreCreate, db: Session = Depends(get_db)) -> DiagnosticCentre:
    centre = DiagnosticCentre(**body.model_dump())
    db.add(centre)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise Conflict("A centre with this name already exists in this city", code="centre_exists")
    return centre


@router.post(
    "/{centre_id}/tests/",
    response_model=OfferingOut,
    status_code=201,
    dependencies=[Depends(require_admin)],
    summary="Offer a test at a centre, with its price (admin)",
)
def add_offering(centre_id: int, body: OfferingCreate, db: Session = Depends(get_db)) -> OfferingOut:
    _get_centre(db, centre_id)
    if db.get(DiagnosticTest, body.test_id) is None:
        raise NotFound("Diagnostic test not found", code="test_not_found")

    offering = CentreTest(centre_id=centre_id, test_id=body.test_id, price=body.price)
    db.add(offering)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise Conflict("This centre already offers this test; use PATCH to change it", code="offering_exists")
    return OfferingOut.model_validate(_get_offering(db, centre_id, body.test_id))


@router.patch(
    "/{centre_id}/tests/{test_id}/",
    response_model=OfferingOut,
    dependencies=[Depends(require_admin)],
    summary="Change price or availability (admin). Existing bookings keep their price.",
)
def update_offering(centre_id: int, test_id: int, body: OfferingUpdate, db: Session = Depends(get_db)) -> OfferingOut:
    offering = _get_offering(db, centre_id, test_id)
    if body.price is not None:
        offering.price = body.price
    if body.is_active is not None:
        offering.is_active = body.is_active
    db.commit()
    return OfferingOut.model_validate(offering)
