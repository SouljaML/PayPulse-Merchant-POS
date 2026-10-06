import uuid
from datetime import datetime, timedelta, timezone

from jose import jwt
from passlib.context import CryptContext

from app.config import get_settings

settings = get_settings()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(
    *,
    user_id: uuid.UUID,
    role: str,
    merchant_id: uuid.UUID | None,
    shop_id: uuid.UUID | None = None,
    must_change_password: bool = False,
    email: str | None = None,
    full_name: str | None = None,
) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {
        "sub": str(user_id),
        "role": role,
        "merchant_id": str(merchant_id) if merchant_id else None,
        "shop_id": str(shop_id) if shop_id else None,
        "must_change_password": must_change_password,
        "email": email,
        "full_name": full_name,
        "exp": expire,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
