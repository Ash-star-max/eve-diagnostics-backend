"""Shared FastAPI dependencies: authentication, authorisation, pagination."""
from dataclasses import dataclass

import jwt
from fastapi import Depends, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.database import get_db
from app.errors import Forbidden, Unauthorized
from app.models import User, UserRole
from app.security import decode_access_token

# auto_error=False so a missing token gets our consistent 401 body.
_bearer = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise Unauthorized("Missing bearer token")
    try:
        payload = decode_access_token(credentials.credentials)
        user_id = int(payload["sub"])
    except jwt.ExpiredSignatureError:
        raise Unauthorized("Token has expired", code="token_expired")
    except (jwt.InvalidTokenError, ValueError):
        raise Unauthorized("Invalid token", code="invalid_token")

    # Role is read from the database, never trusted from the token.
    user = db.get(User, user_id)
    if user is None:
        raise Unauthorized("Invalid token", code="invalid_token")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != UserRole.ADMIN:
        raise Forbidden("Admin access required")
    return user


@dataclass(frozen=True)
class Pagination:
    limit: int
    offset: int


def pagination(
    limit: int = Query(20, ge=1, le=100, description="Page size (max 100)"),
    offset: int = Query(0, ge=0),
) -> Pagination:
    return Pagination(limit=limit, offset=offset)
