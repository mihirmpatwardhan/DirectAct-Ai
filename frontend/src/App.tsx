import React from 'react';
import { Settings, Cpu, Zap, Brain } from 'lucide-react';
import { useAppStore } from './store/appStore';
import { Sidebar } from './components/Sidebar/Sidebar';
import { ChatPanel } from './components/ChatPanel/ChatPanel';
import { TimelinePanel } from './components/TimelinePanel/TimelinePanel';
import { ProfileSelector } from './components/ProfileSelector/ProfileSelector';
import { useWebSocket } from './hooks/useWebSocket';

function App() {
  // Use Zustand as the single source of truth for active session —
  // no local useState needed. Sidebar writes to Zustand, ChatPanel reads from it.
  const activeSessionId = useAppStore((s) => s.activeSessionId);
  const { wsStatus, llmProvider, llmReady } = useAppStore();
  // There must be exactly one socket per dashboard/session. Both ChatPanel and
  // TimelinePanel need to send messages, but the server intentionally keeps a
  // single live connection per session to prevent duplicated event delivery.
  const { sendMessage, sendApproval } = useWebSocket(activeSessionId);

  return (
    <div className="app-shell">
      {/* Top Bar */}
      <header className="app-topbar">
        <div className="topbar-logo">
          <div className="topbar-logo-icon">⚡</div>
          <span className="topbar-logo-name">DirectAct-AI</span>
          <span className="topbar-logo-tagline">Define. Direct. Done.</span>
        </div>

        <div className="topbar-sep" />

        {/* LLM Provider status */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 11 }}>
          <Brain size={12} style={{ color: llmReady ? 'var(--success)' : 'var(--text-muted)' }} />
          <span style={{
            color: llmReady ? 'var(--success)' : 'var(--text-muted)',
            fontWeight: 500,
          }}>
            {llmReady ? llmProvider.toUpperCase() : 'Stub'}
          </span>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12 }}>
          <div className={`conn-dot ${wsStatus}`} title={`WebSocket: ${wsStatus}`} />
          <span style={{ color: 'var(--text-muted)' }}>
            {wsStatus === 'connected' ? 'Live' : wsStatus === 'connecting' ? 'Connecting…' : 'Offline'}
          </span>
        </div>

        <div style={{ flex: 1 }} />

        {/* Chrome Profile Selector */}
        <ProfileSelector />

        <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 11, color: 'var(--text-muted)' }}>
            <Cpu size={12} />
            <span>Gemini</span>
          </div>
          <button className="btn-icon btn" title="Settings">
            <Settings size={14} />
          </button>
        </div>
      </header>

      {/* Left: Session Sidebar */}
      <Sidebar />

      {/* Center: Chat Panel — fills central space */}
      <ChatPanel sessionId={activeSessionId} sendMessage={sendMessage} />

      {/* Right: Timeline / Execution Panel */}
      <TimelinePanel sendApproval={sendApproval} />
    </div>
  );
}

export default App;
