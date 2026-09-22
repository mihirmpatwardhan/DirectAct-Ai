import React from 'react';
import { Navbar } from '../components/Navbar/Navbar';
import { Footer } from '../components/Footer/Footer';

const TECH_STACK = [
  { icon: '⚛️', name: 'React 19' },
  { icon: '🐍', name: 'FastAPI' },
  { icon: '🧠', name: 'Gemini AI' },
  { icon: '🎭', name: 'Playwright' },
  { icon: '🔐', name: 'AES-256' },
  { icon: '🗄️', name: 'SQLAlchemy' },
  { icon: '📡', name: 'WebSocket' },
  { icon: '⚡', name: 'Vite' },
  { icon: '🛡️', name: 'AMSI Guard' },
  { icon: '🏗️', name: 'TypeScript' },
  { icon: '🔄', name: 'Zustand' },
  { icon: '🪟', name: 'Win32 API' },
];

export function AboutPage() {
  return (
    <div className="public-page">
      <Navbar />

      {/* Hero */}
      <div className="about-hero">
        <h1>
          About <span className="gradient-text">DirectAct-AI</span>
        </h1>
        <p>
          DirectAct-AI (Lakshya-AI Engine) is a next-generation autonomous desktop
          and web agent platform designed to turn high-level human natural language
          goals into secure, autonomous execution streams on host operating systems.
        </p>

        <div className="about-stats">
          <div className="stat-card">
            <div className="stat-number">8</div>
            <div className="stat-label">Security Guard Layers</div>
          </div>
          <div className="stat-card">
            <div className="stat-number">2</div>
            <div className="stat-label">Automation Modes</div>
          </div>
          <div className="stat-card">
            <div className="stat-number">AES-256</div>
            <div className="stat-label">Vault Encryption</div>
          </div>
          <div className="stat-card">
            <div className="stat-number">Zero</div>
            <div className="stat-label">Trust Architecture</div>
          </div>
        </div>
      </div>

      {/* Mission */}
      <section className="section">
        <div className="section-header">
          <div className="section-label">🎯 Our Mission</div>
          <h2 className="section-title">Security-First AI Execution</h2>
          <p className="section-subtitle">
            Unlike traditional LLM wrappers that execute arbitrary code,
            DirectAct-AI operates on a Deterministic Zero-Trust Execution Model.
            Every prompt is mapped through immutable action schemas, routed through
            a non-LLM security pipeline, and logged in a tamper-evident hash-chained
            audit ledger.
          </p>
        </div>

        <div className="features-grid">
          <div className="feature-card">
            <div className="feature-icon">🏗️</div>
            <h3 className="feature-title">Chain of Responsibility</h3>
            <p className="feature-desc">
              8-step MalwareGuard pipeline where each security check can
              independently halt execution. No single point of failure.
            </p>
          </div>

          <div className="feature-card">
            <div className="feature-icon">⚙️</div>
            <h3 className="feature-title">Strategy Pattern</h3>
            <p className="feature-desc">
              Dynamic OS engine selection — Windows, Linux, macOS — detected
              at runtime via PlatformProbe with specialized automation drivers.
            </p>
          </div>

          <div className="feature-card">
            <div className="feature-icon">🔄</div>
            <h3 className="feature-title">Saga Orchestration</h3>
            <p className="feature-desc">
              Multi-step task execution with rollback capabilities.
              Each step is independently validated and audited.
            </p>
          </div>
        </div>
      </section>

      {/* Tech Stack */}
      <section className="section">
        <div className="section-header">
          <div className="section-label">🛠️ Tech Stack</div>
          <h2 className="section-title">Built with Modern Technology</h2>
          <p className="section-subtitle">
            Enterprise-grade technology stack for maximum performance and security.
          </p>
        </div>

        <div className="tech-grid">
          {TECH_STACK.map((tech) => (
            <div className="tech-item" key={tech.name}>
              <div className="tech-item-icon">{tech.icon}</div>
              <div className="tech-item-name">{tech.name}</div>
            </div>
          ))}
        </div>
      </section>

      {/* Architecture */}
      <section className="section" style={{ textAlign: 'center' }}>
        <div className="section-header">
          <div className="section-label">📐 Architecture</div>
          <h2 className="section-title">How It All Connects</h2>
          <p className="section-subtitle">
            Clean separation of concerns — LLM intelligence never touches system execution directly.
          </p>
        </div>

        <div style={{
          maxWidth: 700,
          margin: '0 auto',
          padding: '32px',
          borderRadius: 'var(--pub-radius-lg)',
          background: 'var(--pub-glass)',
          border: '1px solid var(--pub-glass-border)',
          fontFamily: "'JetBrains Mono', monospace",
          fontSize: '0.82rem',
          lineHeight: 1.8,
          textAlign: 'left',
          color: 'var(--pub-text-secondary)',
          overflowX: 'auto',
        }}>
          <pre style={{ margin: 0 }}>{`  [ User UI (React + Zustand) ]
            │ (WebSocket / REST)
            ▼
  [ FastAPI Server / Router ]
            │
            ▼
  [ Hybrid Task Router ] ──► (Regex / LLM)
            │
            ▼
  [ Saga Execution Orchestrator ]
            │
            ▼
  ┌──────────────────────────────────┐
  │ 🛡️ MALWARE GUARD PIPELINE       │
  │  8-Step Chain of Responsibility  │
  └──────────────────────────────────┘
            │ (Pass Verdict)
            ▼
  ┌──────────────────────────────────┐
  │ ⚙️ EXECUTION ENGINES             │
  │  ├── Windows (PowerShell/Win32)  │
  │  └── Web (Playwright/Chromium)   │
  └──────────────────────────────────┘`}</pre>
        </div>
      </section>

      <Footer />
    </div>
  );
}
