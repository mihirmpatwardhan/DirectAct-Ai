import { useEffect, useRef, useCallback } from 'react';
import { useAppStore } from '../store/appStore';
import type { WsEvent, WsStreamChunk, WsStreamEnd, WsActionUpdate, WsViewportScreenshot, WsStatus } from '../types';
import type {
  TaskStartedEvent, TaskStepStartedEvent, TaskStepCompletedEvent,
  SecurityCheckResultEvent, PermissionRequestedEvent, AuditLogEntryEvent,
} from '../types/events';

const WS_URL = (sessionId: string) =>
  `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/${sessionId}`;

const RECONNECT_DELAY_MS = 3000;
const MAX_RECONNECT = 5;

export function useWebSocket(sessionId: string | null) {
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectCount = useRef(0);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const {
    setWsStatus,
    appendStreamChunk,
    finalizeStreamMessage,
    addAction,
    updateAction,
    setIsStreaming,
    setViewportFrame,
    setLlmProvider,
    setActiveTab,
    // Typed event handlers (Phase 1.4)
    onTaskStarted,
    onTaskStepStarted,
    onTaskStepCompleted,
    onTaskCompleted,
    onTaskFailed,
    onSecurityCheckResult,
    onPermissionRequested,
    onPermissionResolved,
    onAuditLogEntry,
  } = useAppStore();

  const connect = useCallback(() => {
    if (!sessionId) return;

    if (wsRef.current && wsRef.current.readyState !== WebSocket.CLOSED) {
      wsRef.current.close();
    }

    setWsStatus('connecting');
    const ws = new WebSocket(WS_URL(sessionId));
    wsRef.current = ws;

    ws.onopen = () => {
      setWsStatus('connected');
      reconnectCount.current = 0;
      console.log('[WS] Connected to session:', sessionId);
    };

    ws.onmessage = (event) => {
      try {
        const data: WsEvent = JSON.parse(event.data);
        handleEvent(data, sessionId);
      } catch {
        console.error('[WS] Failed to parse message', event.data);
      }
    };

    ws.onclose = (e) => {
      setWsStatus('disconnected');
      console.log('[WS] Disconnected', e.code, e.reason);
      if (reconnectCount.current < MAX_RECONNECT) {
        reconnectCount.current++;
        reconnectTimer.current = setTimeout(connect, RECONNECT_DELAY_MS);
      }
    };

    ws.onerror = () => {
      setWsStatus('error');
    };
  }, [sessionId]);

  const handleEvent = (event: any, sid: string) => {
    switch (event.type) {
      // ── LLM Streaming (new typed names + legacy) ─────────────────────────
      case 'text_chunk':
      case 'stream_chunk': {
        const chunk = event.content ?? event.chunk ?? '';
        const msgId = event.message_id;
        if (msgId) appendStreamChunk(sid, msgId, chunk);
        setIsStreaming(true);
        break;
      }
      case 'stream_end': {
        if (event.message_id) finalizeStreamMessage(sid, event.message_id);
        setIsStreaming(false);
        break;
      }

      // ── Task Lifecycle ────────────────────────────────────────────────────
      case 'task_started': {
        const e = event as TaskStartedEvent;
        onTaskStarted(e.task_id, e.original_intent, e.total_steps, e.highest_risk, e.requires_any_approval);
        break;
      }
      case 'task_step_started': {
        onTaskStepStarted(event as TaskStepStartedEvent);
        break;
      }
      case 'task_step_completed': {
        onTaskStepCompleted(event as TaskStepCompletedEvent);
        break;
      }
      case 'task_completed': {
        onTaskCompleted(event.task_id);
        break;
      }
      case 'task_failed': {
        onTaskFailed(event.task_id, event.reason);
        break;
      }

      // ── Security Pipeline ─────────────────────────────────────────────────
      case 'security_check_result': {
        onSecurityCheckResult(event as SecurityCheckResultEvent);
        break;
      }
      case 'action_blocked': {
        // Show in security tab
        setActiveTab('security');
        break;
      }

      // ── Human-in-the-Loop ─────────────────────────────────────────────────
      case 'permission_requested': {
        onPermissionRequested(event as PermissionRequestedEvent);
        // Tab switch handled in store
        break;
      }
      case 'permission_granted':
      case 'permission_denied': {
        if (event.action_id) onPermissionResolved(event.action_id);
        break;
      }

      // ── Audit Log ─────────────────────────────────────────────────────────
      case 'audit_log_entry': {
        onAuditLogEntry(event as AuditLogEntryEvent);
        break;
      }

      // ── Legacy action_update ──────────────────────────────────────────────
      case 'action_update': {
        const e = event as WsActionUpdate;
        if (e.action?.id) {
          const action = e.action as Parameters<typeof addAction>[0];
          if (action.id) {
            addAction({
              id: action.id,
              session_id: action.session_id ?? sid,
              action_type: action.action_type ?? 'system',
              description: action.description ?? '',
              command: action.command ?? '',
              status: action.status ?? 'pending',
              threat_level: action.threat_level ?? 'none',
              requires_approval: action.requires_approval ?? false,
              created_at: action.created_at ?? new Date().toISOString(),
            });
            if (action.status === 'awaiting_approval') {
              setActiveTab('approvals');
            }
          }
        }
        break;
      }

      // ── Viewport ──────────────────────────────────────────────────────────
      case 'viewport_screenshot': {
        const e = event as WsViewportScreenshot;
        setViewportFrame({ data: e.data, label: e.label, timestamp: e.timestamp });
        break;
      }

      // ── System Status ─────────────────────────────────────────────────────
      case 'system_status':
      case 'status': {
        const details = event.details ?? event.data ?? {};
        if (details?.llm_provider) {
          setLlmProvider(details.llm_provider as string, details.llm_ready as boolean ?? false);
        }
        break;
      }

      case 'error_occurred':
      case 'error':
        console.error('[WS] Server error:', event.message ?? event.error, event.details);
        break;
      case 'pong':
      case 'ack':
        break;
      default:
        console.debug('[WS] Unknown event type:', event.type);
    }
  };

  const sendMessage = useCallback(
    (content: string, targetEngine?: 'auto' | 'web' | 'desktop') => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(
          JSON.stringify({ type: 'chat_message', content, target_engine: targetEngine })
        );
      }
    },
    []
  );

  const sendPing = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'ping' }));
    }
  }, []);

  const sendApproval = useCallback((actionId: string, approved: boolean) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(
        JSON.stringify({ type: 'approval_response', action_id: actionId, approved })
      );
    }
  }, []);

  const sendBrowserStart = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'start_browser' }));
    }
  }, []);

  const sendBrowserClose = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'close_browser' }));
    }
  }, []);

  useEffect(() => {
    connect();
    const heartbeat = setInterval(sendPing, 25000);
    return () => {
      clearInterval(heartbeat);
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
    };
  }, [connect, sendPing]);

  return { sendMessage, sendApproval, sendBrowserStart, sendBrowserClose };
}
