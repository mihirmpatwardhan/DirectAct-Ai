"""
WebSocket Route — Phase 2–4: Full pipeline integration.
  - Real Gemini LLM streaming
  - Hybrid Task Router classification
  - Orchestrator → Security Guard → Action Log
  - Viewport screenshot streaming
  - Human-in-the-loop approval flow
  - JWT authentication via ?token= query parameter
  - Per-user session ownership enforcement
"""
import json
import uuid
import random
import logging
import asyncio
from datetime import datetime
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from sqlalchemy import select
from app.core.websocket_manager import manager
from app.core.database import AsyncSessionLocal
from app.core.auth_utils import get_user_from_token_string
from app.models.models import Message, MessageRole, Session as SessionModel
from app.services.llm_service import llm_service, strip_planner_artifacts
from app.services.orchestrator import orchestrator
from app.services.task_router import task_router, TaskType

logger = logging.getLogger(__name__)

_greeting_locks: dict[str, asyncio.Lock] = {}
router = APIRouter()


@router.websocket("/ws/{session_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    session_id: str,
    token: str = Query(default=""),
):
    """
    WebSocket endpoint — real-time, full-pipeline communication.
    Requires a valid JWT token passed as ?token=<jwt> query parameter.
    Rejects connections for sessions not owned by the authenticated user.
    """
    # ── 1. Authenticate ──────────────────────────────────────────────────────
    async with AsyncSessionLocal() as auth_db:
        user = await get_user_from_token_string(token, auth_db)

    if user is None:
        # Close with policy-violation code so the client knows it's auth failure
        await websocket.close(code=4001, reason="Authentication required")
        logger.warning(f"WS rejected (no auth): session={session_id}")
        return

    user_id = str(user.id)

    # ── 2. Verify session ownership ──────────────────────────────────────────
    async with AsyncSessionLocal() as sess_db:
        result = await sess_db.execute(
            select(SessionModel).where(SessionModel.id == session_id)
        )
        session_record = result.scalar_one_or_none()

    if session_record is None:
        await websocket.close(code=4004, reason="Session not found")
        logger.warning(f"WS rejected (session not found): session={session_id}, user={user_id}")
        return

    if session_record.user_id != user_id:
        await websocket.close(code=4003, reason="Access denied")
        logger.warning(
            f"WS rejected (ownership mismatch): session={session_id}, "
            f"owner={session_record.user_id}, requester={user_id}"
        )
        return

    # ── 3. Accept & Run ──────────────────────────────────────────────────────
    await manager.connect(websocket, session_id)
    logger.info(f"WS connected: session={session_id}, user={user_id}")

    # Run the optional greeting in the background. This lets the receive loop
    # accept a command immediately after the socket opens instead of making
    # the first user message race the greeting handshake.
    greeting_task = asyncio.create_task(_send_greeting_if_empty(session_id))

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
                    # Preserve greeting history ordering, but never drop an
                    # early message sent by the local app during connect.
                    await greeting_task
                    await _handle_chat_message(
                        session_id=session_id,
                        user_content=payload.get("content", ""),
                        target_engine=payload.get("target_engine"),
                        user_id=user_id,
                    )

                elif event_type == "approval_response":
                    action_id = payload.get("action_id")
                    approved = payload.get("approved", False)
                    await _handle_approval(session_id, action_id, approved, user_id)

                elif event_type == "start_browser":
                    await _handle_start_browser(session_id)

                elif event_type == "close_browser":
                    await _handle_close_browser(session_id)

                elif event_type == "start_mcp_browser":
                    await _handle_start_mcp_browser(session_id)

                elif event_type == "setup_login":
                    await _handle_setup_login(session_id)

                elif event_type == "close_mcp_browser":
                    await _handle_close_mcp_browser(session_id)

                else:
                    await manager.send_to_session(session_id, {
                        "type": "ack",
                        "received_type": event_type,
                    })

            except json.JSONDecodeError:
                await manager.send_error(session_id, "Invalid JSON payload")
            except Exception as e:
                logger.error(f"Error handling WS message in session {session_id}: {e}", exc_info=True)
                await manager.send_error(session_id, f"Error processing message: {str(e)[:150]}")

    except (WebSocketDisconnect, RuntimeError):
        manager.disconnect(websocket, session_id)
        logger.info(f"WS disconnected: session={session_id}, user={user_id}")
    finally:
        # FIX: always cancel the greeting task to avoid orphaned coroutines
        if not greeting_task.done():
            greeting_task.cancel()
            try:
                await greeting_task
            except asyncio.CancelledError:
                pass


GREETING_MESSAGES = [
    "👋 **Hey there! I'm DirectAct-AI** — your AI copilot for web & desktop automation.\n\n"
    "I can help you with:\n"
    "- 🖥️ **Launch apps** — *\"Open Notepad and write a task list\"*\n"
    "- 🌐 **Browse the web** — *\"Go to YouTube and search for Python tutorials\"*\n"
    "- 📁 **Manage files** — *\"Create a folder called Projects on the Desktop\"*\n"
    "- 💬 **Answer anything** — Ask me any question, I'm not just an automation bot!\n\n"
    "What would you like to do today? 🚀",
]


async def _send_greeting_if_empty(session_id: str) -> None:
    """
    Stream a greeting assistant message if the session has no prior messages.
    Persists the greeting to the DB so it loads on reconnect.
    """
    lock = _greeting_locks.setdefault(session_id, asyncio.Lock())
    async with lock:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Message)
                .where(Message.session_id == session_id)
                .limit(1)
            )
            existing = result.scalar_one_or_none()
            if existing:
                return  # Already has messages — skip greeting

            greeting = random.choice(GREETING_MESSAGES)
            msg_id = str(uuid.uuid4())

            words = greeting.split(" ")
            for i in range(0, len(words), 4):
                chunk = " ".join(words[i:i + 4]) + " "
                await manager.send_text_chunk(session_id, chunk, msg_id)
                await asyncio.sleep(0.04)

            await manager.send_stream_end(session_id, msg_id)

            greeting_msg = Message(
                id=msg_id,
                session_id=session_id,
                role=MessageRole.ASSISTANT,
                content=greeting,
                created_at=datetime.utcnow(),
            )
            db.add(greeting_msg)
            await db.commit()


# ────────────────────────────────────────────────
# Event Handlers
# ────────────────────────────────────────────────

async def _handle_chat_message(
    session_id: str,
    user_content: str,
    target_engine: str = None,
    user_id: str = "",
) -> None:
    """
    Full pipeline:
      1. Persist user message
      2. Run orchestrator (route → security → action log → approval gate)
      3. Stream LLM response
      4. Persist assistant message
    Session ownership is already verified by the websocket_endpoint caller.
    """
    if not user_content.strip():
        return

    async with AsyncSessionLocal() as db:
        # Verify session (existence only — ownership already confirmed at connect time)
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
        try:
            orch_result = await orchestrator.process(
                user_input=user_content,
                session_id=session_id,
                db=db,
                target_engine=target_engine,
            )
        except Exception as orch_err:
            logger.error(f"Orchestrator pipeline error: {orch_err}", exc_info=True)
            msg_id = str(uuid.uuid4())
            error_msg = f"⚠️ An error occurred during task execution: {str(orch_err)[:200]}"
            await manager.send_text_chunk(session_id, error_msg, msg_id)
            await manager.send_stream_end(session_id, msg_id)
            return

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

        # 4. Stream a response. Automation tasks already executed through the
        # deterministic engines above, so do not make a second LLM call just
        # to narrate them. This keeps actions fast and works when API quota is
        # exhausted; only pure questions need the LLM.
        msg_id = str(uuid.uuid4())
        full_response = ""
        start_time = datetime.utcnow()

        if orch_result.task_type in {"web", "desktop"}:
            if orch_result.status == "awaiting_approval":
                full_response = "⏸️ This action is waiting for your approval in the right panel."
            elif orch_result.status == "blocked":
                full_response = f"🛡️ Action blocked: {orch_result.block_reason or 'security policy'}"
            elif orch_result.status == "failed":
                out = getattr(orch_result, "output", "")
                full_response = f"⚠️ Task could not be completed: {out}" if out else "⚠️ The action could not be completed. Check the Timeline for the exact error."
            else:
                out = getattr(orch_result, "output", "")
                if out:
                    full_response = out if out.startswith("✅") else f"✅ {out}"
                else:
                    full_response = "✅ Action executed. Check the Timeline for details."
            await manager.send_text_chunk(session_id, full_response, msg_id)
            await manager.send_stream_end(session_id, msg_id)
        else:
            try:
                sanitized_sent = ""
                async for chunk in llm_service.stream_response(history, enriched_prompt):
                    full_response += chunk
                    sanitized = strip_planner_artifacts(full_response)
                    if not sanitized:
                        continue
                    if sanitized.startswith(sanitized_sent):
                        delta = sanitized[len(sanitized_sent):]
                    else:
                        delta = sanitized
                    sanitized_sent = sanitized
                    if delta:
                        await manager.send_text_chunk(session_id, delta, msg_id)
                full_response = strip_planner_artifacts(full_response) or full_response
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


async def _handle_approval(
    session_id: str,
    action_id: str,
    approved: bool,
    user_id: str = "",
) -> None:
    """Process a human approval/decline for a pending action."""
    if not action_id:
        return
    async with AsyncSessionLocal() as db:
        # FIX: verify the action's session belongs to the user before approving
        from app.models.models import ActionLog
        action_result = await db.execute(
            select(ActionLog).where(ActionLog.id == action_id)
        )
        action = action_result.scalar_one_or_none()
        if not action:
            logger.warning(f"Approval: action {action_id} not found")
            return

        sess_result = await db.execute(
            select(SessionModel).where(SessionModel.id == action.session_id)
        )
        sess = sess_result.scalar_one_or_none()
        if not sess or sess.user_id != user_id:
            logger.warning(
                f"Approval rejected (ownership): action={action_id}, "
                f"owner={sess.user_id if sess else 'unknown'}, requester={user_id}"
            )
            await manager.send_error(session_id, "Access denied: cannot approve this action")
            return

        result = await orchestrator.approve_action(
            action_id=action_id,
            session_id=session_id,
            approved=approved,
            db=db,
        )
        logger.info(f"Approval: action={action_id}, approved={approved} → {result}")

        if approved:
            # Trigger execution after user approval.
            # NOTE: do NOT pass `db` — same closed-session bug as above (Bug #3 fix).
            asyncio.create_task(orchestrator.execute_action(action_id, session_id))
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


async def _handle_start_mcp_browser(session_id: str) -> None:
    """Start browser session using system Chrome profile."""
    try:
        from app.services.web_engine import web_engine
        await web_engine.start_session(session_id, headless=False)
        await manager.send_status(session_id, "browser_ready", {
            "message": "Chrome browser started with your saved profile",
        })
    except Exception as e:
        await manager.send_error(session_id, f"Browser start failed: {str(e)}")


async def _handle_setup_login(session_id: str) -> None:
    """Launch a headed browser for profile / login setup."""
    try:
        from app.services.web_engine import web_engine
        await web_engine.start_session(session_id, headless=False)
        await manager.send_status(session_id, "login_setup_started", {
            "status": "browser_launched",
            "message": "Chrome launched. You can log into any account or manage your profile.",
        })
    except Exception as e:
        await manager.send_error(session_id, f"Login setup failed: {str(e)}")


async def _handle_close_mcp_browser(session_id: str) -> None:
    """Close the browser session."""
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
            from app.services.app_discovery import app_discovery
            resolved = app_discovery.resolve(app)
            if resolved:
                context_lines.append(f"Target app: {resolved.name}")
                context_lines.append("INSTALLED: YES — verified on this Windows PC.")
                if resolved.app_user_model_id:
                    context_lines.append(
                        f"Launch method: Windows Start Menu / PWA (AppID: {resolved.app_user_model_id})"
                    )
                elif resolved.executable_path:
                    context_lines.append(f"Executable: {resolved.executable_path}")
                context_lines.append(
                    "IMPORTANT: The system is launching this app now. "
                    "Tell the user it is opening successfully. "
                    "Do NOT say the app is missing, not installed, or unavailable."
                )
            else:
                context_lines.append(f"Target app: {app}")
                context_lines.append(
                    "Attempting launch via Windows Start Menu lookup. "
                    "Do not claim the app is missing before trying."
                )
        context_lines.append("Briefly describe the desktop action you will take.")

    context = "\n".join(context_lines)
    return f"{user_input}\n\n---\n{context}"
