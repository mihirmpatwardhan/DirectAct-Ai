import React, { useEffect, useState } from 'react';
import { Plus, MessageSquare, Trash2, ChevronRight } from 'lucide-react';
import { format } from 'date-fns';
import { useAppStore } from '../../store/appStore';
import { fetchSessions, createSession, deleteSession } from '../../lib/api';
import type { Session } from '../../types';

export const Sidebar: React.FC = () => {
  const {
    sessions,
    activeSessionId,
    setSessions,
    addSession,
    setActiveSession,
    setMessages,
  } = useAppStore();
  const [isCreating, setIsCreating] = useState(false);

  useEffect(() => {
    fetchSessions()
      .then(async (s) => {
        setSessions(s);
        if (s.length > 0) {
          // Clear any stale messages before activating the first session
          setMessages(s[0].id, []);
          setActiveSession(s[0].id);
        } else {
          // Auto-create initial session if none exist
          try {
            const newSess = await createSession(`Session ${format(new Date(), 'MMM d HH:mm')}`);
            addSession(newSess);
            setMessages(newSess.id, []);
            setActiveSession(newSess.id);
          } catch (err) {
            console.error('Auto session creation failed', err);
          }
        }
      })
      .catch(console.error);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleNewSession = async () => {
    if (isCreating) return;
    setIsCreating(true);
    try {
      const session = await createSession(`Session ${format(new Date(), 'MMM d HH:mm')}`);
      addSession(session);
      // Clear messages immediately so the new chat starts totally empty
      setMessages(session.id, []);
      setActiveSession(session.id);
    } catch (e) {
      console.error('Failed to create session', e);
    } finally {
      setIsCreating(false);
    }
  };

  const handleSelectSession = (session: Session) => {
    if (session.id === activeSessionId) return; // already active — no-op
    // Immediately wipe the message cache for this session so the old
    // session's messages never flash before fetchMessages completes.
    setMessages(session.id, []);
    setActiveSession(session.id);
  };

  const handleDeleteSession = async (e: React.MouseEvent, session: Session) => {
    e.stopPropagation();
    try {
      await deleteSession(session.id);
      const updated = sessions.filter((s) => s.id !== session.id);
      setSessions(updated);
      if (activeSessionId === session.id) {
        const next = updated[0] ?? null;
        if (next) {
          setMessages(next.id, []);
          setActiveSession(next.id);
        } else {
          setActiveSession(null);
        }
      }
    } catch {
      console.error('Failed to delete session');
    }
  };

  return (
    <div className="sidebar">
      <div className="sidebar-header">
        <button
          id="new-session-btn"
          className="btn btn-primary"
          style={{ width: '100%', justifyContent: 'center' }}
          onClick={handleNewSession}
          disabled={isCreating}
        >
          <Plus size={14} />
          {isCreating ? 'Creating…' : 'New Chat'}
        </button>
      </div>

      <div className="session-list">
        {sessions.length === 0 && (
          <div style={{ padding: '20px 12px', textAlign: 'center', color: 'var(--text-muted)', fontSize: 12 }}>
            No chats yet.<br />Hit <strong>+ New Chat</strong> to begin.
          </div>
        )}
        {sessions.map((session) => (
          <SessionItem
            key={session.id}
            session={session}
            isActive={session.id === activeSessionId}
            onSelect={() => handleSelectSession(session)}
            onDelete={(e) => handleDeleteSession(e, session)}
          />
        ))}
      </div>

      {/* Footer */}
      <div style={{
        padding: '10px 12px',
        borderTop: '1px solid var(--border-subtle)',
        fontSize: 11,
        color: 'var(--text-muted)',
        display: 'flex',
        alignItems: 'center',
        gap: 6,
      }}>
        <MessageSquare size={11} />
        {sessions.length} chat{sessions.length !== 1 ? 's' : ''}
      </div>
    </div>
  );
};

const SessionItem: React.FC<{
  session: Session;
  isActive: boolean;
  onSelect: () => void;
  onDelete: (e: React.MouseEvent) => void;
}> = ({ session, isActive, onSelect, onDelete }) => {
  const [showDelete, setShowDelete] = useState(false);

  return (
    <div
      className={`session-item ${isActive ? 'active' : ''}`}
      onClick={onSelect}
      onMouseEnter={() => setShowDelete(true)}
      onMouseLeave={() => setShowDelete(false)}
      title={session.name}
    >
      <MessageSquare size={13} style={{ flexShrink: 0, color: isActive ? 'var(--accent-primary)' : undefined }} />
      <div className="session-name" style={{ flex: 1, minWidth: 0 }}>
        <div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {session.name}
        </div>
        <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 1 }}>
          {session.message_count} msg{session.message_count !== 1 ? 's' : ''} · {format(new Date(session.updated_at), 'MMM d')}
        </div>
      </div>
      {showDelete ? (
        <button
          className="btn-icon"
          style={{ width: 22, height: 22 }}
          onClick={onDelete}
          title="Delete chat"
        >
          <Trash2 size={11} />
        </button>
      ) : (
        <ChevronRight size={11} style={{ color: 'var(--text-muted)', opacity: isActive ? 1 : 0 }} />
      )}
    </div>
  );
};

