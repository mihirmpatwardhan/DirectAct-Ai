import React, { useEffect, useRef, useState, useCallback } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Send, Zap, Globe, Monitor } from 'lucide-react';
import { format } from 'date-fns';
import { useAppStore } from '../../store/appStore';
import { useWebSocket } from '../../hooks/useWebSocket';
import { fetchMessages, createSession } from '../../lib/api';
import type { Message } from '../../types';

const SUGGESTIONS = [
  { icon: '🌐', text: 'Open google.com and search for AI news' },
  { icon: '📁', text: 'Open File Explorer' },
  { icon: '📊', text: 'Open Notepad and write a task list' },
  { icon: '🔍', text: 'Search YouTube for a tutorial' },
];

interface ChatPanelProps {
  sessionId: string | null;
}

export const ChatPanel: React.FC<ChatPanelProps> = ({ sessionId }) => {
  const [input, setInput] = useState('');
  const [targetEngine, setTargetEngine] = useState<'auto' | 'web' | 'desktop'>('auto');
  const [isSending, setIsSending] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const { messages, addMessage, setMessages, isStreaming, addSession, setActiveSession } = useAppStore();
  const { sendMessage: wsSend } = useWebSocket(sessionId);

  const sessionMessages: Message[] = sessionId ? (messages[sessionId] ?? []) : [];

  // Load message history when session changes
  useEffect(() => {
    if (!sessionId) return;
    fetchMessages(sessionId)
      .then((msgs) => setMessages(sessionId, msgs))
      .catch(console.error);
  }, [sessionId, setMessages]);

  // Auto-scroll to bottom
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [sessionMessages.length, isStreaming]);

  const handleSend = useCallback(async () => {
    let currentSessionId = sessionId;
    if (!currentSessionId) {
      try {
        const newSess = await createSession(`Session ${format(new Date(), 'MMM d HH:mm')}`);
        addSession(newSess);
        setActiveSession(newSess.id);
        currentSessionId = newSess.id;
      } catch (err) {
        console.error('Failed to create session automatically', err);
        return;
      }
    }

    if (!input.trim() || isSending) return;
    const content = input.trim();
    setInput('');
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }

    // Optimistic user message
    const tempUserMsg: Message = {
      id: `temp-user-${Date.now()}`,
      session_id: currentSessionId,
      role: 'user',
      content,
      created_at: new Date().toISOString(),
    };
    addMessage(currentSessionId, tempUserMsg);

    setIsSending(true);
    try {
      // Send via WebSocket with targetEngine
      wsSend(content, targetEngine);
    } finally {
      setIsSending(false);
    }
  }, [input, sessionId, isSending, targetEngine, addMessage, wsSend, addSession, setActiveSession]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInput(e.target.value);
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
      textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 140)}px`;
    }
  };

  const handleSuggestion = (text: string) => {
    setInput(text);
    textareaRef.current?.focus();
  };

  const isEmpty = sessionMessages.length === 0;

  return (
    <div className="chat-panel">
      <div className="chat-messages">
        {isEmpty && !sessionId ? (
          <NoSessionPlaceholder />
        ) : isEmpty ? (
          <WelcomeScreen onSuggestion={handleSuggestion} />
        ) : (
          <>
            {sessionMessages.map((msg) => (
              <MessageRow key={msg.id} message={msg} />
            ))}
            {isStreaming && sessionMessages[sessionMessages.length - 1]?.role !== 'assistant' && (
              <TypingRow />
            )}
          </>
        )}
        <div ref={bottomRef} />
      </div>

      <div className="chat-input-area">
        {/* Quick Mode Selector Pills */}
        <div style={{
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          marginBottom: 8,
          fontSize: 11,
        }}>
          <span style={{ color: 'var(--text-muted)', fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.5px' }}>Mode:</span>
          {([
            { id: 'auto', label: '⚡ Auto Hybrid' },
            { id: 'web', label: '🌐 Web Browser' },
            { id: 'desktop', label: '💻 Desktop App' },
          ] as const).map(({ id, label }) => (
            <button
              key={id}
              onClick={() => setTargetEngine(id)}
              style={{
                padding: '3px 8px',
                borderRadius: 12,
                border: '1px solid',
                borderColor: targetEngine === id ? 'var(--accent-primary)' : 'var(--border-subtle)',
                background: targetEngine === id ? 'rgba(16, 185, 129, 0.15)' : 'var(--bg-elevated)',
                color: targetEngine === id ? 'var(--accent-primary)' : 'var(--text-muted)',
                fontWeight: targetEngine === id ? 600 : 400,
                cursor: 'pointer',
                transition: 'all 0.15s ease',
              }}
            >
              {label}
            </button>
          ))}
        </div>

        <div className="chat-input-wrapper">
          <textarea
            ref={textareaRef}
            id="chat-input"
            className="chat-input"
            placeholder="Tell DirectAct-AI what to do… (Shift+Enter for newline)"
            value={input}
            onChange={handleInput}
            onKeyDown={handleKeyDown}
            disabled={isSending}
            rows={1}
          />
          <button
            id="chat-send-btn"
            className="send-button"
            onClick={handleSend}
            disabled={!input.trim() || isSending}
            title="Send (Enter)"
          >
            <Send size={15} />
          </button>
        </div>
        <p className="input-hint">
          <Zap size={11} style={{ display: 'inline', marginRight: 4 }} />
          DirectAct-AI can control your browser and desktop — always review sensitive actions
        </p>
      </div>
    </div>
  );
};

// ---- Sub-components ----

const WelcomeScreen: React.FC<{ onSuggestion: (t: string) => void }> = ({ onSuggestion }) => (
  <div className="welcome-screen">
    <div className="welcome-logo">⚡</div>
    <div>
      <h1 className="welcome-title">DirectAct-AI</h1>
      <p style={{ color: 'var(--accent-secondary)', fontSize: 13, marginTop: 2 }}>Define. Direct. Done.</p>
    </div>
    <p className="welcome-subtitle">
      Your AI Copilot for <strong style={{ color: 'var(--accent-tertiary)' }}>web</strong> and{' '}
      <strong style={{ color: 'var(--accent-secondary)' }}>desktop</strong> automation.
      Just describe what you want — I'll handle the clicks, forms, and files.
    </p>
    <div className="suggestion-chips">
      {SUGGESTIONS.map((s) => (
        <button
          key={s.text}
          className="suggestion-chip"
          onClick={() => onSuggestion(s.text)}
        >
          <span>{s.icon}</span>
          {s.text}
        </button>
      ))}
    </div>
    <div style={{ display: 'flex', gap: 20, color: 'var(--text-muted)', fontSize: 12 }}>
      <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
        <Globe size={12} /> Web Automation
      </span>
      <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
        <Monitor size={12} /> Desktop Control
      </span>
      <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
        🔒 Secure Execution
      </span>
    </div>
  </div>
);

const NoSessionPlaceholder: React.FC = () => (
  <div className="welcome-screen">
    <div className="welcome-logo" style={{ background: 'var(--bg-elevated)', boxShadow: 'none' }}>
      💬
    </div>
    <p style={{ color: 'var(--text-muted)' }}>Create a new session to get started</p>
  </div>
);

const MessageRow: React.FC<{ message: Message }> = ({ message }) => {
  const isUser = message.role === 'user';
  return (
    <div className={`message-row ${isUser ? 'user' : ''}`}>
      <div className={`message-avatar ${isUser ? 'user' : 'assistant'}`}>
        {isUser ? '👤' : '⚡'}
      </div>
      <div>
        <div className={`message-bubble ${isUser ? 'user' : 'assistant'}`}>
          {isUser ? (
            <span style={{ whiteSpace: 'pre-wrap' }}>{message.content}</span>
          ) : (
            <div className="prose-dark">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {message.content}
              </ReactMarkdown>
              {message.isStreaming && (
                <span style={{
                  display: 'inline-block',
                  width: 8,
                  height: 14,
                  background: 'var(--accent-primary)',
                  marginLeft: 2,
                  borderRadius: 2,
                  animation: 'blink 0.8s ease-in-out infinite',
                }} />
              )}
            </div>
          )}
        </div>
        <p className={`message-time ${isUser ? '' : ''}`} style={{ textAlign: isUser ? 'right' : 'left' }}>
          {format(new Date(message.created_at), 'HH:mm')}
        </p>
      </div>
    </div>
  );
};

const TypingRow: React.FC = () => (
  <div className="message-row">
    <div className="message-avatar assistant">⚡</div>
    <div>
      <div className="typing-indicator">
        <div className="typing-dot" />
        <div className="typing-dot" />
        <div className="typing-dot" />
      </div>
    </div>
  </div>
);
