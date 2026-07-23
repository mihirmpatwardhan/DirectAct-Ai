"""
Actions API Routes — Phases 2–4
=================================
REST endpoints for action logs, approval gates, and execution triggers.
"""
import logging
from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from app.core.database import get_db
from app.models.models import ActionLog, ActionStatus, Session as SessionModel
from app.services.orchestrator import orchestrator

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/actions", tags=["actions"])


class ActionResponse(BaseModel):
    id: str
    session_id: str
    action_type: str
    description: str
    command: str
    status: str
    threat_level: str
    requires_approval: bool
    result: dict
    error_message: str
    duration_ms: float
    created_at: datetime
    completed_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ApprovalRequest(BaseModel):
    approved: bool


@router.get("/{session_id}", response_model=List[ActionResponse])
async def get_actions(
    session_id: str,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    """Get action logs for a session."""
    sess = await db.execute(select(SessionModel).where(SessionModel.id == session_id))
    if not sess.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Session not found")

    result = await db.execute(
        select(ActionLog)
        .where(ActionLog.session_id == session_id)
        .order_by(desc(ActionLog.created_at))
        .limit(limit)
    )
    actions = result.scalars().all()
    return [
        ActionResponse(
            id=a.id,
            session_id=a.session_id,
            action_type=a.action_type,
            description=a.description,
            command=a.command,
            status=a.status.value,
            threat_level=a.threat_level.value,
            requires_approval=a.requires_approval,
            result=a.result or {},
            error_message=a.error_message or "",
            duration_ms=a.duration_ms or 0.0,
            created_at=a.created_at,
            completed_at=a.completed_at,
        )
        for a in actions
    ]


@router.post("/{action_id}/approve")
async def approve_action(
    action_id: str,
    body: ApprovalRequest,
    db: AsyncSession = Depends(get_db),
):
    """Approve or decline a pending action (Human-in-the-Loop gate)."""
    result = await db.execute(select(ActionLog).where(ActionLog.id == action_id))
    action = result.scalar_one_or_none()
    if not action:
        raise HTTPException(status_code=404, detail="Action not found")

    if action.status not in (ActionStatus.AWAITING_APPROVAL, ActionStatus.PENDING):
        raise HTTPException(
            status_code=400,
            detail=f"Action is in '{action.status.value}' state, cannot approve/decline"
        )

    resp = await orchestrator.approve_action(
        action_id=action_id,
        session_id=action.session_id,
        approved=body.approved,
        db=db,
    )
    if body.approved:
        import asyncio
        asyncio.create_task(orchestrator.execute_action(action_id, action.session_id, db))
    return resp


@router.get("/pending/{session_id}")
async def get_pending_approvals(
    session_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Get all actions awaiting human approval for a session."""
    result = await db.execute(
        select(ActionLog)
        .where(ActionLog.session_id == session_id)
        .where(ActionLog.status == ActionStatus.AWAITING_APPROVAL)
        .order_by(ActionLog.created_at)
    )
    actions = result.scalars().all()
    return [
        {
            "id": a.id,
            "action_type": a.action_type,
            "description": a.description,
            "command": a.command[:500],
            "threat_level": a.threat_level.value,
            "created_at": a.created_at.isoformat(),
        }
        for a in actions
    ]
