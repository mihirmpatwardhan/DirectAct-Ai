"""
DirectAct-AI — Authentication Routes
Register, Login, and user profile endpoints.
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.auth_utils import (
    hash_password,
    verify_password,
    create_access_token,
    get_current_user,
)
from app.models.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["authentication"])


# ── Request / Response Schemas ────────────────────────────────────────────────


class RegisterRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    email: EmailStr
    password: str = Field(..., min_length=6, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1)


class AuthResponse(BaseModel):
    token: str
    user: dict


class UserResponse(BaseModel):
    id: str
    name: str
    email: str
    avatar_url: str
    is_active: bool
    created_at: str


# ── Routes ────────────────────────────────────────────────────────────────────


@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    """Register a new user account."""
    # Check if email already exists
    existing = await db.execute(select(User).where(User.email == body.email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists",
        )

    # Create user
    user = User(
        name=body.name,
        email=body.email,
        hashed_password=hash_password(body.password),
    )
    db.add(user)
    # FIX: flush to get the generated ID, then commit to actually persist.
    # The get_db dependency auto-commits on success, but we flush here so
    # we can read back the generated id before the response is returned.
    await db.flush()
    await db.refresh(user)

    # Generate token — capture values before commit closes the session
    user_id = str(user.id)
    user_email = user.email
    user_name = user.name
    user_avatar = user.avatar_url or ""
    user_active = user.is_active
    user_created = user.created_at.isoformat()

    token = create_access_token(data={"sub": user_id, "email": user_email})

    logger.info(f"✅ New user registered: {user_email}")

    return AuthResponse(
        token=token,
        user={
            "id": user_id,
            "name": user_name,
            "email": user_email,
            "avatar_url": user_avatar,
            "is_active": user_active,
            "created_at": user_created,
        },
    )


@router.post("/login", response_model=AuthResponse)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    """Authenticate a user and return a JWT token."""
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    if user is None or not verify_password(body.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is deactivated",
        )

    # Capture values before the session potentially closes
    user_id = str(user.id)
    user_email = user.email
    user_name = user.name
    user_avatar = user.avatar_url or ""
    user_active = user.is_active
    user_created = user.created_at.isoformat()

    token = create_access_token(data={"sub": user_id, "email": user_email})

    logger.info(f"🔐 User logged in: {user_email}")

    return AuthResponse(
        token=token,
        user={
            "id": user_id,
            "name": user_name,
            "email": user_email,
            "avatar_url": user_avatar,
            "is_active": user_active,
            "created_at": user_created,
        },
    )


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    """Get the currently authenticated user's profile."""
    return UserResponse(
        id=str(current_user.id),
        name=current_user.name,
        email=current_user.email,
        avatar_url=current_user.avatar_url or "",
        is_active=current_user.is_active,
        created_at=current_user.created_at.isoformat(),
    )
