/**
 * Typed Event Bus — Frontend Types (Phase 1.4)
 * ==============================================
 * TypeScript mirror of backend/app/schemas/events.py
 * Single source of truth for the WebSocket event contract.
 *
 * Keep this file in sync with events.py — any backend schema change
 * must be reflected here.
 */

// ──────────────────────────────────────────────────────────────────────────────
// Event Types
// ──────────────────────────────────────────────────────────────────────────────

export type EventType =
  // Task lifecycle
  | 'task_started'
  | 'task_step_started'
  | 'task_step_completed'
  | 'task_completed'
  | 'task_failed'
  | 'task_cancelled'
  // Security pipeline
  | 'security_check_started'
  | 'security_check_result'
  | 'action_blocked'
  // Human-in-the-loop
  | 'permission_requested'
  | 'permission_granted'
  | 'permission_denied'
  // LLM streaming
  | 'text_chunk'
  | 'stream_end'
  // System
  | 'system_status'
  | 'error_occurred'
  | 'audit_log_entry'
  // Legacy backward compat
  | 'action_update'
  | 'stream_chunk';

export type RiskLevel = 'safe' | 'low' | 'medium' | 'high' | 'critical';
export type GuardCheckName =
  | 'policy_validation'
  | 'static_analysis'
  | 'privilege_check'
  | 'file_scan'
  | 'sandbox_decision'
  | 'directory_access'
  | 'confirmation_gate'
  | 'audit_log'
  | 'pipeline';

export type ExecutionMode = 'autonomous' | 'hitl';

// ──────────────────────────────────────────────────────────────────────────────
// Base Event
// ──────────────────────────────────────────────────────────────────────────────

export interface BaseEvent {
  type: EventType;
  session_id: string;
  timestamp: string;
}

// ──────────────────────────────────────────────────────────────────────────────
// Task Lifecycle Events
// ──────────────────────────────────────────────────────────────────────────────

export interface TaskStartedEvent extends BaseEvent {
  type: 'task_started';
  task_id: string;
  original_intent: string;
  total_steps: number;
  highest_risk: RiskLevel;
  requires_any_approval: boolean;
}

export interface TaskStepStartedEvent extends BaseEvent {
  type: 'task_step_started';
  task_id: string;
  step_index: number;
  total_steps: number;
  command_type: string;
  description: string;
  risk_level: RiskLevel;
}

export interface TaskStepCompletedEvent extends BaseEvent {
  type: 'task_step_completed';
  task_id: string;
  step_index: number;
  command_type: string;
  success: boolean;
  duration_ms: number;
  output_summary?: string;
  error?: string;
}

export interface TaskCompletedEvent extends BaseEvent {
  type: 'task_completed';
  task_id: string;
  steps_completed: number;
  steps_failed: number;
  total_duration_ms: number;
}

export interface TaskFailedEvent extends BaseEvent {
  type: 'task_failed';
  task_id: string;
  failed_at_step: number;
  reason: string;
}

// ──────────────────────────────────────────────────────────────────────────────
// Security Pipeline Events
// ──────────────────────────────────────────────────────────────────────────────

export interface SecurityCheckStartedEvent extends BaseEvent {
  type: 'security_check_started';
  task_id: string;
  step_index: number;
  check_name: GuardCheckName;
  command_type: string;
}

export interface SecurityCheckResultEvent extends BaseEvent {
  type: 'security_check_result';
  task_id: string;
  step_index: number;
  check_name: GuardCheckName;
  passed: boolean;
  halted: boolean;
  reason: string;
  risk_level: RiskLevel;
  duration_ms: number;
}

export interface ActionBlockedEvent extends BaseEvent {
  type: 'action_blocked';
  task_id: string;
  step_index: number;
  blocked_by: GuardCheckName;
  reason: string;
  redacted_content_hash?: string;
}

// ──────────────────────────────────────────────────────────────────────────────
// Human-in-the-Loop Events
// ──────────────────────────────────────────────────────────────────────────────

export interface PermissionRequestedEvent extends BaseEvent {
  type: 'permission_requested';
  action_id: string;
  task_id: string;
  step_index: number;
  command_type: string;
  description: string;
  risk_level: RiskLevel;
  reason_for_approval: string;
}

export interface PermissionGrantedEvent extends BaseEvent {
  type: 'permission_granted';
  action_id: string;
  task_id: string;
}

export interface PermissionDeniedEvent extends BaseEvent {
  type: 'permission_denied';
  action_id: string;
  task_id: string;
}

// ──────────────────────────────────────────────────────────────────────────────
// LLM Streaming Events
// ──────────────────────────────────────────────────────────────────────────────

export interface TextChunkEvent extends BaseEvent {
  type: 'text_chunk' | 'stream_chunk';
  message_id: string;
  content?: string;
  chunk?: string;  // legacy field name
}

export interface StreamEndEvent extends BaseEvent {
  type: 'stream_end';
  message_id: string;
}

// ──────────────────────────────────────────────────────────────────────────────
// System Events
// ──────────────────────────────────────────────────────────────────────────────

export interface SystemStatusEvent extends BaseEvent {
  type: 'system_status';
  status: string;
  details: Record<string, unknown>;
}

export interface ErrorOccurredEvent extends BaseEvent {
  type: 'error_occurred';
  task_id?: string;
  error_code: string;
  message: string;
  recoverable: boolean;
}

export interface AuditLogEntryEvent extends BaseEvent {
  type: 'audit_log_entry';
  sequence: number;
  action_id: string;
  task_id?: string;
  verdict: 'allowed' | 'blocked' | 'requires_approval';
  check_name?: string;
  risk_level: RiskLevel;
  entry_hash: string;
  prev_hash: string;
  content_redacted: boolean;
}

// Legacy action update (backward compat with existing Timeline panel)
export interface ActionUpdateEvent {
  type: 'action_update';
  action: {
    id: string;
    session_id?: string;
    action_type?: string;
    description?: string;
    command?: string;
    status: string;
    threat_level?: string;
    requires_approval?: boolean;
    created_at?: string;
    duration_ms?: number;
  };
}

// ──────────────────────────────────────────────────────────────────────────────
// Union discriminator
// ──────────────────────────────────────────────────────────────────────────────

export type AnyEvent =
  | TaskStartedEvent
  | TaskStepStartedEvent
  | TaskStepCompletedEvent
  | TaskCompletedEvent
  | TaskFailedEvent
  | SecurityCheckStartedEvent
  | SecurityCheckResultEvent
  | ActionBlockedEvent
  | PermissionRequestedEvent
  | PermissionGrantedEvent
  | PermissionDeniedEvent
  | TextChunkEvent
  | StreamEndEvent
  | SystemStatusEvent
  | ErrorOccurredEvent
  | AuditLogEntryEvent
  | ActionUpdateEvent;

// ──────────────────────────────────────────────────────────────────────────────
// Guard check metadata for UI display
// ──────────────────────────────────────────────────────────────────────────────

export const GUARD_CHECK_LABELS: Record<GuardCheckName, string> = {
  policy_validation: 'Policy Validation',
  static_analysis: 'Static Analysis',
  privilege_check: 'Privilege Check',
  file_scan: 'File Scan (AMSI)',
  sandbox_decision: 'Sandbox Decision',
  directory_access: 'Directory Access',
  confirmation_gate: 'Confirmation Gate',
  audit_log: 'Audit Log',
  pipeline: 'Pipeline',
};

export const RISK_LEVEL_COLORS: Record<RiskLevel, string> = {
  safe: '#10b981',    // emerald-500
  low: '#3b82f6',     // blue-500
  medium: '#f59e0b',  // amber-500
  high: '#f97316',    // orange-500
  critical: '#ef4444', // red-500
};

export const RISK_LEVEL_LABELS: Record<RiskLevel, string> = {
  safe: 'Safe',
  low: 'Low',
  medium: 'Medium',
  high: 'High',
  critical: 'Critical',
};
