import uuid
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models import User

settings = get_settings()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


@dataclass
class CurrentUser:
    id: uuid.UUID
    merchant_id: uuid.UUID | None
    shop_id: uuid.UUID | None
    role: str


async def get_current_user(
    request: Request,
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        user_id = payload.get("sub")
        if user_id is None:
            raise credentials_error
    except JWTError:
        raise credentials_error

    # The token proves who logged in, not that the account is still allowed
    # to act. Check the row itself, so deactivating someone takes effect on
    # their very next request instead of whenever their token expires.
    db_user = await db.get(User, uuid.UUID(user_id))
    if db_user is None or not db_user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="This account has been deactivated")
    if db_user.must_change_password and request.url.path != "/auth/change-password":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You need to change your temporary password before you can continue",
        )

    merchant_id = payload.get("merchant_id")
    shop_id = payload.get("shop_id")
    return CurrentUser(
        id=uuid.UUID(user_id),
        merchant_id=uuid.UUID(merchant_id) if merchant_id else None,
        shop_id=uuid.UUID(shop_id) if shop_id else None,
        role=payload.get("role", ""),
    )


def require_roles(*allowed_roles: str):
    """Usage: Depends(require_roles('platform_admin', 'compliance_officer'))"""

    async def _check(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role not in allowed_roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return user

    return _check


async def require_merchant_scope(
    merchant_id: uuid.UUID, user: CurrentUser = Depends(get_current_user)
) -> CurrentUser:
    """Ensures a merchant-portal user can only ever act on their own merchant_id,
    even if platform_admin routes are structurally similar."""
    if user.role == "platform_admin":
        return user
    if user.merchant_id != merchant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized for this merchant")
    return user


async def require_merchant_manager(
    merchant_id: uuid.UUID, user: CurrentUser = Depends(get_current_user)
) -> CurrentUser:
    """Stricter than require_merchant_scope: shop/till/teller management is
    an owner-level action, not something any logged-in teller of the same
    merchant should be able to do just because they belong to it."""
    if user.role == "platform_admin":
        return user
    if user.role == "merchant_owner" and user.merchant_id == merchant_id:
        return user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN, detail="Only the merchant owner or a platform admin can do this"
    )
