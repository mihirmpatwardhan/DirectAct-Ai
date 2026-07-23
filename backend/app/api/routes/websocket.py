"""
WebSocket Route — Phase 2–4: Full pipeline integration.
  - Real Gemini LLM streaming
  - Hybrid Task Router classification
  - Orchestrator → Security Guard → Action Log
  - Viewport screenshot streaming
  - Human-in-the-loop approval flow
"""
import json
import uuid
import logging
import asyncio
from datetime import datetime
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from app.core.websocket_manager import manager
from app.core.database import AsyncSessionLocal
from app.models.models import Message, MessageRole, Session as SessionModel
from app.services.llm_service import llm_service
from app.services.orchestrator import orchestrator
from app.services.task_router import task_router, TaskType

logger = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    """
    WebSocket endpoint — real-time, full-pipeline communication.
    """
    await manager.connect(websocket, session_id)
    logger.info(f"WS connected: session={session_id}")

    await manager.send_status(session_id, "connected", {
        "session_id": session_id,
        "message": "DirectAct-AI WebSocket connected",
        "llm_provider": llm_service.active_provider,
        "llm_ready": llm_service.is_configured,
    })

    try:
        while True:
            data = await websocket.receive_text()
            try:
                payload = json.loads(data)
                event_type = payload.get("type", "unknown")
                logger.debug(f"WS event: type={event_type}, session={session_id}")

                if event_type == "ping":
                    await manager.send_to_session(session_id, {"type": "pong"})

                elif event_type == "chat_message":
                    await _handle_chat_message(
                        session_id=session_id,
                        user_content=payload.get("content", ""),
                        target_engine=payload.get("target_engine"),
                    )

                elif event_type == "approval_response":
                    action_id = payload.get("action_id")
                    approved = payload.get("approved", False)
                    await _handle_approval(session_id, action_id, approved)

                elif event_type == "start_browser":
                    await _handle_start_browser(session_id)

                elif event_type == "close_browser":
                    await _handle_close_browser(session_id)

                else:
                    await manager.send_to_session(session_id, {
                        "type": "ack",
                        "received_type": event_type,
                    })

            except json.JSONDecodeError:
                await manager.send_error(session_id, "Invalid JSON payload")

    except WebSocketDisconnect:
        manager.disconnect(websocket, session_id)
        logger.info(f"WS disconnected: session={session_id}")


# ────────────────────────────────────────────────
# Event Handlers
# ────────────────────────────────────────────────

async def _handle_chat_message(session_id: str, user_content: str, target_engine: str = None) -> None:
    """
    Full pipeline:
      1. Persist user message
      2. Run orchestrator (route → security → action log → approval gate)
      3. Stream LLM response
      4. Persist assistant message
    """
    if not user_content.strip():
        return

    async with AsyncSessionLocal() as db:
        # Verify session
        result = await db.execute(select(SessionModel).where(SessionModel.id == session_id))
        session = result.scalar_one_or_none()
        if not session:
            await manager.send_error(session_id, f"Session {session_id} not found")
            return

        # 1. Persist user message
        user_msg = Message(
            id=str(uuid.uuid4()),
            session_id=session_id,
            role=MessageRole.USER,
            content=user_content,
            created_at=datetime.utcnow(),
        )
        db.add(user_msg)
        await db.flush()

        # 2. Orchestrator pipeline (routing + security + action log)
        orch_result = await orchestrator.process(
            user_input=user_content,
            session_id=session_id,
            db=db,
            target_engine=target_engine,
        )

        # Automatically execute if dispatched (no approval required)
        if orch_result.status == "dispatched" and orch_result.decision.task_type != TaskType.QUERY:
            asyncio.create_task(orchestrator.execute_action(orch_result.action_id, session_id, db))

        # 3. Load conversation history for LLM context
        history_result = await db.execute(
            select(Message)
            .where(Message.session_id == session_id)
            .where(Message.role != MessageRole.SYSTEM)
            .order_by(Message.created_at)
            .limit(20)
        )
        history_msgs = history_result.scalars().all()
        history = [
            {"role": m.role.value, "content": m.content}
            for m in history_msgs
            if m.id != user_msg.id
        ]

        # Build enriched prompt if orchestrator has context
        enriched_prompt = _build_enriched_prompt(user_content, orch_result)

        # 4. Stream LLM response
        msg_id = str(uuid.uuid4())
        full_response = ""
        start_time = datetime.utcnow()

        try:
            async for chunk in llm_service.stream_response(history, enriched_prompt):
                full_response += chunk
                await manager.send_text_chunk(session_id, chunk, msg_id)
        except Exception as e:
            logger.error(f"LLM streaming failed: {e}", exc_info=True)
            error_msg = f"\n\n⚠️ *Streaming error: {str(e)[:200]}*"
            full_response += error_msg
            await manager.send_text_chunk(session_id, error_msg, msg_id)
        finally:
            await manager.send_stream_end(session_id, msg_id)

        # 5. Persist assistant message
        duration_ms = (datetime.utcnow() - start_time).total_seconds() * 1000
        assistant_msg = Message(
            id=msg_id,
            session_id=session_id,
            role=MessageRole.ASSISTANT,
            content=full_response,
            created_at=datetime.utcnow(),
            tokens_used=len(full_response.split()),
        )
        db.add(assistant_msg)
        session.updated_at = datetime.utcnow()
        await db.commit()

        logger.info(
            f"Chat: session={session_id}, provider={llm_service.active_provider}, "
            f"task={orch_result.decision.task_type.value}, duration={duration_ms:.0f}ms"
        )


async def _handle_approval(session_id: str, action_id: str, approved: bool) -> None:
    """Process a human approval/decline for a pending action."""
    if not action_id:
        return
    async with AsyncSessionLocal() as db:
        result = await orchestrator.approve_action(
            action_id=action_id,
            session_id=session_id,
            approved=approved,
            db=db,
        )
        logger.info(f"Approval: action={action_id}, approved={approved} → {result}")

        if approved:
            # Trigger execution after user approval
            asyncio.create_task(orchestrator.execute_action(action_id, session_id, db))
            msg_id = str(uuid.uuid4())
            await manager.send_text_chunk(
                session_id,
                f"✅ Action approved. Executing engine task...",
                msg_id,
            )
            await manager.send_stream_end(session_id, msg_id)


async def _handle_start_browser(session_id: str) -> None:
    """Start a Playwright browser session for the given session."""
    try:
        from app.services.web_engine import web_engine
        success = await web_engine.start_session(session_id, headless=False)
        await manager.send_status(session_id, "browser_ready" if success else "browser_error", {
            "message": "Browser started" if success else "Failed to start browser",
        })
    except Exception as e:
        await manager.send_error(session_id, f"Browser start failed: {str(e)}")


async def _handle_close_browser(session_id: str) -> None:
    """Close the Playwright browser session."""
    try:
        from app.services.web_engine import web_engine
        await web_engine.close_session(session_id)
        await manager.send_status(session_id, "browser_closed", {})
    except Exception as e:
        await manager.send_error(session_id, f"Browser close failed: {str(e)}")


# ────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────

def _build_enriched_prompt(user_input: str, orch_result) -> str:
    """
    Enrich the user prompt with routing context so the LLM can give
    a more precise, action-aware response.
    """
    decision = orch_result.decision
    task_type = decision.task_type.value

    if task_type == "query":
        return user_input  # Pure query — pass through unchanged

    context_lines = [
        f"[SYSTEM CONTEXT — do not repeat this to user]",
        f"Task classified as: {task_type.upper()}",
        f"Intent: {decision.extracted_intent}",
        f"Confidence: {decision.confidence:.0%}",
        f"Orchestrator status: {orch_result.status}",
        f"Threat level: {orch_result.threat_level}",
    ]

    if orch_result.status == "blocked":
        context_lines.append(f"BLOCKED REASON: {orch_result.block_reason}")
        context_lines.append("Tell the user this action was blocked by the security guard and why.")
    elif orch_result.status == "awaiting_approval":
        context_lines.append("Tell the user this action requires approval before executing. It is shown in the Approvals panel on the right.")
    elif task_type == "web":
        url = decision.parameters.get("url", "")
        if url:
            context_lines.append(f"Target URL: {url}")
        context_lines.append("Briefly describe what you will do on the web. Mention the Viewport panel will show live progress.")
    elif task_type == "desktop":
        app = decision.parameters.get("app_name", "")
        if app:
            context_lines.append(f"Target app: {app}")
        context_lines.append("Briefly describe the desktop action you will take.")

    context = "\n".join(context_lines)
    return f"{user_input}\n\n---\n{context}"
