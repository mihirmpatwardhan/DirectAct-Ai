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


def _is_interactive_desktop_task(text: str) -> bool:
    """Whether a request needs GUI controls, without naming an app."""
    return bool(re.search(
        r"\b(click|double[- ]?click|type|write|enter|fill|press|select|choose|paste|save|search|check|uncheck|play|pause|resume|skip|listen|create|edit|send|reply|upload|download)\b|\b(and|then)\b",
        text or "",
        re.IGNORECASE,
    ))


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

            # Auto-recover open_url steps where LLM forgot to include the url field
            if cmd_type == CommandType.OPEN_URL and "url" not in filtered:
                desc = step.get("description", "")
                # Try to extract URL from description
                import re as _re
                url_match = _re.search(r'https?://[^\s"]+', desc)
                if url_match:
                    filtered["url"] = url_match.group(0)
                else:
                    # Keep the browser on a neutral page. The agent decides
                    # where to go from the complete task instead of being
                    # coupled to a particular search provider.
                    filtered["url"] = "about:blank"
                logger.info(f"Auto-recovered open_url: url={filtered['url']}")

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
        output: str = "",
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
        self.output = output

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
            "output": self.output,
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
    # Keep the guarded plan so approval resumes the same compound task.
    _pending_sagas: dict[str, tuple[ActionPlan, str, int, str]] = {}

    async def process(
        self,
        user_input: str,
        session_id: str,
        db: AsyncSession,
        target_engine: Optional[str] = None,
        execution_mode: str = ExecutionMode.AUTONOMOUS,
    ) -> OrchestratorResult:
        """Process user input through the full pipeline."""
        import time as _time
        process_t0 = _time.perf_counter()
        action_id = str(uuid.uuid4())
        logger.info(f"Orchestrator: processing action={action_id[:8]} session={session_id}")

        # Step 1: Route
        decision = task_router.classify(user_input, target_engine=target_engine)
        if decision.task_type == TaskType.AMBIGUOUS and target_engine not in {"web", "desktop"}:
            decision = await task_router.classify_with_llm(user_input)
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

        # Step 4: Interactive desktop work is a single guarded saga step whose
        # executor runs the UIA agent. Keeping the raw intent on the step is
        # important: reducing "open Notepad and type ..." to LaunchApp would
        # silently drop everything after the launch.
        #
        # Step 4: Desktop automation tasks route to the UIA agent loop,
        # passing the raw user intent directly to the LLM agent without
        # hardcoding or loss of scope.
        plan_t0 = _time.perf_counter()
        if decision.task_type == TaskType.WEB or target_engine == "web":
            return await self._execute_web_task(
                user_input=user_input,
                decision=decision,
                action_id=action_id,
                session_id=session_id,
                db=db,
                execution_mode=execution_mode,
            )
        elif decision.task_type == TaskType.DESKTOP or target_engine == "desktop":
            app_target = decision.parameters.get("app_name") or ""
            plan = ActionPlan(
                task_id=action_id,
                original_intent=user_input,
                steps=[LaunchApp(
                    app_id=app_target or user_input,
                    description=f"Control desktop: {user_input}",
                    requires_approval=decision.requires_approval,
                )],
            )
            plan_elapsed = (_time.perf_counter() - plan_t0) * 1000
            logger.info("Orchestrator: routing desktop task to agent loop with raw intent")
        else:
            plan_dict = await llm_service.parse_action_plan_from_response(
                await self._get_plan_from_llm(user_input, decision)
            )
            plan_elapsed = (_time.perf_counter() - plan_t0) * 1000
            logger.info(f"⏱️  LLM planning completed in {plan_elapsed:.0f}ms")

            if plan_dict:
                plan = parse_action_plan(plan_dict)
            else:
                plan = self._build_fallback_plan(user_input, decision, action_id)

        logger.info(f"Orchestrator: plan has {len(plan.steps)} step(s), highest_risk={plan.highest_risk.value}")

        # Persist a JSON copy as a restart-safe recovery record. The in-memory
        # entry is faster, while the DB record prevents approval from falling
        # back to regex-based command reconstruction after a process restart.
        await self._update_action(
            db, action_id, "pending",
            result={
                "pending_plan": plan.model_dump(mode="json"),
                "raw_input": user_input,
                "next_step": 0,
                "execution_mode": execution_mode,
            },
        )

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
        execution_status = await self._execute_saga(
            plan=plan,
            action_id=action_id,
            session_id=session_id,
            db=db,
            execution_mode=execution_mode,
            raw_input=user_input,
        )
        total_elapsed = (_time.perf_counter() - process_t0) * 1000
        logger.info(f"⏱️  Orchestrator pipeline total: {total_elapsed:.0f}ms (planning={plan_elapsed:.0f}ms)")

        status = execution_status
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

    async def _execute_web_task(
        self,
        user_input: str,
        decision,
        action_id: str,
        session_id: str,
        db: AsyncSession,
        execution_mode: str = ExecutionMode.AUTONOMOUS,
    ) -> OrchestratorResult:
        """
        Directly execute a web automation task via the agentic web_engine loop.
        Bypasses os_engine and fallback plan machinery so complex web tasks
        (movie ticket booking, travel planning, form filling) interact with live Chrome.
        """
        import time as _time
        t0 = _time.perf_counter()
        logger.info(f"Orchestrator: routing web task {action_id[:8]} directly to web_engine")

        url = decision.parameters.get("url") or ""
        raw_task = decision.parameters.get("raw_task") or user_input

        # 1. Security inspection via MalwareGuard
        # Do not substitute a vendor URL when the user did not provide one.
        # The generic browser agent will choose its first navigation from the
        # task and current page state after this neutral guard check.
        check_cmd = OpenURL(url=url or "about:blank", description=user_input)
        event_emitter = lambda evt: _emit(session_id, evt)
        ctx = GuardContext(
            action_id=action_id,
            task_id=action_id,
            session_id=session_id,
            execution_mode=execution_mode,
            raw_input=user_input,
            event_emitter=event_emitter,
        )
        guard_result: PipelineResult = await malware_guard.validate(check_cmd, ctx)
        if not guard_result.allowed:
            await _emit(session_id, TaskFailedEvent(
                type=EventType.TASK_FAILED,
                session_id=session_id,
                task_id=action_id,
                failed_at_step=0,
                reason=guard_result.block_reason or "Blocked by Malware Guard",
            ))
            await self._update_action(
                db, action_id, "failed",
                threat_level=guard_result.highest_risk.value,
                error_message=guard_result.block_reason or "Blocked by Malware Guard",
            )
            return OrchestratorResult(
                action_id=action_id,
                status="blocked",
                task_type="web",
                intent=decision.extracted_intent,
                confidence=decision.confidence,
                routing_method=decision.routing_method.value,
                threat_level=guard_result.highest_risk.value,
                block_reason=guard_result.block_reason,
                parameters=decision.parameters,
                output=f"Action blocked by Malware Guard: {guard_result.block_reason}",
            )

        # 2. Emit TaskStartedEvent to Timeline
        await _emit(session_id, TaskStartedEvent(
            type=EventType.TASK_STARTED,
            session_id=session_id,
            task_id=action_id,
            original_intent=user_input,
            total_steps=20,
            highest_risk=guard_result.highest_risk.value if hasattr(guard_result, "highest_risk") else "low",
            requires_any_approval=False,
        ))
        await self._update_action(db, action_id, "running")

        # 3. Call web_engine.run_task directly
        from app.services.web_engine import web_engine
        try:
            web_result = await web_engine.run_task(
                task_query=user_input,
                url=url,
                session_id=session_id,
                task_id=action_id,
            )
        except Exception as e:
            logger.error(f"Web engine execution error: {e}", exc_info=True)
            web_result = {"success": False, "error": str(e), "steps": 0}

        elapsed = (_time.perf_counter() - t0) * 1000
        success = bool(web_result.get("success", False))
        steps = web_result.get("steps", 1)
        output = web_result.get("output", "")
        error_msg = web_result.get("error", "")

        if success:
            await _emit(session_id, TaskCompletedEvent(
                type=EventType.TASK_COMPLETED,
                session_id=session_id,
                task_id=action_id,
                steps_completed=steps,
                steps_failed=0,
                total_duration_ms=elapsed,
            ))
            await self._update_action(
                db, action_id, "completed",
                result={"output": output, "steps": steps, "duration_ms": elapsed},
            )
            return OrchestratorResult(
                action_id=action_id,
                status="completed",
                task_type="web",
                intent=decision.extracted_intent,
                confidence=decision.confidence,
                routing_method=decision.routing_method.value,
                parameters=decision.parameters,
                output=output or f"Successfully executed web task across {steps} step(s).",
            )
        else:
            fail_reason = error_msg or "Web task could not be completed"
            await _emit(session_id, TaskFailedEvent(
                type=EventType.TASK_FAILED,
                session_id=session_id,
                task_id=action_id,
                failed_at_step=steps,
                reason=fail_reason,
            ))
            await self._update_action(
                db, action_id, "failed",
                error_message=fail_reason,
                result={"error": fail_reason, "steps": steps, "duration_ms": elapsed},
            )
            return OrchestratorResult(
                action_id=action_id,
                status="failed",
                task_type="web",
                intent=decision.extracted_intent,
                confidence=decision.confidence,
                routing_method=decision.routing_method.value,
                parameters=decision.parameters,
                output=fail_reason,
            )

    async def _get_plan_from_llm(self, user_input: str, decision) -> str:
        """
        Ask the LLM to produce a structured action_plan JSON block.
        Uses a dedicated system prompt so the output is compact JSON,
        not prose, which avoids wasting tokens before the user-visible
        stream starts (Bug #4 fix).
        """
        system_prompt = (
            "You are an action planner. Given the user's request and its classification, "
            "respond ONLY with a valid JSON object (no prose, no markdown fences, no code blocks).\n"
            "Schema:\n"
            '{"task_id":"<uuid>","original_intent":"<str>","steps":[<step>, ...],"requires_sequential":true}\n'
            "Step schemas by command_type:\n"
            '  open_url:     {"command_type":"open_url","url":"https://...","description":"<str>","risk_level":"none"}\n'
            '  launch_app:   {"command_type":"launch_app","app_id":"<name>","description":"<str>","risk_level":"none"}\n'
            '  create_file:  {"command_type":"create_file","path":"<path>","content":"<str>","description":"<str>","risk_level":"low"}\n'
            '  query_llm:    {"command_type":"query_llm","query":"<str>","description":"<str>","risk_level":"none"}\n'
            "CRITICAL: For open_url steps, always include the full 'url' field starting with https://.\n"
            f"Task classification: {decision.task_type.value}\n"
            f"Extracted intent: {decision.extracted_intent}"
        )
        plan_message = [{"role": "user", "content": system_prompt}]
        full_response = ""
        try:
            async for chunk in llm_service.stream_response(plan_message, user_input):
                full_response += chunk
        except Exception as e:
            logger.warning(f"Plan extraction LLM call failed: {e} — will use fallback plan")
        return full_response


    def _build_fallback_plan(self, user_input: str, decision, action_id: str) -> ActionPlan:
        """Build a minimal ActionPlan from routing decision when LLM plan is absent."""
        steps: List[BaseCommand] = []

        if decision.task_type == TaskType.WEB:
            url = decision.parameters.get("url")
            raw_task = decision.parameters.get("raw_task") or user_input
            if not url:
                url = "about:blank"
            steps.append(OpenURL(url=url, description=raw_task))
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
        start_index: int = 0,
        approved_step: Optional[int] = None,
    ):
        """Execute each step of the saga through the guard chain."""
        # When resuming after approval, earlier steps were already completed
        # and must remain reflected in the final progress event.
        steps_completed = start_index
        steps_failed = 0
        total_duration_ms = 0.0
        compensating_actions: List[tuple[int, BaseCommand]] = []

        event_emitter = lambda evt: _emit(session_id, evt)

        for i, command in enumerate(plan.steps):
            if i < start_index:
                continue
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
                    await self._update_action(db, action_id, "failed", threat_level=guard_result.highest_risk.value)
                    return "blocked"
                continue

            # Approval gate
            if guard_result.requires_approval and i != approved_step:
                await self._request_approval(command, guard_result, i, action_id, plan, session_id, db)
                # Resume this exact plan after approval; never reconstruct a
                # single guessed command from the original sentence.
                self._pending_sagas[action_id] = (plan, raw_input, i, execution_mode)
                await self._update_action(
                    db, action_id, "awaiting_approval",
                    result={
                        "pending_plan": plan.model_dump(mode="json"),
                        "raw_input": raw_input,
                        "next_step": i,
                        "execution_mode": execution_mode,
                    },
                )
                return "awaiting_approval"

            # Skip pure queries — no OS execution needed
            if command.command_type == CommandType.QUERY_LLM:
                steps_completed += 1
                continue

            # Execute command. Interactive desktop plans use the same guard
            # boundary as ordinary commands, then hand the full intent to the
            # UI Automation agent so it can launch, find, click, and type.
            if (
                isinstance(command, LaunchApp)
                and hasattr(os_engine, "run_desktop_agent")
                and (
                    _is_interactive_desktop_task(plan.original_intent)
                    or str(getattr(command, "description", "") or "").startswith("Control desktop:")
                )
            ):
                cmd_result = await os_engine.run_desktop_agent(
                    plan.original_intent,
                    session_id=session_id,
                )
            else:
                cmd_result = await os_engine.execute(command, session_id=session_id)
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
                    await self._update_action(
                        db,
                        action_id,
                        "failed",
                        result={"error": cmd_result.error or "Step execution failed"},
                        error_message=cmd_result.error or "Step execution failed",
                    )
                    return "failed"

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
        return "completed"

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
        if not approved:
            self._pending_sagas.pop(action_id, None)
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
        db: AsyncSession = None,
    ) -> dict:
        """Resume the guarded plan after approval.

        The plan is kept in memory for the lifetime of the running task. A
        missing plan is reported explicitly instead of executing a dangerous,
        incomplete approximation based on regexes over the original text.
        """
        from app.core.database import AsyncSessionLocal
        from app.models.models import ActionLog, ActionStatus
        from sqlalchemy import select

        async with AsyncSessionLocal() as local_db:
            result = await local_db.execute(select(ActionLog).where(ActionLog.id == action_id))
            action = result.scalar_one_or_none()
            if not action:
                return {"error": "Action not found"}

            start = datetime.utcnow()
            action.status = ActionStatus.RUNNING
            await local_db.commit()
            await manager.send_action_update(session_id, {"id": action_id, "status": "running"})

            pending = self._pending_sagas.pop(action_id, None)
            if not pending:
                stored = action.result or {}
                plan_data = stored.get("pending_plan") if isinstance(stored, dict) else None
                if isinstance(plan_data, dict):
                    try:
                        pending = (
                            parse_action_plan(plan_data),
                            stored.get("raw_input") or action.command,
                            int(stored.get("next_step", 0)),
                            stored.get("execution_mode") or ExecutionMode.AUTONOMOUS,
                        )
                    except Exception as restore_error:
                        logger.error("Could not restore approved task plan: %s", restore_error)
            if not pending:
                error = "The guarded task plan is no longer available; please submit the task again."
                action.status = ActionStatus.FAILED
                action.error_message = error
                action.completed_at = datetime.utcnow()
                await local_db.commit()
                await manager.send_action_update(session_id, {
                    "id": action_id, "status": ActionStatus.FAILED.value, "error": error,
                })
                return {"action_id": action_id, "status": ActionStatus.FAILED.value, "error": error}

            plan, raw_input, start_index, execution_mode = pending
            try:
                saga_status = await self._execute_saga(
                    plan=plan,
                    action_id=action_id,
                    session_id=session_id,
                    db=local_db,
                    execution_mode=execution_mode,
                    raw_input=raw_input,
                    start_index=start_index,
                    approved_step=start_index,
                )
            except Exception as e:
                logger.error(f"Approved task resume failed: {e}", exc_info=True)
                saga_status = "failed"

            duration_ms = (datetime.utcnow() - start).total_seconds() * 1000
            status_map = {
                "completed": ActionStatus.COMPLETED,
                "blocked": ActionStatus.FAILED,
                "failed": ActionStatus.FAILED,
                "awaiting_approval": ActionStatus.AWAITING_APPROVAL,
            }
            final_status = status_map.get(saga_status, ActionStatus.FAILED)
            action.status = final_status
            action.completed_at = datetime.utcnow()
            action.duration_ms = duration_ms
            await local_db.commit()

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

    async def _update_action(
        self,
        db,
        action_id,
        status,
        requires_approval=None,
        threat_level=None,
        result=None,
        error_message=None,
    ):
        from app.models.models import ActionLog, ActionStatus, ThreatLevel as TL
        from sqlalchemy import select, update
        try:
            status_map = {
                "pending": ActionStatus.PENDING, "running": ActionStatus.RUNNING,
                "completed": ActionStatus.COMPLETED, "failed": ActionStatus.FAILED,
                "awaiting_approval": ActionStatus.AWAITING_APPROVAL,
            }
            update_vals = {
                "status": status_map.get(status, ActionStatus.COMPLETED),
            }
            if status not in {"pending", "running"}:
                update_vals["completed_at"] = datetime.utcnow()
            if requires_approval is not None:
                update_vals["requires_approval"] = requires_approval
            if threat_level:
                threat_map = {"none": TL.NONE, "low": TL.LOW, "medium": TL.MEDIUM,
                              "high": TL.HIGH, "critical": TL.CRITICAL}
                update_vals["threat_level"] = threat_map.get(threat_level, TL.NONE)
            if result is not None:
                update_vals["result"] = result
            if error_message is not None:
                update_vals["error_message"] = error_message
            await db.execute(
                update(ActionLog).where(ActionLog.id == action_id).values(**update_vals)
            )
            await db.commit()
        except Exception as e:
            logger.error(f"Failed to update action: {e}")


# Singleton
orchestrator = ExecutionOrchestrator()
