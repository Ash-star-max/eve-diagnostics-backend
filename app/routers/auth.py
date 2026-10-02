from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.errors import Conflict, Unauthorized
from app.models import User, UserRole
from app.rate_limit import limit_login_attempts
from app.schemas import LoginRequest, SignupRequest, TokenResponse, UserOut
from app.security import DUMMY_PASSWORD_HASH, create_access_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup/", response_model=UserOut, status_code=201, summary="Create a patient account")
def signup(body: SignupRequest, db: Session = Depends(get_db)) -> User:
    email = body.email.lower()
    if db.scalar(select(User.id).where(User.email == email)) is not None:
        raise Conflict("An account with this email already exists", code="email_taken")

    # Public signup always creates a PATIENT; admins are provisioned via seed.
    user = User(email=email, full_name=body.full_name, password_hash=hash_password(body.password), role=UserRole.PATIENT)
    db.add(user)
    try:
        db.commit()
    except IntegrityError:  # lost a race with a concurrent signup
        db.rollback()
        raise Conflict("An account with this email already exists", code="email_taken")
    return user


@router.post(
    "/login/",
    response_model=TokenResponse,
    dependencies=[Depends(limit_login_attempts)],
    summary="Exchange email + password for a JWT",
)
def login(body: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.scalar(select(User).where(User.email == body.email.lower()))
    if user is None:
        verify_password(body.password, DUMMY_PASSWORD_HASH)  # equalise timing
        raise Unauthorized("Invalid email or password", code="invalid_credentials")
    if not verify_password(body.password, user.password_hash):
        raise Unauthorized("Invalid email or password", code="invalid_credentials")

    token, expires_in = create_access_token(user.id)
    return TokenResponse(access_token=token, expires_in=expires_in)


@router.get("/me/", response_model=UserOut, summary="Current user")
def me(user: User = Depends(get_current_user)) -> User:
    return user
