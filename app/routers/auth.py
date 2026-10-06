import secrets

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password, verify_password
from app.database import get_db
from app.dependencies import CurrentUser, get_current_user
from app.models import Role, User
from app.schemas import ChangePasswordRequest, ResetPasswordOut, ResetPasswordRequest
from app.services import audit_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
async def login(form: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)):
    user = await db.scalar(select(User).where(User.email == form.username))
    if user is None or not user.is_active or not verify_password(form.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect email or password")

    role = await db.get(Role, user.role_id)
    token = create_access_token(
        user_id=user.id,
        role=role.name,
        merchant_id=user.merchant_id,
        shop_id=user.shop_id,
        must_change_password=user.must_change_password,
        email=user.email,
        full_name=user.full_name,
    )

    # TODO: if user.mfa_secret is set, require and verify a TOTP code here before
    # issuing the token rather than after — this stub intentionally skips MFA.

    return {"access_token": token, "token_type": "bearer", "must_change_password": user.must_change_password}


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Any authenticated user changes their own password — always requires
    the current password, so a hijacked-but-not-yet-logged-out session can't
    be used to lock the real owner out."""
    db_user = await db.get(User, user.id)
    if db_user is None or not verify_password(body.current_password, db_user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    if body.new_password == body.current_password:
        raise HTTPException(status_code=400, detail="Choose a new password that's different from the current one")

    db_user.hashed_password = hash_password(body.new_password)
    db_user.must_change_password = False
    await db.commit()
    return {"detail": "Password changed"}


# No 0/O, 1/l/I — this gets read aloud or copied off a screen to someone else.
_TEMP_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"


@router.post("/reset-user-password", response_model=ResetPasswordOut)
async def reset_user_password(
    body: ResetPasswordRequest,
    response: Response,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Reset someone else's password, looked up by their email (the login
    name — there is no separate username).

    - platform_admin: any merchant owner or teller, on any merchant.
    - merchant_owner: tellers of their OWN merchant only. Not other owners
      (a co-owner must not be able to take over the main owner's account) and
      not anyone at another merchant.
    - anyone else: refused.

    Out-of-scope emails get the same "no matching user" as ones that don't
    exist, so this can't be used to discover who has an account elsewhere.

    The new password is generated here and returned exactly once. The account
    is flagged so it must choose its own password before doing anything else,
    which means the person who ran the reset doesn't end up knowing a
    working password for it."""
    if user.role not in ("platform_admin", "merchant_owner"):
        raise HTTPException(status_code=403, detail="You don't have permission to reset passwords")

    row = (
        await db.execute(
            select(User, Role.name)
            .join(Role, User.role_id == Role.id)
            .where(func.lower(User.email) == body.email.strip().lower())
        )
    ).first()
    not_found = HTTPException(status_code=404, detail="No matching user found")
    if row is None:
        raise not_found
    target, role_name = row

    if user.role == "platform_admin":
        in_scope = role_name in ("merchant_owner", "teller")
    else:
        in_scope = role_name == "teller" and target.merchant_id is not None and target.merchant_id == user.merchant_id
    if not in_scope:
        raise not_found

    temporary_password = "".join(secrets.choice(_TEMP_PASSWORD_ALPHABET) for _ in range(10))
    target.hashed_password = hash_password(temporary_password)
    target.must_change_password = True

    # The password itself is deliberately NOT recorded anywhere.
    await audit_service.record(
        db,
        actor_user_id=user.id,
        action="user.password_reset",
        target_type="user",
        target_id=str(target.id),
        details={"email": target.email, "role": role_name},
    )
    await db.commit()

    response.headers["Cache-Control"] = "no-store"
    return ResetPasswordOut(
        email=target.email, full_name=target.full_name, role=role_name, temporary_password=temporary_password
    )
