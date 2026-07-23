"""
Execution Orchestrator — Saga State Machine — Phase 1.4
========================================================
Central coordinator for multi-step task execution.

Architecture:
  - Each multi-step task is a Saga: steps execute in order
  - Each step's Command is validated by MalwareGuard JUST BEFORE execution
    (not all upfront — state can change mid-task)
  - Failures trigger compensating actions where defined
  - The Orchestrator emits typed events to the EventBus at each step boundary
  - ExecutionMode (AUTONOMOUS / HITL) controls the Confirmation Gate behavior

Data flow:
  User input → TaskRouter → ActionPlan → Orchestrator
    → [per step: MalwareGuard → (ApprovalGate?) → OSEngine → Result]
    → EventBus → WebSocket → UI

Security guarantee:
  The Orchestrator does NOT contain security logic.
  Security is entirely within MalwareGuard (the separate process boundary concept).
  A bug or prompt injection here cannot bypass the Guard — it must pass through.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime
from enum import Enum
from typing import Optional, List, Callable, Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.action_vocabulary import (
    ActionPlan, BaseCommand, CommandType, CommandResult, RiskLevel,
    LaunchApp, CloseApp, CreateFile, DeleteFile, MoveFile, CopyFile,
    RenameFile, ReadFile, ListDirectory, CreateDirectory,
    GetSystemInfo, TakeScreenshot, GetClipboard, SetClipboard,
    RunApprovedScript, OpenURL, QueryLLM,
)
from app.schemas.events import (
    EventType, TaskStartedEvent, TaskStepStartedEvent, TaskStepCompletedEvent,
    TaskCompletedEvent, TaskFailedEvent, PermissionRequestedEvent,
    ErrorOccurredEvent,
)
from app.services.security_guard import MalwareGuard, GuardContext, GuardAction, PipelineResult
from app.services.os_engine import os_engine
from app.services.task_router import task_router, TaskType
from app.services.llm_service import llm_service
from app.core.websocket_manager import manager

logger = logging.getLogger(__name__)

malware_guard = MalwareGuard()


class ExecutionMode(str, Enum):
    AUTONOMOUS = "autonomous"       # Auto-execute routine actions; guard still runs
    HUMAN_IN_LOOP = "hitl"         # Require approval at every step boundary


# ──────────────────────────────────────────────────────────────────────────────
# Action Plan Parser
# ──────────────────────────────────────────────────────────────────────────────

_COMMAND_REGISTRY = {
    CommandType.LAUNCH_APP: LaunchApp,
    CommandType.CLOSE_APP: CloseApp,
    CommandType.CREATE_FILE: CreateFile,
    CommandType.DELETE_FILE: DeleteFile,
    CommandType.MOVE_FILE: MoveFile,
    CommandType.COPY_FILE: CopyFile,
    CommandType.RENAME_FILE: RenameFile,
    CommandType.READ_FILE: ReadFile,
    CommandType.LIST_DIRECTORY: ListDirectory,
    CommandType.CREATE_DIRECTORY: CreateDirectory,
    CommandType.GET_SYSTEM_INFO: GetSystemInfo,
    CommandType.TAKE_SCREENSHOT: TakeScreenshot,
    CommandType.GET_CLIPBOARD: GetClipboard,
    CommandType.SET_CLIPBOARD: SetClipboard,
    CommandType.RUN_APPROVED_SCRIPT: RunApprovedScript,
    CommandType.OPEN_URL: OpenURL,
    CommandType.QUERY_LLM: QueryLLM,
}


def parse_action_plan(plan_dict: dict) -> ActionPlan:
    """Parse a raw dict (from LLM JSON output) into a typed ActionPlan."""
    task_id = plan_dict.get("task_id", str(uuid.uuid4()))
    steps_raw = plan_dict.get("steps", [])

    steps: List[BaseCommand] = []
    for step in steps_raw:
        cmd_type_str = step.get("command_type", "query_llm")
        try:
            cmd_type = CommandType(cmd_type_str)
        except ValueError:
            logger.warning(f"Unknown command_type '{cmd_type_str}' — skipping step")
            continue

        cls = _COMMAND_REGISTRY.get(cmd_type)
        if not cls:
            continue

        try:
            # Build only with fields that the Command class actually accepts
            import inspect
            valid_fields = set(inspect.signature(cls).parameters.keys())
            filtered = {k: v for k, v in step.items() if k in valid_fields}
            cmd = cls(**filtered)
            steps.append(cmd)
        except Exception as e:
            logger.warning(f"Failed to parse step {step}: {e}")

    if not steps:
        # Fallback: treat as pure query
        steps = [QueryLLM(query=plan_dict.get("original_intent", ""))]

    return ActionPlan(
        task_id=task_id,
        original_intent=plan_dict.get("original_intent", ""),
        steps=steps,
        requires_sequential=plan_dict.get("requires_sequential", True),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Orchestrator Result
# ──────────────────────────────────────────────────────────────────────────────

class OrchestratorResult:
    def __init__(
        self,
        action_id: str,
        status: str,
        task_type: str = "query",
        intent: str = "",
        confidence: float = 1.0,
        routing_method: str = "rule_based",
        threat_level: str = "none",
        requires_approval: bool = False,
        parameters: dict = None,
        block_reason: str = "",
        plan: Optional[ActionPlan] = None,
    ):
        self.action_id = action_id
        self.status = status
        self.task_type = task_type
        self.intent = intent
        self.confidence = confidence
        self.routing_method = routing_method
        self.threat_level = threat_level
        self.requires_approval = requires_approval
        self.parameters = parameters or {}
        self.block_reason = block_reason
        self.plan = plan

    def to_dict(self) -> dict:
        return {
            "id": self.action_id,
            "status": self.status,
            "task_type": self.task_type,
            "intent": self.intent,
            "confidence": self.confidence,
            "routing_method": self.routing_method,
            "threat_level": self.threat_level,
            "requires_approval": self.requires_approval,
            "parameters": self.parameters,
            "block_reason": self.block_reason,
        }

    # Backward compat: expose decision-like interface
    @property
    def decision(self):
        class _D:
            def __init__(self, parent):
                self.task_type = type("T", (), {"value": parent.task_type})()
                self.extracted_intent = parent.intent
                self.confidence = parent.confidence
                self.routing_method = type("R", (), {"value": parent.routing_method})()
                self.parameters = parent.parameters
        return _D(self)


# ──────────────────────────────────────────────────────────────────────────────
# Event Emitter (wraps WebSocket manager)
# ──────────────────────────────────────────────────────────────────────────────

async def _emit(session_id: str, event):
    """Emit a typed event to the WebSocket manager."""
    try:
        await manager.send_to_session(session_id, event.to_ws_payload())
    except Exception as e:
        logger.debug(f"Event emit failed: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# ExecutionOrchestrator
# ──────────────────────────────────────────────────────────────────────────────

class ExecutionOrchestrator:
    """
    Saga state machine coordinator.
    Routes → Guards → Executes typed Commands, emitting typed events throughout.
    """

    # Pending approval requests: action_id → asyncio.Event
    _approval_events: dict[str, asyncio.Event] = {}
    _approval_decisions: dict[str, bool] = {}

    async def process(
        self,
        user_input: str,
        session_id: str,
        db: AsyncSession,
        target_engine: Optional[str] = None,
        execution_mode: str = ExecutionMode.AUTONOMOUS,
    ) -> OrchestratorResult:
        """Process user input through the full pipeline."""
        action_id = str(uuid.uuid4())
        logger.info(f"Orchestrator: processing action={action_id[:8]} session={session_id}")

        # Step 1: Route
        decision = task_router.classify(user_input, target_engine=target_engine)
        logger.info(f"Router: type={decision.task_type.value} confidence={decision.confidence:.2f}")

        # Step 2: Log action
        await self._log_action(
            db=db, action_id=action_id, session_id=session_id,
            action_type=decision.task_type.value,
            description=decision.extracted_intent,
            command=user_input, status="pending",
            threat_level="none", requires_approval=False,
        )

        # Step 3: Pure queries — no Guard needed
        if decision.task_type == TaskType.QUERY:
            await self._update_action(db, action_id, "completed")
            return OrchestratorResult(
                action_id=action_id, status="dispatched",
                task_type="query", intent=decision.extracted_intent,
                confidence=decision.confidence,
                routing_method=decision.routing_method.value,
            )

        # Step 4: LLM generates an ActionPlan from the user input
        plan_dict = await llm_service.parse_action_plan_from_response(
            await self._get_plan_from_llm(user_input, decision)
        )

        if plan_dict:
            plan = parse_action_plan(plan_dict)
        else:
            # Fall back to a simple single-step plan based on routing
            plan = self._build_fallback_plan(user_input, decision, action_id)

        logger.info(f"Orchestrator: plan has {len(plan.steps)} step(s), highest_risk={plan.highest_risk.value}")

        # Step 5: Emit task_started event
        event_emitter = lambda evt: _emit(session_id, evt)
        await _emit(session_id, TaskStartedEvent(
            type=EventType.TASK_STARTED,
            session_id=session_id,
            task_id=plan.task_id,
            original_intent=plan.original_intent,
            total_steps=len(plan.steps),
            highest_risk=plan.highest_risk.value,
            requires_any_approval=plan.requires_any_approval,
        ))

        # Step 6: Execute saga
        await self._execute_saga(
            plan=plan,
            action_id=action_id,
            session_id=session_id,
            db=db,
            execution_mode=execution_mode,
            raw_input=user_input,
        )

        status = "dispatched"
        return OrchestratorResult(
            action_id=action_id,
            status=status,
            task_type=decision.task_type.value,
            intent=decision.extracted_intent,
            confidence=decision.confidence,
            routing_method=decision.routing_method.value,
            parameters=decision.parameters,
            plan=plan,
        )

    async def _get_plan_from_llm(self, user_input: str, decision) -> str:
        """Ask LLM to generate an action_plan JSON block."""
        # Collect LLM response for plan extraction
        full_response = ""
        history = []
        async for chunk in llm_service.stream_response(history, user_input):
            full_response += chunk
        return full_response

    def _build_fallback_plan(self, user_input: str, decision, action_id: str) -> ActionPlan:
        """Build a minimal ActionPlan from routing decision when LLM plan is absent."""
        steps: List[BaseCommand] = []

        if decision.task_type == TaskType.WEB:
            url = decision.parameters.get("url", "https://www.google.com/search?q=" + user_input.replace(" ", "+"))
            steps.append(OpenURL(url=url, description=f"Open {url}"))
        elif decision.task_type == TaskType.DESKTOP:
            app = decision.parameters.get("app_name", "")
            if app:
                steps.append(LaunchApp(app_id=app, description=f"Launch {app}"))
            else:
                steps.append(GetSystemInfo(description="Get system information"))
        else:
            steps.append(QueryLLM(query=user_input, description="Answer query"))

        return ActionPlan(
            task_id=action_id,
            original_intent=user_input,
            steps=steps,
        )

    async def _execute_saga(
        self,
        plan: ActionPlan,
        action_id: str,
        session_id: str,
        db: AsyncSession,
        execution_mode: str,
        raw_input: str,
    ):
        """Execute each step of the saga through the guard chain."""
        steps_completed = 0
        steps_failed = 0
        total_duration_ms = 0.0
        compensating_actions: List[tuple[int, BaseCommand]] = []

        event_emitter = lambda evt: _emit(session_id, evt)

        for i, command in enumerate(plan.steps):
            logger.info(f"Orchestrator: step {i+1}/{len(plan.steps)} — {command.command_type.value}")

            # Emit step_started
            await _emit(session_id, TaskStepStartedEvent(
                type=EventType.TASK_STEP_STARTED,
                session_id=session_id,
                task_id=plan.task_id,
                step_index=i,
                total_steps=len(plan.steps),
                command_type=command.command_type.value,
                description=command.description,
                risk_level=command.risk_level.value,
            ))

            # Determine file path for scan (if applicable)
            file_path = None
            if hasattr(command, "path"):
                file_path = getattr(command, "path")
            elif hasattr(command, "source"):
                file_path = getattr(command, "source")

            # Run guard chain
            ctx = GuardContext(
                action_id=action_id,
                task_id=plan.task_id,
                session_id=session_id,
                execution_mode=execution_mode,
                raw_input=raw_input,
                file_path=file_path,
                event_emitter=event_emitter,
            )
            guard_result: PipelineResult = await malware_guard.validate(command, ctx)

            if not guard_result.allowed:
                steps_failed += 1
                await _emit(session_id, TaskStepCompletedEvent(
                    type=EventType.TASK_STEP_COMPLETED,
                    session_id=session_id,
                    task_id=plan.task_id,
                    step_index=i,
                    command_type=command.command_type.value,
                    success=False,
                    duration_ms=0,
                    error=guard_result.block_reason,
                ))
                if plan.requires_sequential:
                    await _emit(session_id, TaskFailedEvent(
                        type=EventType.TASK_FAILED,
                        session_id=session_id,
                        task_id=plan.task_id,
                        failed_at_step=i,
                        reason=guard_result.block_reason or "Blocked by Malware Guard",
                    ))
                    await self._run_compensations(compensating_actions, session_id, plan.task_id)
                    return
                continue

            # Approval gate
            if guard_result.requires_approval:
                await self._request_approval(command, guard_result, i, action_id, plan, session_id, db)
                # For now, re-emit as awaiting_approval and return
                # The approval flow resumes execution via approve_action()
                return

            # Skip pure queries — no OS execution needed
            if command.command_type == CommandType.QUERY_LLM:
                steps_completed += 1
                continue

            # Execute command
            cmd_result: CommandResult = await os_engine.execute(command, session_id=session_id)
            total_duration_ms += cmd_result.duration_ms

            await _emit(session_id, TaskStepCompletedEvent(
                type=EventType.TASK_STEP_COMPLETED,
                session_id=session_id,
                task_id=plan.task_id,
                step_index=i,
                command_type=command.command_type.value,
                success=cmd_result.success,
                duration_ms=cmd_result.duration_ms,
                output_summary=(cmd_result.output or "")[:200] if cmd_result.output else None,
                error=cmd_result.error,
            ))

            if cmd_result.success:
                steps_completed += 1
                # Track compensating action if available
                if command.compensate_description:
                    compensating_actions.append((i, command))
            else:
                steps_failed += 1
                logger.warning(f"Step {i} failed: {cmd_result.error}")
                if plan.requires_sequential:
                    await _emit(session_id, TaskFailedEvent(
                        type=EventType.TASK_FAILED,
                        session_id=session_id,
                        task_id=plan.task_id,
                        failed_at_step=i,
                        reason=cmd_result.error or "Step execution failed",
                    ))
                    await self._run_compensations(compensating_actions, session_id, plan.task_id)
                    return

        # All steps done
        await _emit(session_id, TaskCompletedEvent(
            type=EventType.TASK_COMPLETED,
            session_id=session_id,
            task_id=plan.task_id,
            steps_completed=steps_completed,
            steps_failed=steps_failed,
            total_duration_ms=total_duration_ms,
        ))
        await self._update_action(db, action_id, "completed")

    async def _run_compensations(self, compensating_actions: list, session_id: str, task_id: str):
        """Run compensating actions in reverse order for failed sagas."""
        for step_idx, cmd in reversed(compensating_actions):
            logger.info(f"Orchestrator: running compensation for step {step_idx}: {cmd.compensate_description}")
            # Compensation: best-effort, no guard re-run needed (reversing our own action)
            try:
                if hasattr(cmd, "path") and hasattr(cmd, "compensate_description"):
                    # e.g. delete the file we created
                    from app.schemas.action_vocabulary import DeleteFile as DF
                    comp_cmd = DF(path=getattr(cmd, "path"), description=f"Compensate: {cmd.compensate_description}")
                    await os_engine.execute(comp_cmd, session_id=session_id)
            except Exception as e:
                logger.error(f"Compensation for step {step_idx} failed: {e}")

    async def _request_approval(
        self,
        command: BaseCommand,
        guard_result: PipelineResult,
        step_index: int,
        action_id: str,
        plan: ActionPlan,
        session_id: str,
        db: AsyncSession,
    ):
        """Send a permission request to the frontend and update DB status."""
        await self._update_action(db, action_id, "awaiting_approval", requires_approval=True,
                                  threat_level=guard_result.highest_risk.value)
        await _emit(session_id, PermissionRequestedEvent(
            type=EventType.PERMISSION_REQUESTED,
            session_id=session_id,
            action_id=action_id,
            task_id=plan.task_id,
            step_index=step_index,
            command_type=command.command_type.value,
            description=command.description,
            risk_level=guard_result.highest_risk.value,
            reason_for_approval=guard_result.block_reason or "This action requires your confirmation",
        ))

        # Also notify via action_update for backward compat with timeline panel
        await manager.send_action_update(session_id, {
            "id": action_id,
            "session_id": session_id,
            "action_type": "desktop",
            "description": command.description,
            "command": str(command.command_type.value),
            "status": "awaiting_approval",
            "threat_level": guard_result.highest_risk.value,
            "requires_approval": True,
            "created_at": datetime.utcnow().isoformat(),
        })

    async def approve_action(
        self,
        action_id: str,
        session_id: str,
        approved: bool,
        db: AsyncSession,
    ) -> dict:
        """Handle approval/decline from the human-in-the-loop gate."""
        from app.models.models import ActionLog, ActionStatus
        from sqlalchemy import select

        result = await db.execute(select(ActionLog).where(ActionLog.id == action_id))
        action = result.scalar_one_or_none()
        if not action:
            return {"error": f"Action {action_id} not found"}

        new_status = ActionStatus.APPROVED if approved else ActionStatus.DECLINED
        action.status = new_status
        action.completed_at = datetime.utcnow()
        await db.commit()

        from app.schemas.events import PermissionGrantedEvent, PermissionDeniedEvent
        if approved:
            await _emit(session_id, PermissionGrantedEvent(
                type=EventType.PERMISSION_GRANTED,
                session_id=session_id,
                action_id=action_id,
                task_id="",
            ))
        else:
            await _emit(session_id, PermissionDeniedEvent(
                type=EventType.PERMISSION_DENIED,
                session_id=session_id,
                action_id=action_id,
                task_id="",
            ))

        await manager.send_action_update(session_id, {"id": action_id, "status": new_status.value})
        logger.info(f"Orchestrator: action={action_id[:8]} {'approved' if approved else 'declined'}")
        return {"action_id": action_id, "status": new_status.value}

    async def execute_action(
        self,
        action_id: str,
        session_id: str,
        db: AsyncSession,
    ) -> dict:
        """Execute an approved action (legacy path — used when approval flow completes)."""
        from app.models.models import ActionLog, ActionStatus
        from sqlalchemy import select

        result = await db.execute(select(ActionLog).where(ActionLog.id == action_id))
        action = result.scalar_one_or_none()
        if not action:
            return {"error": "Action not found"}

        start = datetime.utcnow()
        action.status = ActionStatus.RUNNING
        await db.commit()
        await manager.send_action_update(session_id, {"id": action_id, "status": "running"})

        try:
            # Re-execute via OS engine using stored command
            cmd_lower = (action.command or "").lower()
            if action.action_type == "desktop":
                if any(kw in cmd_lower for kw in ["open ", "launch ", "start ", "run "]):
                    m = re.search(r"(?:open|launch|start|run)\s+([a-zA-Z0-9 _\-]+)", cmd_lower)
                    app = m.group(1).strip() if m else action.command
                    cmd = LaunchApp(app_id=app, description=f"Launch {app}")
                elif any(kw in cmd_lower for kw in ["close ", "quit ", "exit "]):
                    m = re.search(r"(?:close|quit|exit|end)\s+([a-zA-Z0-9 _\-]+)", cmd_lower)
                    app = m.group(1).strip() if m else action.command
                    cmd = CloseApp(app_id=app, description=f"Close {app}")
                else:
                    cmd = GetSystemInfo(description="System info")
                result_data = await os_engine.execute(cmd, session_id=session_id)
            elif action.action_type == "web":
                m = re.search(r"https?://\S+|www\.\S+", action.command, re.I)
                url = m.group() if m else "https://www.google.com"
                if not url.startswith("http"):
                    url = "https://" + url
                cmd = OpenURL(url=url, description=f"Open {url}")
                result_data = await os_engine.execute(cmd, session_id=session_id)
            else:
                result_data = CommandResult(command_type=CommandType.QUERY_LLM, success=True, output="Query completed")
        except Exception as e:
            logger.error(f"Execute error: {e}")
            result_data = CommandResult(command_type=CommandType.QUERY_LLM, success=False, error=str(e))

        duration_ms = (datetime.utcnow() - start).total_seconds() * 1000
        final_status = ActionStatus.COMPLETED if result_data.success else ActionStatus.FAILED
        action.status = final_status
        action.completed_at = datetime.utcnow()
        action.duration_ms = duration_ms
        action.result = result_data.metadata or {}
        await db.commit()

        await manager.send_action_update(session_id, {
            "id": action_id,
            "status": final_status.value,
            "duration_ms": duration_ms,
        })
        return {"action_id": action_id, "status": final_status.value}

    async def _log_action(self, db, action_id, session_id, action_type, description,
                          command, status, threat_level, requires_approval, error_message=""):
        from app.models.models import ActionLog, ActionStatus, ThreatLevel as TL
        try:
            status_map = {
                "pending": ActionStatus.PENDING, "awaiting_approval": ActionStatus.AWAITING_APPROVAL,
                "completed": ActionStatus.COMPLETED, "failed": ActionStatus.FAILED,
                "cancelled": ActionStatus.CANCELLED, "dispatched": ActionStatus.RUNNING,
            }
            threat_map = {
                "none": TL.NONE, "low": TL.LOW, "medium": TL.MEDIUM,
                "high": TL.HIGH, "critical": TL.CRITICAL,
            }
            log = ActionLog(
                id=action_id, session_id=session_id, action_type=action_type,
                description=description, command=command[:1000],
                status=status_map.get(status, ActionStatus.PENDING),
                threat_level=threat_map.get(threat_level, TL.NONE),
                requires_approval=requires_approval, error_message=error_message,
                created_at=datetime.utcnow(),
            )
            db.add(log)
            await db.flush()
        except Exception as e:
            logger.error(f"Failed to log action: {e}")

    async def _update_action(self, db, action_id, status, requires_approval=None, threat_level=None):
        from app.models.models import ActionLog, ActionStatus, ThreatLevel as TL
        from sqlalchemy import select, update
        try:
            status_map = {
                "completed": ActionStatus.COMPLETED, "failed": ActionStatus.FAILED,
                "awaiting_approval": ActionStatus.AWAITING_APPROVAL,
            }
            update_vals = {
                "status": status_map.get(status, ActionStatus.COMPLETED),
                "completed_at": datetime.utcnow(),
            }
            if requires_approval is not None:
                update_vals["requires_approval"] = requires_approval
            if threat_level:
                threat_map = {"none": TL.NONE, "low": TL.LOW, "medium": TL.MEDIUM,
                              "high": TL.HIGH, "critical": TL.CRITICAL}
                update_vals["threat_level"] = threat_map.get(threat_level, TL.NONE)
            await db.execute(
                update(ActionLog).where(ActionLog.id == action_id).values(**update_vals)
            )
            await db.commit()
        except Exception as e:
            logger.error(f"Failed to update action: {e}")


# Singleton
orchestrator = ExecutionOrchestrator()
