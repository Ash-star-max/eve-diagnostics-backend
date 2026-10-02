"""Password hashing, JWT handling and webhook signature verification."""
import hashlib
import hmac
from datetime import timedelta

import bcrypt
import jwt

from app.config import get_settings
from app.utils import utcnow


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=get_settings().bcrypt_rounds)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


# Used to spend the same time on "unknown email" as on "wrong password", so
# login response timing doesn't reveal which emails are registered.
DUMMY_PASSWORD_HASH = hash_password("not-a-real-password-0")


def create_access_token(user_id: int) -> tuple[str, int]:
    settings = get_settings()
    now = utcnow()
    expires_in = settings.jwt_expire_minutes * 60
    payload = {
        "sub": str(user_id),
        "type": "access",
        "iat": now,
        "exp": now + timedelta(seconds=expires_in),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_in


def decode_access_token(token: str) -> dict:
    """Raises jwt.InvalidTokenError (or a subclass) if the token is not valid."""
    settings = get_settings()
    payload = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
        options={"require": ["exp", "sub"]},
    )
    if payload.get("type") != "access":
        raise jwt.InvalidTokenError("Not an access token")
    return payload


def sign_webhook(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_webhook_signature(body: bytes, signature: str | None, secret: str) -> bool:
    if not signature:
        return False
    return hmac.compare_digest(sign_webhook(body, secret), signature)
