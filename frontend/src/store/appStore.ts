import { create } from 'zustand';
import { immer } from 'zustand/middleware/immer';
import type {
  Session,
  Message,
  ActionLog,
  WsConnectionStatus,
} from '../types';
import type {
  AuditLogEntryEvent,
  GuardCheckName,
  PermissionRequestedEvent,
  RiskLevel,
  SecurityCheckResultEvent,
  TaskStepCompletedEvent,
  TaskStepStartedEvent,
  ExecutionMode,
} from '../types/events';

// ──────────────────────────────────────────────────────────────────────────────
// Live Execution Preview types
// ──────────────────────────────────────────────────────────────────────────────

export interface TaskStep {
  task_id: string;
  step_index: number;
  total_steps: number;
  command_type: string;
  description: string;
  risk_level: RiskLevel;
  status: 'pending' | 'running' | 'success' | 'failed' | 'blocked' | 'awaiting_approval';
  duration_ms?: number;
  output_summary?: string;
  error?: string;
}

export interface ActiveTask {
  task_id: string;
  original_intent: string;
  total_steps: number;
  highest_risk: RiskLevel;
  requires_any_approval: boolean;
  steps: TaskStep[];
  status: 'running' | 'completed' | 'failed';
  started_at: string;
}

export interface SecurityCheckRecord {
  check_name: GuardCheckName;
  passed: boolean;
  halted: boolean;
  reason: string;
  risk_level: RiskLevel;
  duration_ms: number;
  timestamp: string;
}

export interface PendingApproval {
  action_id: string;
  task_id: string;
  step_index: number;
  command_type: string;
  description: string;
  risk_level: RiskLevel;
  reason_for_approval: string;
  timestamp: string;
}

export interface ViewportFrame {
  data: string;    // base64 webp
  label: string;
  timestamp: string;
}

// ──────────────────────────────────────────────────────────────────────────────
// Store interface
// ──────────────────────────────────────────────────────────────────────────────

interface AppState {
  // Sessions
  sessions: Session[];
  activeSessionId: string | null;

  // Messages (keyed by session_id)
  messages: Record<string, Message[]>;

  // Actions / Timeline (legacy)
  actions: ActionLog[];

  // Live Execution Preview (Phase 1.4+)
  activeTasks: Record<string, ActiveTask>;    // task_id → task
  currentTaskId: string | null;
  securityChecks: SecurityCheckRecord[];      // Recent guard pipeline checks
  pendingApprovals: PendingApproval[];        // Awaiting human confirmation
  auditLog: AuditLogEntryEvent[];             // Hash-chained audit entries

  // Execution mode
  executionMode: ExecutionMode;

  // Viewport (live browser screenshots)
  viewportFrame: ViewportFrame | null;
  viewportHistory: ViewportFrame[];

  // WebSocket
  wsStatus: WsConnectionStatus;

  // LLM provider info
  llmProvider: string;
  llmReady: boolean;

  // UI State
  activeTab: 'timeline' | 'approvals' | 'threats' | 'security' | 'vault';
  isStreaming: boolean;
  layoutMode: 'large' | 'cinema' | 'equal';

  // ── Session mutations ──────────────────────────────────────────────────────
  setSessions: (sessions: Session[]) => void;
  addSession: (session: Session) => void;
  setActiveSession: (id: string | null) => void;

  // ── Message mutations ──────────────────────────────────────────────────────
  addMessage: (sessionId: string, message: Message) => void;
  setMessages: (sessionId: string, messages: Message[]) => void;
  appendStreamChunk: (sessionId: string, messageId: string, chunk: string) => void;
  finalizeStreamMessage: (sessionId: string, messageId: string) => void;

  // ── Action mutations (legacy) ──────────────────────────────────────────────
  addAction: (action: ActionLog) => void;
  updateAction: (id: string, updates: Partial<ActionLog>) => void;
  setActions: (actions: ActionLog[]) => void;

  // ── Typed event handlers (Phase 1.4) ──────────────────────────────────────
  onTaskStarted: (task_id: string, original_intent: string, total_steps: number,
                  highest_risk: RiskLevel, requires_any_approval: boolean) => void;
  onTaskStepStarted: (evt: TaskStepStartedEvent) => void;
  onTaskStepCompleted: (evt: TaskStepCompletedEvent) => void;
  onTaskCompleted: (task_id: string) => void;
  onTaskFailed: (task_id: string, reason: string) => void;
  onSecurityCheckResult: (evt: SecurityCheckResultEvent) => void;
  onPermissionRequested: (evt: PermissionRequestedEvent) => void;
  onPermissionResolved: (action_id: string) => void;
  onAuditLogEntry: (evt: AuditLogEntryEvent) => void;

  // ── Other mutations ────────────────────────────────────────────────────────
  setViewportFrame: (frame: ViewportFrame) => void;
  setWsStatus: (status: WsConnectionStatus) => void;
  setLlmProvider: (provider: string, ready: boolean) => void;
  setActiveTab: (tab: AppState['activeTab']) => void;
  setIsStreaming: (v: boolean) => void;
  setLayoutMode: (mode: 'large' | 'cinema' | 'equal') => void;
  setExecutionMode: (mode: ExecutionMode) => void;
}

// ──────────────────────────────────────────────────────────────────────────────
// Store implementation
// ──────────────────────────────────────────────────────────────────────────────

export const useAppStore = create<AppState>()(
  immer((set) => ({
    // Initial state
    sessions: [],
    activeSessionId: null,
    messages: {},
    actions: [],
    activeTasks: {},
    currentTaskId: null,
    securityChecks: [],
    pendingApprovals: [],
    auditLog: [],
    executionMode: 'hitl',
    viewportFrame: null,
    viewportHistory: [],
    wsStatus: 'disconnected',
    llmProvider: 'unknown',
    llmReady: false,
    activeTab: 'timeline',
    isStreaming: false,
    layoutMode: 'large',

    // ── Session mutations ────────────────────────────────────────────────────
    setSessions: (sessions) =>
      set((state) => { state.sessions = sessions; }),

    addSession: (session) =>
      set((state) => { state.sessions.unshift(session); }),

    setActiveSession: (id) =>
      set((state) => { state.activeSessionId = id; }),

    // ── Message mutations ────────────────────────────────────────────────────
    addMessage: (sessionId, message) =>
      set((state) => {
        if (!state.messages[sessionId]) state.messages[sessionId] = [];
        state.messages[sessionId].push(message);
      }),

    setMessages: (sessionId, messages) =>
      set((state) => { state.messages[sessionId] = messages; }),

    appendStreamChunk: (sessionId, messageId, chunk) =>
      set((state) => {
        const msgs = state.messages[sessionId];
        if (!msgs) return;
        const msg = msgs.find((m: Message) => m.id === messageId);
        if (msg) {
          msg.content += chunk;
        } else {
          msgs.push({
            id: messageId,
            session_id: sessionId,
            role: 'assistant',
            content: chunk,
            created_at: new Date().toISOString(),
            isStreaming: true,
          });
        }
      }),

    finalizeStreamMessage: (sessionId, messageId) =>
      set((state) => {
        const msgs = state.messages[sessionId];
        if (!msgs) return;
        const msg = msgs.find((m: Message) => m.id === messageId);
        if (msg) msg.isStreaming = false;
      }),

    // ── Action mutations (legacy) ────────────────────────────────────────────
    addAction: (action) =>
      set((state: AppState) => {
        const idx = state.actions.findIndex((a: ActionLog) => a.id === action.id);
        if (idx >= 0) {
          Object.assign(state.actions[idx], action);
        } else {
          state.actions.unshift(action);
        }
      }),

    updateAction: (id, updates) =>
      set((state: AppState) => {
        const action = state.actions.find((a: ActionLog) => a.id === id);
        if (action) Object.assign(action, updates);
      }),

    setActions: (actions) =>
      set((state: AppState) => { state.actions = actions; }),

    // ── Typed event handlers (Phase 1.4) ────────────────────────────────────
    onTaskStarted: (task_id, original_intent, total_steps, highest_risk, requires_any_approval) =>
      set((state) => {
        state.activeTasks[task_id] = {
          task_id,
          original_intent,
          total_steps,
          highest_risk,
          requires_any_approval,
          steps: [],
          status: 'running',
          started_at: new Date().toISOString(),
        };
        state.currentTaskId = task_id;
      }),

    onTaskStepStarted: (evt) =>
      set((state) => {
        const task = state.activeTasks[evt.task_id];
        if (!task) return;
        const existing = task.steps[evt.step_index];
        if (existing) {
          existing.status = 'running';
        } else {
          task.steps[evt.step_index] = {
            task_id: evt.task_id,
            step_index: evt.step_index,
            total_steps: evt.total_steps,
            command_type: evt.command_type,
            description: evt.description,
            risk_level: evt.risk_level,
            status: 'running',
          };
        }
      }),

    onTaskStepCompleted: (evt) =>
      set((state) => {
        const task = state.activeTasks[evt.task_id];
        if (!task) return;
        const step = task.steps[evt.step_index];
        if (step) {
          step.status = evt.success ? 'success' : 'failed';
          step.duration_ms = evt.duration_ms;
          step.output_summary = evt.output_summary;
          step.error = evt.error;
        }
      }),

    onTaskCompleted: (task_id) =>
      set((state) => {
        const task = state.activeTasks[task_id];
        if (task) task.status = 'completed';
      }),

    onTaskFailed: (task_id, reason) =>
      set((state) => {
        const task = state.activeTasks[task_id];
        if (task) task.status = 'failed';
      }),

    onSecurityCheckResult: (evt) =>
      set((state) => {
        state.securityChecks.unshift({
          check_name: evt.check_name,
          passed: evt.passed,
          halted: evt.halted,
          reason: evt.reason,
          risk_level: evt.risk_level,
          duration_ms: evt.duration_ms,
          timestamp: evt.timestamp,
        });
        // Keep last 100 security check results
        if (state.securityChecks.length > 100) {
          state.securityChecks = state.securityChecks.slice(0, 100);
        }
      }),

    onPermissionRequested: (evt) =>
      set((state) => {
        state.pendingApprovals.push({
          action_id: evt.action_id,
          task_id: evt.task_id,
          step_index: evt.step_index,
          command_type: evt.command_type,
          description: evt.description,
          risk_level: evt.risk_level,
          reason_for_approval: evt.reason_for_approval,
          timestamp: evt.timestamp,
        });
        // Auto-switch to approvals tab
        state.activeTab = 'approvals';
      }),

    onPermissionResolved: (action_id) =>
      set((state) => {
        state.pendingApprovals = state.pendingApprovals.filter(
          (p) => p.action_id !== action_id
        );
      }),

    onAuditLogEntry: (evt) =>
      set((state) => {
        state.auditLog.unshift(evt);
        if (state.auditLog.length > 200) {
          state.auditLog = state.auditLog.slice(0, 200);
        }
      }),

    // ── Other mutations ──────────────────────────────────────────────────────
    setViewportFrame: (frame) =>
      set((state) => {
        state.viewportFrame = frame;
        state.viewportHistory.unshift(frame);
        if (state.viewportHistory.length > 20) {
          state.viewportHistory = state.viewportHistory.slice(0, 20);
        }
      }),

    setWsStatus: (status) =>
      set((state) => { state.wsStatus = status; }),

    setLlmProvider: (provider, ready) =>
      set((state) => {
        state.llmProvider = provider;
        state.llmReady = ready;
      }),

    setActiveTab: (tab) =>
      set((state) => { state.activeTab = tab; }),

    setIsStreaming: (v) =>
      set((state) => { state.isStreaming = v; }),

    setLayoutMode: (mode) =>
      set((state) => { state.layoutMode = mode; }),

    setExecutionMode: (mode) =>
      set((state) => { state.executionMode = mode; }),
  }))
);
