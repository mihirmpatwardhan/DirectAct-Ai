import axios from 'axios';
import type { Session, Message, HealthResponse, ActionLog } from '../types';

const api = axios.create({
  baseURL: '/api/v1',
  headers: { 'Content-Type': 'application/json' },
  timeout: 30000,
});

// ---- Health ----
export async function fetchHealth(): Promise<HealthResponse> {
  const res = await api.get<HealthResponse>('/health/detailed');
  return res.data;
}

// ---- Sessions ----
export async function fetchSessions(): Promise<Session[]> {
  const res = await api.get<Session[]>('/sessions');
  return res.data;
}

export async function createSession(name = 'New Session'): Promise<Session> {
  const res = await api.post<Session>('/sessions', { name });
  return res.data;
}

export async function deleteSession(id: string): Promise<void> {
  await api.delete(`/sessions/${id}`);
}

// ---- Chat ----
export async function fetchMessages(sessionId: string): Promise<Message[]> {
  const res = await api.get<Message[]>(`/chat/${sessionId}/messages`);
  return res.data;
}

export async function sendMessage(
  sessionId: string,
  content: string
): Promise<{ user_message: Message; assistant_message: Message }> {
  const res = await api.post('/chat/send', { session_id: sessionId, content });
  return res.data;
}

// ---- Actions ----
export async function fetchActions(sessionId: string): Promise<ActionLog[]> {
  const res = await api.get<ActionLog[]>(`/actions/${sessionId}`);
  return res.data;
}

export async function fetchPendingApprovals(sessionId: string): Promise<ActionLog[]> {
  const res = await api.get<ActionLog[]>(`/actions/pending/${sessionId}`);
  return res.data;
}

export async function approveAction(
  actionId: string,
  approved: boolean
): Promise<{ action_id: string; status: string }> {
  const res = await api.post(`/actions/${actionId}/approve`, { approved });
  return res.data;
}

export default api;
