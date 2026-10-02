from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.deps import Pagination


def paginate(db: Session, stmt: Select, page: Pagination) -> tuple[list, int]:
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    items = db.scalars(stmt.limit(page.limit).offset(page.offset)).unique().all()
    return list(items), total
