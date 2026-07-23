import React, { useEffect, useState } from 'react';
import {
  Clock, ShieldCheck, AlertTriangle, Activity, CheckCircle,
  XCircle, Zap, Shield, Eye, EyeOff, Lock, Loader2,
  ChevronRight, ToggleLeft, ToggleRight, RefreshCw,
} from 'lucide-react';
import { useAppStore } from '../../store/appStore';
import { useWebSocket } from '../../hooks/useWebSocket';
import { fetchActions, approveAction } from '../../lib/api';
import type { ActionLog } from '../../types';
import type { RiskLevel, GuardCheckName } from '../../types/events';
import { GUARD_CHECK_LABELS, RISK_LEVEL_COLORS } from '../../types/events';

// ──────────────────────────────────────────────────────────────────────────────
// Tab definition
// ──────────────────────────────────────────────────────────────────────────────

const TABS = [
  { id: 'timeline' as const, label: 'Timeline', Icon: Clock },
  { id: 'approvals' as const, label: 'Approvals', Icon: ShieldCheck },
  { id: 'security' as const, label: 'Security', Icon: Shield },
  { id: 'threats' as const, label: 'Threats', Icon: AlertTriangle },
];

// ──────────────────────────────────────────────────────────────────────────────
// Risk badge
// ──────────────────────────────────────────────────────────────────────────────

const RiskBadge: React.FC<{ level: RiskLevel }> = ({ level }) => (
  <span style={{
    display: 'inline-flex',
    alignItems: 'center',
    gap: 3,
    padding: '1px 6px',
    borderRadius: 99,
    fontSize: 10,
    fontWeight: 600,
    background: RISK_LEVEL_COLORS[level] + '22',
    color: RISK_LEVEL_COLORS[level],
    border: `1px solid ${RISK_LEVEL_COLORS[level]}44`,
    textTransform: 'uppercase' as const,
    letterSpacing: '0.5px',
  }}>
    {level}
  </span>
);

// ──────────────────────────────────────────────────────────────────────────────
// Connection status dot
// ──────────────────────────────────────────────────────────────────────────────

const ConnectionStatus: React.FC<{ status: string }> = ({ status }) => {
  const color = status === 'connected' ? 'var(--accent-primary)'
    : status === 'connecting' ? 'var(--warning)'
    : 'var(--error)';
  return (
    <span style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 11, color }}>
      <span style={{
        width: 6, height: 6, borderRadius: '50%',
        background: color,
        boxShadow: status === 'connected' ? `0 0 6px ${color}` : 'none',
        animation: status === 'connecting' ? 'pulse 1.5s ease-in-out infinite' : 'none',
      }} />
      {status}
    </span>
  );
};

// ──────────────────────────────────────────────────────────────────────────────
// Execution Mode Toggle
// ──────────────────────────────────────────────────────────────────────────────

const ExecutionModeToggle: React.FC = () => {
  const { executionMode, setExecutionMode } = useAppStore();
  const isAuto = executionMode === 'autonomous';

  return (
    <button
      onClick={() => setExecutionMode(isAuto ? 'hitl' : 'autonomous')}
      title={isAuto ? 'Autonomous mode — click to switch to Human-in-the-Loop' : 'Human-in-the-Loop mode — click to switch to Autonomous'}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 5,
        padding: '3px 8px',
        borderRadius: 8,
        border: '1px solid',
        borderColor: isAuto ? 'var(--accent-tertiary)' : 'var(--accent-primary)',
        background: isAuto ? 'rgba(245, 158, 11, 0.1)' : 'rgba(16, 185, 129, 0.1)',
        color: isAuto ? 'var(--accent-tertiary)' : 'var(--accent-primary)',
        fontSize: 11,
        fontWeight: 600,
        cursor: 'pointer',
        transition: 'all 0.2s',
      }}
    >
      {isAuto ? <ToggleRight size={13} /> : <ToggleLeft size={13} />}
      {isAuto ? '⚡ Auto' : '🛡 HITL'}
    </button>
  );
};

// ──────────────────────────────────────────────────────────────────────────────
// Main Panel
// ──────────────────────────────────────────────────────────────────────────────

export const TimelinePanel: React.FC = () => {
  const {
    activeTab, setActiveTab,
    actions,
    wsStatus,
    activeSessionId,
    activeTasks,
    currentTaskId,
    pendingApprovals,
    securityChecks,
    auditLog,
    onPermissionResolved,
  } = useAppStore();
  const { sendApproval } = useWebSocket(activeSessionId);

  // Load legacy action history
  useEffect(() => {
    if (!activeSessionId) return;
    fetchActions(activeSessionId)
      .then((a) => useAppStore.getState().setActions(a))
      .catch(console.error);
  }, [activeSessionId]);

  const legacyPending = actions.filter((a) => a.status === 'awaiting_approval');
  const threats = actions.filter((a) =>
    ['medium', 'high', 'critical'].includes(a.threat_level)
  );
  const approvalCount = pendingApprovals.length + legacyPending.length;

  const handleApprove = (actionId: string, approved: boolean) => {
    sendApproval(actionId, approved);
    onPermissionResolved(actionId);
    useAppStore.getState().updateAction(actionId, {
      status: approved ? 'approved' : 'declined',
    });
  };

  return (
    <div className="timeline-panel">
      {/* Header */}
      <div className="timeline-header">
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <span style={{ fontSize: 13, fontWeight: 600 }}>Live Execution Preview</span>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <ExecutionModeToggle />
            <ConnectionStatus status={wsStatus} />
          </div>
        </div>

        {/* Active task summary bar */}
        {currentTaskId && activeTasks[currentTaskId] && (
          <ActiveTaskBar task={activeTasks[currentTaskId]} />
        )}
      </div>

      {/* Tabs */}
      <div className="timeline-tabs">
        {TABS.map(({ id, label, Icon }) => {
          const badge = id === 'approvals' ? approvalCount
            : id === 'threats' ? threats.length
            : id === 'security' ? securityChecks.filter(c => c.halted).length
            : 0;
          return (
            <button
              key={id}
              id={`timeline-tab-${id}`}
              className={`timeline-tab ${activeTab === id ? 'active' : ''}`}
              onClick={() => setActiveTab(id as any)}
            >
              <Icon size={12} style={{ display: 'inline', marginRight: 4 }} />
              {label}
              {badge > 0 && (
                <span style={{
                  marginLeft: 4,
                  background: id === 'approvals' ? 'var(--warning)' : 'var(--error)',
                  color: 'white',
                  borderRadius: 99,
                  padding: '0 5px',
                  fontSize: 10,
                  fontWeight: 700,
                }}>
                  {badge}
                </span>
              )}
            </button>
          );
        })}
      </div>

      {/* Tab content */}
      <div className="timeline-content">
        {activeTab === 'timeline' && (
          <LiveTaskTab tasks={activeTasks} legacyActions={actions} />
        )}
        {activeTab === 'approvals' && (
          <ApprovalsTab
            typed={pendingApprovals}
            legacy={legacyPending}
            onApprove={handleApprove}
          />
        )}
        {activeTab === 'security' && (
          <SecurityTab checks={securityChecks} auditLog={auditLog} />
        )}
        {activeTab === 'threats' && (
          <ThreatsTab actions={threats} />
        )}
      </div>
    </div>
  );
};

// ──────────────────────────────────────────────────────────────────────────────
// Active Task Bar
// ──────────────────────────────────────────────────────────────────────────────

const ActiveTaskBar: React.FC<{ task: any }> = ({ task }) => {
  const completed = task.steps.filter((s: any) => s.status === 'success').length;
  const pct = task.total_steps > 0 ? (completed / task.total_steps) * 100 : 0;

  return (
    <div style={{
      marginTop: 8,
      padding: '6px 8px',
      background: 'var(--bg-elevated)',
      borderRadius: 6,
      border: '1px solid var(--border-subtle)',
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
        <span style={{ fontSize: 11, color: 'var(--text-primary)', fontWeight: 500 }}>
          {task.original_intent.slice(0, 50)}{task.original_intent.length > 50 ? '…' : ''}
        </span>
        <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>
          {completed}/{task.total_steps} steps
        </span>
      </div>
      <div style={{ height: 3, background: 'var(--bg-base)', borderRadius: 3, overflow: 'hidden' }}>
        <div style={{
          height: '100%',
          width: `${pct}%`,
          background: task.status === 'failed' ? 'var(--error)'
            : task.status === 'completed' ? 'var(--accent-primary)'
            : 'var(--accent-secondary)',
          borderRadius: 3,
          transition: 'width 0.4s ease',
        }} />
      </div>
      <div style={{ display: 'flex', gap: 8, marginTop: 4 }}>
        <RiskBadge level={task.highest_risk} />
        <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>
          {task.status === 'running' ? '⏳ Running' : task.status === 'completed' ? '✅ Done' : '❌ Failed'}
        </span>
      </div>
    </div>
  );
};

// ──────────────────────────────────────────────────────────────────────────────
// Live Task Tab (replaces old Timeline)
// ──────────────────────────────────────────────────────────────────────────────

const LiveTaskTab: React.FC<{ tasks: Record<string, any>; legacyActions: ActionLog[] }> = ({
  tasks, legacyActions,
}) => {
  const taskList = Object.values(tasks).sort((a, b) =>
    new Date(b.started_at).getTime() - new Date(a.started_at).getTime()
  );

  if (taskList.length === 0 && legacyActions.length === 0) {
    return <EmptyState icon="⚡" text="No tasks yet — send a command to get started" />;
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8, padding: '8px 0' }}>
      {/* Live tasks */}
      {taskList.map((task) => (
        <TaskCard key={task.task_id} task={task} />
      ))}

      {/* Legacy action entries (for completed sessions or older format) */}
      {legacyActions.length > 0 && taskList.length === 0 && (
        legacyActions.map((a) => <LegacyActionRow key={a.id} action={a} />)
      )}
    </div>
  );
};

const TaskCard: React.FC<{ task: any }> = ({ task }) => {
  const [expanded, setExpanded] = useState(true);

  const statusIcon = task.status === 'running' ? <Loader2 size={12} className="spin" />
    : task.status === 'completed' ? <CheckCircle size={12} color="var(--accent-primary)" />
    : <XCircle size={12} color="var(--error)" />;

  return (
    <div style={{
      background: 'var(--bg-elevated)',
      border: '1px solid var(--border-subtle)',
      borderRadius: 8,
      overflow: 'hidden',
    }}>
      {/* Task header */}
      <button
        onClick={() => setExpanded(!expanded)}
        style={{
          width: '100%',
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '8px 10px',
          background: 'transparent',
          border: 'none',
          cursor: 'pointer',
          textAlign: 'left',
        }}
      >
        {statusIcon}
        <span style={{ flex: 1, fontSize: 12, color: 'var(--text-primary)', fontWeight: 500 }}>
          {task.original_intent.slice(0, 55)}{task.original_intent.length > 55 ? '…' : ''}
        </span>
        <RiskBadge level={task.highest_risk} />
        <ChevronRight size={12} style={{
          color: 'var(--text-muted)',
          transform: expanded ? 'rotate(90deg)' : 'none',
          transition: 'transform 0.2s',
        }} />
      </button>

      {/* Step list */}
      {expanded && (
        <div style={{ borderTop: '1px solid var(--border-subtle)', padding: '4px 0' }}>
          {task.steps.map((step: any, i: number) => (
            <StepRow key={i} step={step} />
          ))}
          {task.steps.length === 0 && (
            <div style={{ padding: '6px 10px', fontSize: 11, color: 'var(--text-muted)' }}>
              Waiting for steps…
            </div>
          )}
        </div>
      )}
    </div>
  );
};

const StepRow: React.FC<{ step: any }> = ({ step }) => {
  const icon = step.status === 'running' ? <Loader2 size={11} className="spin" style={{ color: 'var(--accent-secondary)' }} />
    : step.status === 'success' ? <CheckCircle size={11} color="var(--accent-primary)" />
    : step.status === 'blocked' ? <Shield size={11} color="var(--error)" />
    : step.status === 'failed' ? <XCircle size={11} color="var(--error)" />
    : step.status === 'awaiting_approval' ? <Lock size={11} color="var(--warning)" />
    : <Activity size={11} color="var(--text-muted)" />;

  return (
    <div style={{
      display: 'flex',
      alignItems: 'flex-start',
      gap: 8,
      padding: '4px 10px',
      borderBottom: '1px solid var(--border-subtle)',
      opacity: step.status === 'pending' ? 0.4 : 1,
      transition: 'opacity 0.2s',
    }}>
      <span style={{ marginTop: 1, flexShrink: 0 }}>{icon}</span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 11, color: 'var(--text-primary)', lineHeight: 1.3 }}>
          {step.description || step.command_type}
        </div>
        {step.error && (
          <div style={{ fontSize: 10, color: 'var(--error)', marginTop: 2 }}>
            {step.error.slice(0, 100)}
          </div>
        )}
        {step.output_summary && step.status === 'success' && (
          <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 2 }}>
            {step.output_summary.slice(0, 80)}
          </div>
        )}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 4, flexShrink: 0 }}>
        <RiskBadge level={step.risk_level} />
        {step.duration_ms && (
          <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>
            {step.duration_ms.toFixed(0)}ms
          </span>
        )}
      </div>
    </div>
  );
};

// ──────────────────────────────────────────────────────────────────────────────
// Approvals Tab
// ──────────────────────────────────────────────────────────────────────────────

const ApprovalsTab: React.FC<{
  typed: any[];
  legacy: ActionLog[];
  onApprove: (id: string, approved: boolean) => void;
}> = ({ typed, legacy, onApprove }) => {
  if (typed.length === 0 && legacy.length === 0) {
    return <EmptyState icon="✅" text="No pending approvals — all clear" />;
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10, padding: '8px 0' }}>
      {/* Typed approvals (Phase 1.4+) */}
      {typed.map((approval) => (
        <ApprovalCard key={approval.action_id} approval={approval} onApprove={onApprove} typed />
      ))}

      {/* Legacy approvals */}
      {legacy.map((action) => (
        <ApprovalCard key={action.id} approval={{
          action_id: action.id,
          command_type: action.action_type,
          description: action.description,
          risk_level: action.threat_level as RiskLevel,
          reason_for_approval: 'Action requires your approval before execution',
        }} onApprove={onApprove} typed={false} />
      ))}
    </div>
  );
};

const ApprovalCard: React.FC<{
  approval: any;
  onApprove: (id: string, approved: boolean) => void;
  typed: boolean;
}> = ({ approval, onApprove, typed }) => (
  <div style={{
    background: 'var(--bg-elevated)',
    border: '1px solid var(--warning)',
    borderRadius: 10,
    padding: 12,
    boxShadow: '0 0 16px rgba(245, 158, 11, 0.08)',
  }}>
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6 }}>
      <Lock size={13} color="var(--warning)" />
      <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--warning)' }}>
        Approval Required
      </span>
      <RiskBadge level={approval.risk_level || 'high'} />
    </div>

    <div style={{ fontSize: 12, color: 'var(--text-primary)', marginBottom: 4 }}>
      {approval.description}
    </div>
    <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 10 }}>
      {approval.reason_for_approval}
    </div>

    <div style={{ display: 'flex', gap: 8 }}>
      <button
        id={`approve-${approval.action_id}`}
        onClick={() => onApprove(approval.action_id, true)}
        style={{
          flex: 1,
          padding: '6px 0',
          borderRadius: 6,
          border: 'none',
          background: 'var(--accent-primary)',
          color: 'white',
          fontSize: 12,
          fontWeight: 600,
          cursor: 'pointer',
          transition: 'opacity 0.15s',
        }}
      >
        ✓ Approve
      </button>
      <button
        id={`deny-${approval.action_id}`}
        onClick={() => onApprove(approval.action_id, false)}
        style={{
          flex: 1,
          padding: '6px 0',
          borderRadius: 6,
          border: '1px solid var(--error)',
          background: 'transparent',
          color: 'var(--error)',
          fontSize: 12,
          fontWeight: 600,
          cursor: 'pointer',
        }}
      >
        ✗ Deny
      </button>
    </div>
  </div>
);

// ──────────────────────────────────────────────────────────────────────────────
// Security Tab (Guard pipeline live view + Audit Log)
// ──────────────────────────────────────────────────────────────────────────────

const SecurityTab: React.FC<{ checks: any[]; auditLog: any[] }> = ({ checks, auditLog }) => {
  const [showAudit, setShowAudit] = useState(false);

  return (
    <div style={{ padding: '8px 0' }}>
      {/* Guard Pipeline header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '0 0 8px' }}>
        <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.5px' }}>
          Guard Pipeline Results
        </span>
        <button
          onClick={() => setShowAudit(!showAudit)}
          style={{
            display: 'flex', alignItems: 'center', gap: 4,
            padding: '2px 8px', borderRadius: 6,
            border: '1px solid var(--border-subtle)',
            background: 'transparent',
            color: 'var(--text-muted)',
            fontSize: 10, cursor: 'pointer',
          }}
        >
          {showAudit ? <EyeOff size={10} /> : <Eye size={10} />}
          {showAudit ? 'Guard' : 'Audit Log'}
        </button>
      </div>

      {!showAudit ? (
        checks.length === 0 ? (
          <EmptyState icon="🛡" text="No guard checks yet — pipeline is idle" />
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            {checks.slice(0, 30).map((check, i) => (
              <GuardCheckRow key={i} check={check} />
            ))}
          </div>
        )
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          {auditLog.length === 0 ? (
            <EmptyState icon="📋" text="Audit log is empty" />
          ) : (
            auditLog.slice(0, 30).map((entry, i) => (
              <AuditRow key={i} entry={entry} />
            ))
          )}
        </div>
      )}
    </div>
  );
};

const GuardCheckRow: React.FC<{ check: any }> = ({ check }) => (
  <div style={{
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    padding: '5px 8px',
    borderRadius: 6,
    background: check.passed ? 'rgba(16, 185, 129, 0.06)' : 'rgba(239, 68, 68, 0.06)',
    border: `1px solid ${check.passed ? 'rgba(16, 185, 129, 0.15)' : 'rgba(239, 68, 68, 0.2)'}`,
  }}>
    {check.passed
      ? <CheckCircle size={11} color="var(--accent-primary)" />
      : <XCircle size={11} color="var(--error)" />
    }
    <div style={{ flex: 1, minWidth: 0 }}>
      <div style={{ fontSize: 11, fontWeight: 500, color: 'var(--text-primary)' }}>
        {GUARD_CHECK_LABELS[check.check_name as GuardCheckName] || check.check_name}
      </div>
      <div style={{ fontSize: 10, color: 'var(--text-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
        {check.reason}
      </div>
    </div>
    <div style={{ display: 'flex', alignItems: 'center', gap: 4, flexShrink: 0 }}>
      <RiskBadge level={check.risk_level} />
      <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>{check.duration_ms?.toFixed(1)}ms</span>
    </div>
  </div>
);

const AuditRow: React.FC<{ entry: any }> = ({ entry }) => {
  const color = entry.verdict === 'blocked' ? 'var(--error)'
    : entry.verdict === 'requires_approval' ? 'var(--warning)'
    : 'var(--accent-primary)';

  return (
    <div style={{
      padding: '5px 8px',
      borderRadius: 6,
      background: 'var(--bg-elevated)',
      border: '1px solid var(--border-subtle)',
      fontSize: 11,
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span style={{ color, fontWeight: 600, textTransform: 'uppercase', fontSize: 10 }}>
          {entry.verdict}
        </span>
        <span style={{ color: 'var(--text-muted)', fontSize: 10 }}>
          #{entry.sequence}
        </span>
      </div>
      <div style={{ color: 'var(--text-muted)', marginTop: 2, fontSize: 10 }}>
        Hash: <code style={{ fontFamily: 'monospace' }}>{entry.entry_hash?.slice(0, 16)}…</code>
      </div>
    </div>
  );
};

// ──────────────────────────────────────────────────────────────────────────────
// Threats Tab
// ──────────────────────────────────────────────────────────────────────────────

const ThreatsTab: React.FC<{ actions: ActionLog[] }> = ({ actions }) => {
  if (actions.length === 0) {
    return <EmptyState icon="🛡" text="No threats detected — system is clean" />;
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8, padding: '8px 0' }}>
      {actions.map((action) => (
        <ThreatCard key={action.id} action={action} />
      ))}
    </div>
  );
};

const ThreatCard: React.FC<{ action: ActionLog }> = ({ action }) => {
  const tlColor = action.threat_level === 'critical' ? 'var(--error)'
    : action.threat_level === 'high' ? 'var(--warning)'
    : 'var(--accent-tertiary)';

  return (
    <div style={{
      padding: 10,
      background: 'var(--bg-elevated)',
      borderRadius: 8,
      border: `1px solid ${tlColor}55`,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
        <AlertTriangle size={12} color={tlColor} />
        <span style={{ fontSize: 12, fontWeight: 600, color: tlColor, textTransform: 'uppercase' }}>
          {action.threat_level}
        </span>
        <span style={{ fontSize: 10, color: 'var(--text-muted)', marginLeft: 'auto' }}>
          {action.action_type}
        </span>
      </div>
      <div style={{ fontSize: 12, color: 'var(--text-primary)' }}>
        {action.description}
      </div>
    </div>
  );
};

// ──────────────────────────────────────────────────────────────────────────────
// Legacy action row (backward compat)
// ──────────────────────────────────────────────────────────────────────────────

const LegacyActionRow: React.FC<{ action: ActionLog }> = ({ action }) => {
  const icon = action.status === 'completed' ? <CheckCircle size={12} color="var(--accent-primary)" />
    : action.status === 'failed' ? <XCircle size={12} color="var(--error)" />
    : action.status === 'running' ? <Loader2 size={12} className="spin" />
    : action.status === 'awaiting_approval' ? <Lock size={12} color="var(--warning)" />
    : <Activity size={12} color="var(--text-muted)" />;

  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: 8,
      padding: '6px 8px',
      borderRadius: 6,
      background: 'var(--bg-elevated)',
      border: '1px solid var(--border-subtle)',
    }}>
      {icon}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 11, color: 'var(--text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {action.description}
        </div>
        <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>
          {action.action_type} · {action.status}
        </div>
      </div>
      <RiskBadge level={(action.threat_level as RiskLevel) || 'safe'} />
    </div>
  );
};

// ──────────────────────────────────────────────────────────────────────────────
// Empty state
// ──────────────────────────────────────────────────────────────────────────────

const EmptyState: React.FC<{ icon: string; text: string }> = ({ icon, text }) => (
  <div style={{
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    justifyContent: 'center',
    padding: '32px 16px',
    gap: 8,
    color: 'var(--text-muted)',
  }}>
    <span style={{ fontSize: 28 }}>{icon}</span>
    <p style={{ fontSize: 12, textAlign: 'center', lineHeight: 1.5 }}>{text}</p>
  </div>
);
