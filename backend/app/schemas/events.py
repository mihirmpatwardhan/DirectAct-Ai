"""
Typed Event Bus Schema — Phase 1.0
=====================================
All events emitted on the internal event bus and forwarded to the frontend
via WebSocket are defined here as Pydantic models.

This schema is the single contract between backend state and UI rendering.
No UI polling of internal state — event-driven only.

Frontend TypeScript equivalents: frontend/src/types/events.ts
"""
from __future__ import annotations

import enum
import hashlib
import json
from datetime import datetime
from typing import Optional, Any, Union, Literal
from pydantic import BaseModel, Field


# ──────────────────────────────────────────────────────────────────────────────
# Event Types
# ──────────────────────────────────────────────────────────────────────────────

class EventType(str, enum.Enum):
    # Task lifecycle
    TASK_STARTED = "task_started"
    TASK_STEP_STARTED = "task_step_started"
    TASK_STEP_COMPLETED = "task_step_completed"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"

    # Security pipeline (one event per Guard step)
    SECURITY_CHECK_STARTED = "security_check_started"
    SECURITY_CHECK_RESULT = "security_check_result"
    ACTION_BLOCKED = "action_blocked"

    # Human-in-the-loop
    PERMISSION_REQUESTED = "permission_requested"
    PERMISSION_GRANTED = "permission_granted"
    PERMISSION_DENIED = "permission_denied"

    # LLM streaming
    TEXT_CHUNK = "text_chunk"
    STREAM_END = "stream_end"

    # System
    SYSTEM_STATUS = "system_status"
    ERROR_OCCURRED = "error_occurred"
    AUDIT_LOG_ENTRY = "audit_log_entry"

    # Connection management
    CONNECTED = "connected"
    PONG = "pong"
    ACK = "ack"


# ──────────────────────────────────────────────────────────────────────────────
# Base Event
# ──────────────────────────────────────────────────────────────────────────────

class BaseEvent(BaseModel):
    """All events carry a type, session_id, and timestamp."""
    type: EventType
    session_id: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    def to_ws_payload(self) -> dict:
        """Serialize for WebSocket transmission."""
        return json.loads(self.model_dump_json())


# ──────────────────────────────────────────────────────────────────────────────
# Task Lifecycle Events
# ──────────────────────────────────────────────────────────────────────────────

class TaskStartedEvent(BaseEvent):
    type: Literal[EventType.TASK_STARTED] = EventType.TASK_STARTED
    task_id: str
    original_intent: str
    total_steps: int
    highest_risk: str  # RiskLevel value
    requires_any_approval: bool


class TaskStepStartedEvent(BaseEvent):
    type: Literal[EventType.TASK_STEP_STARTED] = EventType.TASK_STEP_STARTED
    task_id: str
    step_index: int
    total_steps: int
    command_type: str
    description: str
    risk_level: str


class TaskStepCompletedEvent(BaseEvent):
    type: Literal[EventType.TASK_STEP_COMPLETED] = EventType.TASK_STEP_COMPLETED
    task_id: str
    step_index: int
    command_type: str
    success: bool
    duration_ms: float
    output_summary: Optional[str] = None  # Truncated output for UI display
    error: Optional[str] = None


class TaskCompletedEvent(BaseEvent):
    type: Literal[EventType.TASK_COMPLETED] = EventType.TASK_COMPLETED
    task_id: str
    steps_completed: int
    steps_failed: int
    total_duration_ms: float


class TaskFailedEvent(BaseEvent):
    type: Literal[EventType.TASK_FAILED] = EventType.TASK_FAILED
    task_id: str
    failed_at_step: int
    reason: str


# ──────────────────────────────────────────────────────────────────────────────
# Security Pipeline Events
# ──────────────────────────────────────────────────────────────────────────────

class GuardCheckName(str, enum.Enum):
    POLICY_VALIDATION = "policy_validation"
    STATIC_ANALYSIS = "static_analysis"
    PRIVILEGE_CHECK = "privilege_check"
    FILE_SCAN = "file_scan"
    SANDBOX_DECISION = "sandbox_decision"
    DIRECTORY_ACCESS = "directory_access"
    CONFIRMATION_GATE = "confirmation_gate"
    AUDIT_LOG = "audit_log"


class SecurityCheckStartedEvent(BaseEvent):
    type: Literal[EventType.SECURITY_CHECK_STARTED] = EventType.SECURITY_CHECK_STARTED
    task_id: str
    step_index: int
    check_name: GuardCheckName
    command_type: str


class SecurityCheckResultEvent(BaseEvent):
    type: Literal[EventType.SECURITY_CHECK_RESULT] = EventType.SECURITY_CHECK_RESULT
    task_id: str
    step_index: int
    check_name: GuardCheckName
    passed: bool
    halted: bool  # True = pipeline stopped here
    reason: str
    risk_level: str
    duration_ms: float


class ActionBlockedEvent(BaseEvent):
    type: Literal[EventType.ACTION_BLOCKED] = EventType.ACTION_BLOCKED
    task_id: str
    step_index: int
    blocked_by: GuardCheckName
    reason: str
    # Note: raw blocked content is NEVER sent to the UI; only a sanitized reason
    redacted_content_hash: Optional[str] = None  # SHA-256 of blocked content for audit


# ──────────────────────────────────────────────────────────────────────────────
# Human-in-the-Loop Events
# ──────────────────────────────────────────────────────────────────────────────

class PermissionRequestedEvent(BaseEvent):
    type: Literal[EventType.PERMISSION_REQUESTED] = EventType.PERMISSION_REQUESTED
    action_id: str
    task_id: str
    step_index: int
    command_type: str
    description: str
    risk_level: str
    reason_for_approval: str


class PermissionGrantedEvent(BaseEvent):
    type: Literal[EventType.PERMISSION_GRANTED] = EventType.PERMISSION_GRANTED
    action_id: str
    task_id: str


class PermissionDeniedEvent(BaseEvent):
    type: Literal[EventType.PERMISSION_DENIED] = EventType.PERMISSION_DENIED
    action_id: str
    task_id: str


# ──────────────────────────────────────────────────────────────────────────────
# LLM Streaming Events
# ──────────────────────────────────────────────────────────────────────────────

class TextChunkEvent(BaseEvent):
    type: Literal[EventType.TEXT_CHUNK] = EventType.TEXT_CHUNK
    message_id: str
    content: str


class StreamEndEvent(BaseEvent):
    type: Literal[EventType.STREAM_END] = EventType.STREAM_END
    message_id: str


# ──────────────────────────────────────────────────────────────────────────────
# System & Error Events
# ──────────────────────────────────────────────────────────────────────────────

class SystemStatusEvent(BaseEvent):
    type: Literal[EventType.SYSTEM_STATUS] = EventType.SYSTEM_STATUS
    status: str
    details: dict = Field(default_factory=dict)


class ErrorOccurredEvent(BaseEvent):
    type: Literal[EventType.ERROR_OCCURRED] = EventType.ERROR_OCCURRED
    task_id: Optional[str] = None
    error_code: str
    message: str
    recoverable: bool = True


# ──────────────────────────────────────────────────────────────────────────────
# Audit Log Event — hash-chained, forwarded to Security Status panel
# ──────────────────────────────────────────────────────────────────────────────

class AuditLogEntryEvent(BaseEvent):
    """Represents a single entry in the hash-chained audit log.
    Forwarded to the UI Security Status panel (with content redacted by default).
    The raw entry is stored in the backend's tamper-evident log."""
    type: Literal[EventType.AUDIT_LOG_ENTRY] = EventType.AUDIT_LOG_ENTRY
    sequence: int
    action_id: str
    task_id: Optional[str] = None
    verdict: str  # "allowed" | "blocked" | "requires_approval"
    check_name: Optional[str] = None
    risk_level: str
    entry_hash: str        # SHA-256 of this entry content
    prev_hash: str         # SHA-256 of previous entry (chain link)
    content_redacted: bool = True  # Raw content never shown by default


# ──────────────────────────────────────────────────────────────────────────────
# Union discriminator for WebSocket deserialization
# ──────────────────────────────────────────────────────────────────────────────

AnyEvent = Union[
    TaskStartedEvent, TaskStepStartedEvent, TaskStepCompletedEvent,
    TaskCompletedEvent, TaskFailedEvent,
    SecurityCheckStartedEvent, SecurityCheckResultEvent, ActionBlockedEvent,
    PermissionRequestedEvent, PermissionGrantedEvent, PermissionDeniedEvent,
    TextChunkEvent, StreamEndEvent,
    SystemStatusEvent, ErrorOccurredEvent,
    AuditLogEntryEvent,
]


def make_redacted_hash(content: str) -> str:
    """Create a SHA-256 hash of sensitive content for audit logging
    without storing the raw content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
