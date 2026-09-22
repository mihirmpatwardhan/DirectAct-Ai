"""
Chat Routes — message persistence + LLM stub (Phase 2 will add real streaming).
All endpoints require JWT authentication and verify session ownership.
"""
import uuid
import logging
from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from app.core.database import get_db
from app.core.config import settings
from app.core.auth_utils import get_current_user
from app.models.models import Message, MessageRole, Session as SessionModel, User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])


class ChatRequest(BaseModel):
    session_id: str
    content: str


class MessageResponse(BaseModel):
    id: str
    session_id: str
    role: str
    content: str
    created_at: datetime
    tokens_used: int = 0

    model_config = {"from_attributes": True}


class ChatResponse(BaseModel):
    user_message: MessageResponse
    assistant_message: MessageResponse


async def _get_owned_session(
    session_id: str,
    db: AsyncSession,
    current_user: User,
) -> SessionModel:
    """
    Load a session and verify the requesting user owns it.
    Raises HTTP 404 if not found, HTTP 403 if owned by another user.
    """
    sess_result = await db.execute(
        select(SessionModel).where(SessionModel.id == session_id)
    )
    session = sess_result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    # FIX: prevent cross-user message reads/writes
    if session.user_id != str(current_user.id):
        raise HTTPException(status_code=403, detail="Access denied")
    return session


@router.get("/{session_id}/messages", response_model=List[MessageResponse])
async def get_messages(
    session_id: str,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Retrieve message history for a session — authenticated & owned by caller."""
    # FIX: verifies ownership before loading messages
    await _get_owned_session(session_id, db, current_user)

    result = await db.execute(
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.created_at)
        .limit(limit)
    )
    messages = result.scalars().all()
    return [
        MessageResponse(
            id=m.id,
            session_id=m.session_id,
            role=m.role.value,
            content=m.content,
            created_at=m.created_at,
            tokens_used=m.tokens_used,
        )
        for m in messages
    ]


@router.post("/send", response_model=ChatResponse)
async def send_message(
    payload: ChatRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Send a message and get a response.
    Requires authentication and ownership of the target session.
    """
    # FIX: verifies session belongs to the calling user
    await _get_owned_session(payload.session_id, db, current_user)

    # Save user message
    user_msg = Message(
        id=str(uuid.uuid4()),
        session_id=payload.session_id,
        role=MessageRole.USER,
        content=payload.content,
    )
    db.add(user_msg)

    # Generate stub response (Phase 2 will replace with real LLM)
    stub_response = _generate_stub_response(payload.content)
    assistant_msg = Message(
        id=str(uuid.uuid4()),
        session_id=payload.session_id,
        role=MessageRole.ASSISTANT,
        content=stub_response,
    )
    db.add(assistant_msg)
    await db.flush()

    logger.info(f"Chat: session={payload.session_id}, user='{payload.content[:50]}...'")

    return ChatResponse(
        user_message=MessageResponse(
            id=user_msg.id,
            session_id=user_msg.session_id,
            role=user_msg.role.value,
            content=user_msg.content,
            created_at=user_msg.created_at,
        ),
        assistant_message=MessageResponse(
            id=assistant_msg.id,
            session_id=assistant_msg.session_id,
            role=assistant_msg.role.value,
            content=assistant_msg.content,
            created_at=assistant_msg.created_at,
        ),
    )


def _generate_stub_response(user_input: str) -> str:
    """Phase 1 stub — echoes intent. Phase 2 replaces with LLM."""
    lower = user_input.lower()
    if any(kw in lower for kw in ["open", "launch", "start"]):
        return (
            f"🖥️ **Desktop Action Detected**\n\n"
            f"I'll help you with: *{user_input}*\n\n"
            f"*(Phase 4 Intent Router will classify and execute this)*"
        )
    elif any(kw in lower for kw in ["search", "go to", "browse", "open website", "navigate"]):
        return (
            f"🌐 **Web Action Detected**\n\n"
            f"I'll navigate the web for: *{user_input}*\n\n"
            f"*(Phase 5 Playwright Engine will handle this)*"
        )
    else:
        return (
            f"👋 **DirectAct-AI Online**\n\n"
            f"You said: *{user_input}*\n\n"
            f"I'm your AI Copilot — I can control your **browser** and **desktop**. "
            f"Try asking me to open an app, search the web, or automate a task!\n\n"
            f"*(LLM integration coming in Phase 2)*"
        )
