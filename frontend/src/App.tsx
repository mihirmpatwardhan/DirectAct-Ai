import React, { useState } from 'react';
import { Settings, Cpu, Zap, Brain } from 'lucide-react';
import { useAppStore } from './store/appStore';
import { Sidebar } from './components/Sidebar/Sidebar';
import { ChatPanel } from './components/ChatPanel/ChatPanel';
import { ViewportPanel } from './components/ViewportPanel/ViewportPanel';
import { TimelinePanel } from './components/TimelinePanel/TimelinePanel';

function App() {
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [customChatWidth, setCustomChatWidth] = useState<number>(340);
  const { wsStatus, llmProvider, llmReady, layoutMode } = useAppStore();

  return (
    <div
      className={`app-shell mode-${layoutMode}`}
      style={{ '--chat-width': `${customChatWidth}px` } as React.CSSProperties}
    >
      {/* Top Bar */}
      <header className="app-topbar">
        <div className="topbar-logo">
          <div className="topbar-logo-icon">⚡</div>
          <span className="topbar-logo-name">DirectAct-AI</span>
          <span className="topbar-logo-tagline">Define. Direct. Done.</span>
        </div>

        <div className="topbar-sep" />

        {/* Panel Width Adjuster Slider */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 11, color: 'var(--text-muted)' }}>
          <span>Chat Width:</span>
          <input
            type="range"
            min="240"
            max="600"
            step="10"
            value={customChatWidth}
            onChange={(e) => setCustomChatWidth(Number(e.target.value))}
            style={{ width: 80, cursor: 'pointer', accentColor: 'var(--accent-primary)' }}
            title={`Adjust Chat Width (${customChatWidth}px)`}
          />
          <span style={{ fontSize: 10, fontFamily: 'monospace' }}>{customChatWidth}px</span>
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
      <Sidebar onSessionSelect={setActiveSessionId} />

      {/* Center: Chat Panel */}
      <ChatPanel sessionId={activeSessionId} />

      {/* Center-Right: Live Viewport */}
      <ViewportPanel />

      {/* Right: Timeline / Execution Panel */}
      <TimelinePanel />
    </div>
  );
}

export default App;
