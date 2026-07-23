// ============================================================
// DirectAct-AI — Shared TypeScript Types
// ============================================================

export type MessageRole = 'user' | 'assistant' | 'system';

export interface Message {
  id: string;
  session_id: string;
  role: MessageRole;
  content: string;
  created_at: string;
  tokens_used?: number;
  isStreaming?: boolean;
}

export interface Session {
  id: string;
  name: string;
  status: 'active' | 'paused' | 'completed' | 'error';
  created_at: string;
  updated_at: string;
  message_count: number;
}

export type ActionStatus =
  | 'pending'
  | 'awaiting_approval'
  | 'approved'
  | 'declined'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled';

export type ThreatLevel = 'none' | 'low' | 'medium' | 'high' | 'critical';

export type ActionType = 'web' | 'desktop' | 'system' | 'query';

export interface ActionLog {
  id: string;
  session_id: string;
  action_type: ActionType;
  description: string;
  command: string;
  status: ActionStatus;
  threat_level: ThreatLevel;
  requires_approval: boolean;
  result?: Record<string, unknown>;
  error_message?: string;
  duration_ms?: number;
  created_at: string;
  completed_at?: string;
}

// WebSocket Event Types
export type WsEventType =
  | 'stream_chunk'
  | 'stream_end'
  | 'status'
  | 'action_update'
  | 'viewport_screenshot'
  | 'error'
  | 'pong'
  | 'ack';

export interface WsEvent {
  type: WsEventType;
  [key: string]: unknown;
}

export interface WsStreamChunk extends WsEvent {
  type: 'stream_chunk';
  message_id: string;
  chunk: string;
}

export interface WsStreamEnd extends WsEvent {
  type: 'stream_end';
  message_id: string;
}

export interface WsStatus extends WsEvent {
  type: 'status';
  status: string;
  data: Record<string, unknown>;
}

export interface WsActionUpdate extends WsEvent {
  type: 'action_update';
  action: Partial<ActionLog>;
}

export interface WsViewportScreenshot extends WsEvent {
  type: 'viewport_screenshot';
  label: string;
  data: string;      // base64 webp
  format: string;
  timestamp: string;
}

export interface WsError extends WsEvent {
  type: 'error';
  error: string;
  details?: string;
}

// API Responses
export interface HealthResponse {
  status: string;
  app: string;
  version: string;
  timestamp: string;
  system?: Record<string, string>;
  resources?: Record<string, number>;
  websocket?: { active_sessions: number };
  llm_provider?: string;
  llm_active_provider?: string;
  llm_ready?: boolean;
  gemini_configured?: boolean;
}

export type WsConnectionStatus = 'connecting' | 'connected' | 'disconnected' | 'error';
