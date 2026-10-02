"""Global catalogue of diagnostic tests (prices are per centre)."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import Pagination, pagination, require_admin
from app.errors import Conflict
from app.models import DiagnosticTest
from app.schemas import DiagnosticTestCreate, DiagnosticTestOut, Page
from app.services.pagination import paginate

router = APIRouter(prefix="/tests", tags=["diagnostic tests"])


@router.get("/", response_model=Page[DiagnosticTestOut], summary="List diagnostic tests (public)")
def list_tests(
    q: str | None = Query(None, max_length=100, description="Search by name or code"),
    page: Pagination = Depends(pagination),
    db: Session = Depends(get_db),
):
    stmt = select(DiagnosticTest).order_by(DiagnosticTest.name)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(DiagnosticTest.name.ilike(like), DiagnosticTest.code.ilike(like)))
    items, total = paginate(db, stmt, page)
    return Page(items=items, total=total, limit=page.limit, offset=page.offset)


@router.post(
    "/",
    response_model=DiagnosticTestOut,
    status_code=201,
    dependencies=[Depends(require_admin)],
    summary="Add a test to the catalogue (admin)",
)
def create_test(body: DiagnosticTestCreate, db: Session = Depends(get_db)) -> DiagnosticTest:
    test = DiagnosticTest(code=body.code, name=body.name, description=body.description)
    db.add(test)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise Conflict(f"A test with code {body.code} already exists", code="test_code_taken")
    return test
