"""
Session Management Routes — with per-user isolation and JWT authentication.
"""
import uuid
from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, and_
from app.core.database import get_db
from app.core.auth_utils import get_current_user
from app.models.models import Session as SessionModel, SessionStatus, User

router = APIRouter(prefix="/sessions", tags=["sessions"])


class SessionCreate(BaseModel):
    name: Optional[str] = "New Session"


class SessionResponse(BaseModel):
    id: str
    name: str
    status: str
    created_at: datetime
    updated_at: datetime
    message_count: int = 0

    model_config = {"from_attributes": True}


@router.get("", response_model=List[SessionResponse])
async def list_sessions(
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List sessions for the authenticated user, ordered by most recent."""
    # FIX: filter by user_id so each user only sees their own sessions
    result = await db.execute(
        select(SessionModel)
        .where(SessionModel.user_id == str(current_user.id))
        .order_by(desc(SessionModel.updated_at))
        .limit(limit)
        .offset(offset)
    )
    sessions = result.scalars().all()
    return [
        SessionResponse(
            id=s.id,
            name=s.name,
            status=s.status.value,
            created_at=s.created_at,
            updated_at=s.updated_at,
            message_count=len(s.messages),
        )
        for s in sessions
    ]


@router.post("", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    payload: SessionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a new chat session owned by the authenticated user."""
    # FIX: set user_id so the session belongs to the creating user
    session = SessionModel(
        id=str(uuid.uuid4()),
        name=payload.name or "New Session",
        status=SessionStatus.ACTIVE,
        user_id=str(current_user.id),
    )
    db.add(session)
    await db.flush()
    await db.refresh(session)
    return SessionResponse(
        id=session.id,
        name=session.name,
        status=session.status.value,
        created_at=session.created_at,
        updated_at=session.updated_at,
        message_count=0,
    )


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get a specific session by ID — only accessible to its owner."""
    result = await db.execute(
        select(SessionModel).where(SessionModel.id == session_id)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    # FIX: ownership check — prevent user A from reading user B's session
    if session.user_id != str(current_user.id):
        raise HTTPException(status_code=403, detail="Access denied")
    return SessionResponse(
        id=session.id,
        name=session.name,
        status=session.status.value,
        created_at=session.created_at,
        updated_at=session.updated_at,
        message_count=len(session.messages),
    )


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a session — only the owning user can delete it."""
    result = await db.execute(
        select(SessionModel).where(SessionModel.id == session_id)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    # FIX: ownership check before deletion
    if session.user_id != str(current_user.id):
        raise HTTPException(status_code=403, detail="Access denied")
    await db.delete(session)
