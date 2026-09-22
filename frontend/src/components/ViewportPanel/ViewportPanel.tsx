import React, { useState } from 'react';
import { Globe, Monitor, Terminal, RefreshCw } from 'lucide-react';
import { useAppStore } from '../../store/appStore';

export const ViewportPanel: React.FC = () => {
  const { viewportFrame, viewportHistory, isStreaming, layoutMode, setLayoutMode } = useAppStore();
  const [activeView, setActiveView] = useState<'live' | 'history' | 'terminal'>('live');
  const [historyIdx, setHistoryIdx] = useState(0);

  const displayFrame = activeView === 'history' && viewportHistory.length > 0
    ? viewportHistory[historyIdx]
    : viewportFrame;
  const isDesktopFrame = Boolean(displayFrame?.label.toLowerCase().includes('desktop'));

  return (
    <div className="viewport-panel">
      {/* Header */}
      <div className="viewport-header">
        {isDesktopFrame ? (
          <Monitor size={14} style={{ color: 'var(--accent-secondary)' }} />
        ) : (
          <Globe size={14} style={{ color: 'var(--accent-tertiary)' }} />
        )}
        <span style={{ fontSize: 12, color: 'var(--text-secondary)', fontWeight: 600 }}>
          {isDesktopFrame ? 'Live Desktop' : 'Live Browser'}
        </span>
        {isStreaming && (
          <span style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 11, color: 'var(--accent-tertiary)' }}>
            <span className="live-dot" />
            LIVE
          </span>
        )}

        <div style={{ flex: 1 }} />

        {/* Viewport Size Controls */}
        <div style={{
          display: 'flex',
          alignItems: 'center',
          gap: 2,
          background: 'var(--bg-elevated)',
          border: '1px solid var(--border-default)',
          borderRadius: 6,
          padding: 2,
        }}>
          <button
            className="btn btn-ghost"
            style={{
              padding: '2px 6px',
              fontSize: 10,
              background: layoutMode === 'large' ? 'var(--accent-primary)' : 'transparent',
              color: layoutMode === 'large' ? 'white' : 'var(--text-muted)',
              borderRadius: 4,
            }}
            onClick={() => setLayoutMode('large')}
            title="Max Viewport Mode (Compact Chat)"
          >
            🖥️ Max
          </button>
          <button
            className="btn btn-ghost"
            style={{
              padding: '2px 6px',
              fontSize: 10,
              background: layoutMode === 'cinema' ? 'var(--accent-primary)' : 'transparent',
              color: layoutMode === 'cinema' ? 'white' : 'var(--text-muted)',
              borderRadius: 4,
            }}
            onClick={() => setLayoutMode('cinema')}
            title="Ultra-Wide Cinema Mode"
          >
            🍿 Cinema
          </button>
          <button
            className="btn btn-ghost"
            style={{
              padding: '2px 6px',
              fontSize: 10,
              background: layoutMode === 'equal' ? 'var(--accent-primary)' : 'transparent',
              color: layoutMode === 'equal' ? 'white' : 'var(--text-muted)',
              borderRadius: 4,
            }}
            onClick={() => setLayoutMode('equal')}
            title="50-50 Split Mode"
          >
            ⚖️ 50-50
          </button>
        </div>
      </div>

      {/* View Tabs */}
      <div style={{ display: 'flex', borderBottom: '1px solid var(--border-subtle)', flexShrink: 0 }}>
        {([
          { id: 'live', icon: <Globe size={11} />, label: 'Browser' },
          { id: 'history', icon: <RefreshCw size={11} />, label: `History (${viewportHistory.length})` },
          { id: 'terminal', icon: <Terminal size={11} />, label: 'Terminal' },
        ] as const).map(({ id, icon, label }) => (
          <button
            key={id}
            className={`timeline-tab ${activeView === id ? 'active' : ''}`}
            onClick={() => setActiveView(id)}
            style={{ fontSize: 11 }}
          >
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 3 }}>
              {icon} {label}
            </span>
          </button>
        ))}
      </div>

      {/* Viewport Content */}
      <div style={{ flex: 1, overflow: 'hidden', position: 'relative', background: '#000' }}>
        {activeView === 'live' && (
          displayFrame ? (
            <LiveScreenshot frame={displayFrame} />
          ) : (
            <ViewportPlaceholder />
          )
        )}

        {activeView === 'history' && viewportHistory.length > 0 && (
          <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
            <LiveScreenshot frame={viewportHistory[historyIdx]} />
            {/* History scrubber */}
            <div style={{
              padding: '8px 12px',
              background: 'var(--bg-surface)',
              borderTop: '1px solid var(--border-subtle)',
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              flexShrink: 0,
            }}>
              <button
                className="btn btn-ghost"
                style={{ padding: '2px 8px', fontSize: 11, minWidth: 0 }}
                onClick={() => setHistoryIdx(Math.max(0, historyIdx - 1))}
                disabled={historyIdx === 0}
              >‹</button>
              <span style={{ fontSize: 11, color: 'var(--text-muted)', flex: 1, textAlign: 'center' }}>
                {historyIdx + 1} / {viewportHistory.length} — {viewportHistory[historyIdx]?.label}
              </span>
              <button
                className="btn btn-ghost"
                style={{ padding: '2px 8px', fontSize: 11, minWidth: 0 }}
                onClick={() => setHistoryIdx(Math.min(viewportHistory.length - 1, historyIdx + 1))}
                disabled={historyIdx >= viewportHistory.length - 1}
              >›</button>
            </div>
          </div>
        )}

        {activeView === 'history' && viewportHistory.length === 0 && (
          <ViewportPlaceholder message="No history yet. Screenshots will appear after browser actions." />
        )}

        {activeView === 'terminal' && <TerminalView />}
      </div>

      {/* Status bar */}
      {displayFrame && activeView === 'live' && (
        <div style={{
          padding: '5px 12px',
          borderTop: '1px solid var(--border-subtle)',
          background: 'var(--bg-surface)',
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          fontSize: 11,
          color: 'var(--text-muted)',
          flexShrink: 0,
        }}>
          <Monitor size={10} />
          <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {displayFrame.label}
          </span>
          <span>{new Date(displayFrame.timestamp).toLocaleTimeString()}</span>
        </div>
      )}
    </div>
  );
};

// ── Sub-components ──────────────────────────────────

const LiveScreenshot: React.FC<{ frame: { data: string; label: string; timestamp: string } }> = ({ frame }) => (
  <div style={{ width: '100%', height: '100%', position: 'relative', overflow: 'hidden' }}>
    <img
      src={frame.data.startsWith('data:') ? frame.data : `data:image/jpeg;base64,${frame.data}`}
      alt={frame.label}
      style={{
        width: '100%',
        height: '100%',
        objectFit: 'contain',
        imageRendering: 'crisp-edges',
      }}
    />
    {/* Highlight overlay — pulsing border when live */}
    <div style={{
      position: 'absolute',
      inset: 0,
      pointerEvents: 'none',
      border: '2px solid transparent',
      borderRadius: 2,
      animation: 'viewport-live-pulse 2s ease-in-out infinite',
    }} />
  </div>
);

const ViewportPlaceholder: React.FC<{ message?: string }> = ({
  message = 'Browser or desktop screenshots will stream here during automation',
}) => (
  <div className="viewport-placeholder" style={{ height: '100%' }}>
    <div className="viewport-placeholder-icon" style={{
      animation: 'pulse-glow 3s ease-in-out infinite',
      background: 'var(--bg-elevated)',
    }}>
      🌐
    </div>
    <p style={{ fontSize: 13, fontWeight: 500, color: 'var(--text-secondary)' }}>
      Live Browser Preview
    </p>
    <p style={{ fontSize: 12, maxWidth: 240, textAlign: 'center', color: 'var(--text-muted)', lineHeight: 1.6 }}>
      {message}
    </p>
    <div style={{ display: 'flex', gap: 8, marginTop: 8, flexWrap: 'wrap', justifyContent: 'center' }}>
      <CapabilityBadge icon="🌐" label="Screenshot stream" color="var(--accent-tertiary)" />
      <CapabilityBadge icon="🔴" label="Element highlight" color="var(--error)" />
      <CapabilityBadge icon="🔒" label="Isolated profile" color="var(--success)" />
    </div>
  </div>
);

const CapabilityBadge: React.FC<{ icon: string; label: string; color: string }> = ({ icon, label, color }) => (
  <div style={{
    display: 'flex',
    alignItems: 'center',
    gap: 4,
    padding: '4px 10px',
    background: 'var(--bg-elevated)',
    border: `1px solid var(--border-subtle)`,
    borderRadius: 99,
    fontSize: 11,
    color: 'var(--text-muted)',
  }}>
    <span style={{ color }}>{icon}</span>
    <span>{label}</span>
  </div>
);

const TerminalView: React.FC = () => {
  const { actions } = useAppStore();
  const desktopActions = actions.filter(a => a.action_type === 'desktop');

  return (
    <div style={{
      height: '100%',
      overflow: 'auto',
      padding: '12px',
      fontFamily: "'JetBrains Mono', monospace",
      fontSize: 12,
      background: '#0a0a0a',
      display: 'flex',
      flexDirection: 'column',
      gap: 4,
    }}>
      <div style={{ color: 'var(--success)', marginBottom: 8 }}>
        DirectAct-AI PowerShell Engine — Audit Trail
      </div>
      {desktopActions.length === 0 ? (
        <div style={{ color: 'var(--text-muted)' }}>
          {'>_ No desktop commands executed yet'}
        </div>
      ) : (
        desktopActions.map((action) => (
          <TerminalLine key={action.id} action={action} />
        ))
      )}
      <div style={{ color: 'var(--accent-primary)' }}>{'>_ '}<BlinkCursor /></div>
    </div>
  );
};

const TerminalLine: React.FC<{ action: import('../../types').ActionLog }> = ({ action }) => {
  const statusColor: Record<string, string> = {
    completed: 'var(--success)',
    failed: 'var(--error)',
    running: 'var(--accent-tertiary)',
    blocked: 'var(--error)',
    awaiting_approval: 'var(--warning)',
  };
  return (
    <div>
      <span style={{ color: 'var(--text-muted)' }}>[{new Date(action.created_at).toLocaleTimeString()}] </span>
      <span style={{ color: 'var(--accent-secondary)' }}>PS&gt; </span>
      <span style={{ color: 'var(--text-secondary)' }}>{action.command.slice(0, 120)}</span>
      <span style={{ color: statusColor[action.status] ?? 'var(--text-muted)', marginLeft: 8 }}>
        [{action.status}]
      </span>
    </div>
  );
};

const BlinkCursor: React.FC = () => (
  <span style={{
    display: 'inline-block',
    width: 7,
    height: 13,
    background: 'var(--accent-primary)',
    borderRadius: 2,
    animation: 'blink 1s ease-in-out infinite',
    verticalAlign: 'middle',
  }} />
);
