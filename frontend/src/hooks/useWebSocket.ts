import { useEffect, useRef, useCallback } from 'react';
import { useAppStore } from '../store/appStore';
import type { WsActionUpdate, WsViewportScreenshot } from '../types';
import type {
  TaskStartedEvent, TaskStepStartedEvent, TaskStepCompletedEvent,
  SecurityCheckResultEvent, PermissionRequestedEvent, AuditLogEntryEvent,
} from '../types/events';

const TOKEN_KEY = 'directact_token';

/**
 * FIX: Include the JWT token as a query parameter so the server can authenticate
 * the WebSocket connection. WebSocket API does not support custom headers, so
 * the token must be sent in the URL (?token=<jwt>).
 */
const WS_URL = (sessionId: string): string => {
  const token = localStorage.getItem(TOKEN_KEY) ?? '';
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const tokenParam = token ? `?token=${encodeURIComponent(token)}` : '';
  return `${proto}://${location.host}/ws/${sessionId}${tokenParam}`;
};

const BASE_RECONNECT_MS = 3000;
const MAX_RECONNECT_MS = 30000;

export function useWebSocket(sessionId: string | null) {
  // ── Stable refs (never re-ordered, always the same number of hooks) ─────────
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const isIntentionalClose = useRef(false);
  const reconnectAttempt = useRef(0);
  // storeRef lets event handler always see latest state without causing re-renders
  const storeRef = useRef(useAppStore.getState());

  // Sync storeRef on every store change — NO deps change, just subscription
  useEffect(() => {
    return useAppStore.subscribe((state) => {
      storeRef.current = state;
    });
  }, []);

  // ── Stable callbacks (all with empty deps — they read from refs) ───────────
  const handleEvent = useCallback((event: any, sid: string) => {
    const s = storeRef.current;
    switch (event.type) {
      case 'text_chunk':
      case 'stream_chunk': {
        const chunk = event.content ?? event.chunk ?? '';
        const msgId = event.message_id;
        if (msgId) s.appendStreamChunk(sid, msgId, chunk);
        s.setIsStreaming(true);
        break;
      }
      case 'stream_end': {
        if (event.message_id) s.finalizeStreamMessage(sid, event.message_id);
        s.setIsStreaming(false);
        break;
      }
      case 'task_started': {
        const e = event as TaskStartedEvent;
        s.onTaskStarted(e.task_id, e.original_intent, e.total_steps, e.highest_risk, e.requires_any_approval);
        break;
      }
      case 'task_step_started':
        s.onTaskStepStarted(event as TaskStepStartedEvent);
        break;
      case 'task_step_completed':
        s.onTaskStepCompleted(event as TaskStepCompletedEvent);
        break;
      case 'task_completed':
        s.onTaskCompleted(event.task_id);
        break;
      case 'task_failed':
        s.onTaskFailed(event.task_id, event.reason);
        break;
      case 'security_check_result':
        s.onSecurityCheckResult(event as SecurityCheckResultEvent);
        break;
      case 'action_blocked':
        s.setActiveTab('security');
        break;
      case 'permission_requested':
        s.onPermissionRequested(event as PermissionRequestedEvent);
        break;
      case 'permission_granted':
      case 'permission_denied':
        if (event.action_id) s.onPermissionResolved(event.action_id);
        break;
      case 'audit_log_entry':
        s.onAuditLogEntry(event as AuditLogEntryEvent);
        break;
      case 'action_update': {
        const e = event as WsActionUpdate;
        if (e.action?.id) {
          const action = e.action as Parameters<typeof s.addAction>[0];
          if (action.id) {
            s.addAction({
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
            if (action.status === 'awaiting_approval') s.setActiveTab('approvals');
          }
        }
        break;
      }
      case 'viewport_screenshot': {
        const e = event as WsViewportScreenshot;
        s.setViewportFrame({ data: e.data, label: e.label, timestamp: e.timestamp });
        break;
      }
      case 'system_status':
      case 'status': {
        const details = event.details ?? event.data ?? {};
        if (details?.llm_provider) {
          s.setLlmProvider(details.llm_provider as string, details.llm_ready as boolean ?? false);
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
  }, []); // empty deps: storeRef keeps it current without changing identity

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

  const sendApproval = useCallback((actionId: string, approved: boolean) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'approval_response', action_id: actionId, approved }));
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

  // ── Core WebSocket lifecycle (single effect, only fires when sessionId changes) ─
  useEffect(() => {
    if (!sessionId) return;

    let destroyed = false;

    function connect() {
      if (destroyed) return;
      if (
        wsRef.current &&
        (wsRef.current.readyState === WebSocket.OPEN ||
          wsRef.current.readyState === WebSocket.CONNECTING)
      ) return;

      storeRef.current.setWsStatus('connecting');
      // FIX: WS_URL now includes ?token=<jwt> for server-side authentication
      const ws = new WebSocket(WS_URL(sessionId!));
      wsRef.current = ws;

      ws.onopen = () => {
        if (destroyed) { ws.close(); return; }
        reconnectAttempt.current = 0;
        storeRef.current.setWsStatus('connected');
        console.log('[WS] Connected:', sessionId);
      };

      ws.onmessage = (ev) => {
        if (destroyed) return;
        try {
          handleEvent(JSON.parse(ev.data), sessionId!);
        } catch {
          console.error('[WS] Parse error', ev.data);
        }
      };

      ws.onclose = (e) => {
        if (destroyed) return;
        storeRef.current.setWsStatus('disconnected');
        if (isIntentionalClose.current) { isIntentionalClose.current = false; return; }

        // FIX: if server rejected with auth error (4001), do not retry
        if (e.code === 4001) {
          console.error('[WS] Authentication failed — token invalid or missing. Not retrying.');
          storeRef.current.setWsStatus('error');
          return;
        }
        // FIX: if server rejected with access denied (4003 / 4004), do not retry
        if (e.code === 4003 || e.code === 4004) {
          console.error('[WS] Access denied or session not found. Not retrying.');
          storeRef.current.setWsStatus('error');
          return;
        }

        console.log('[WS] Disconnected:', e.code, e.reason);
        // Exponential back-off: 3s → 4.5s → 6.75s … capped at 30s
        const delay = Math.min(
          BASE_RECONNECT_MS * Math.pow(1.5, reconnectAttempt.current),
          MAX_RECONNECT_MS
        );
        reconnectAttempt.current += 1;
        if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
        reconnectTimer.current = setTimeout(connect, delay);
      };

      ws.onerror = () => {
        if (destroyed) return;
        storeRef.current.setWsStatus('error');
        // onclose fires after onerror → reconnect scheduled there
      };
    }

    connect();

    const heartbeat = setInterval(() => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ type: 'ping' }));
      }
    }, 25000);

    return () => {
      destroyed = true;
      clearInterval(heartbeat);
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      if (wsRef.current) {
        isIntentionalClose.current = true;
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, [sessionId, handleEvent]); // handleEvent has stable identity (empty deps)

  return { sendMessage, sendApproval, sendBrowserStart, sendBrowserClose };
}
