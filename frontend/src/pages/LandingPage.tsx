import React from 'react';
import { Link } from 'react-router-dom';
import { Navbar } from '../components/Navbar/Navbar';
import { Footer } from '../components/Footer/Footer';

function Particles() {
  return (
    <div className="particles">
      {Array.from({ length: 8 }).map((_, i) => (
        <div key={i} className="particle" />
      ))}
    </div>
  );
}

export function LandingPage() {
  return (
    <div className="public-page">
      <Navbar />
      <Particles />

      {/* ── Hero Section ── */}
      <section className="hero">
        <div className="hero-3d-cards">
          <div className="float-card">
            <div className="float-card-icon">🌐</div>
            <div className="float-card-label">Web Automation</div>
          </div>
          <div className="float-card">
            <div className="float-card-icon">🖥️</div>
            <div className="float-card-label">Desktop Control</div>
          </div>
          <div className="float-card">
            <div className="float-card-icon">🛡️</div>
            <div className="float-card-label">Security First</div>
          </div>
          <div className="float-card">
            <div className="float-card-icon">🧠</div>
            <div className="float-card-label">Gemini Engine</div>
          </div>
        </div>

        <div className="hero-badge">
          <div className="hero-badge-dot" />
          Powered by Google Gemini 2.5
        </div>

        <h1>
          Define. Direct.<br />
          <span className="gradient-text">Done.</span>
        </h1>

        <p className="hero-subtitle">
          Turn your natural language goals into fully autonomous browser &amp;
          desktop actions — secured by 8-layer malware guard pipeline
          with zero-trust execution.
        </p>

        <div className="hero-cta">
          <Link to="/signup" className="btn-primary">
            🚀 Get Started Free
          </Link>
          <Link to="/about" className="btn-secondary">
            Learn More →
          </Link>
        </div>
      </section>

      {/* ── Features Section ── */}
      <section className="section" id="features">
        <div className="section-header">
          <div className="section-label">✨ Features</div>
          <h2 className="section-title">
            Everything You Need for<br />
            <span className="gradient-text">Autonomous Automation</span>
          </h2>
          <p className="section-subtitle">
            From browser tasks to desktop control — DirectAct-AI handles it all
            with enterprise-grade security.
          </p>
        </div>

        <div className="features-grid">
          <div className="feature-card">
            <div className="feature-icon">🌐</div>
            <h3 className="feature-title">Web Browser Automation</h3>
            <p className="feature-desc">
              Login to any website, search, fill forms, scrape data — all through
              natural language commands. Uses your own browser profile.
            </p>
          </div>

          <div className="feature-card">
            <div className="feature-icon">🖥️</div>
            <h3 className="feature-title">PC Desktop Automation</h3>
            <p className="feature-desc">
              Open apps, manage files, control windows — native OS automation
              powered by PowerShell &amp; Win32 APIs.
            </p>
          </div>

          <div className="feature-card">
            <div className="feature-icon">🛡️</div>
            <h3 className="feature-title">8-Layer Security Guard</h3>
            <p className="feature-desc">
              Every command passes through policy validation, AST analysis, AMSI scan,
              sandboxing, and hash-chained audit — zero-trust by design.
            </p>
          </div>

          <div className="feature-card">
            <div className="feature-icon">📡</div>
            <h3 className="feature-title">Real-Time Streaming</h3>
            <p className="feature-desc">
              Watch every step live via WebSocket. See screenshots, execution logs,
              and security verdicts in real-time on your dashboard.
            </p>
          </div>

          <div className="feature-card">
            <div className="feature-icon">🧠</div>
            <h3 className="feature-title">Gemini AI Engine</h3>
            <p className="feature-desc">
              Powered by Google Gemini 2.5 with circuit-breaker key rotation
              and multi-provider fallback for 99.9% uptime.
            </p>
          </div>

          <div className="feature-card">
            <div className="feature-icon">🔐</div>
            <h3 className="feature-title">Encrypted Vault</h3>
            <p className="feature-desc">
              Your credentials stored with AES-256-GCM encryption.
              Login once — automation uses your credentials securely.
            </p>
          </div>
        </div>
      </section>

      {/* ── How It Works ── */}
      <section className="section">
        <div className="section-header">
          <div className="section-label">🔄 How It Works</div>
          <h2 className="section-title">Three Simple Steps</h2>
          <p className="section-subtitle">
            From intent to execution in seconds.
          </p>
        </div>

        <div className="steps-container">
          <div className="step-item">
            <div className="step-dot" />
            <div className="step-number">Step 01</div>
            <h3 className="step-title">Sign Up &amp; Login</h3>
            <p className="step-desc">
              Create your account with your email. Your credentials are encrypted
              and stored securely in the vault — used for both app access and
              browser automation.
            </p>
          </div>

          <div className="step-item">
            <div className="step-dot" />
            <div className="step-number">Step 02</div>
            <h3 className="step-title">Define Your Intent</h3>
            <p className="step-desc">
              Tell DirectAct-AI what you want in plain English or Marathi.
              "Open Amazon and search for laptop under 50000" or
              "Open VS Code and create a new project."
            </p>
          </div>

          <div className="step-item">
            <div className="step-dot" />
            <div className="step-number">Step 03</div>
            <h3 className="step-title">Watch It Execute</h3>
            <p className="step-desc">
              Sit back as the AI agent executes your task autonomously.
              Watch live screenshots, security checks, and execution logs
              streaming to your dashboard in real-time.
            </p>
          </div>
        </div>
      </section>

      {/* ── CTA Section ── */}
      <section className="section" style={{ textAlign: 'center', paddingBottom: 120 }}>
        <h2 className="section-title" style={{ marginBottom: 16 }}>
          Ready to <span className="gradient-text">Automate Everything?</span>
        </h2>
        <p className="section-subtitle" style={{ marginBottom: 40 }}>
          Join DirectAct-AI and experience the future of autonomous computing.
        </p>
        <Link to="/signup" className="btn-primary" style={{ fontSize: '1.1rem', padding: '18px 48px' }}>
          🚀 Start Now — It's Free
        </Link>
      </section>

      <Footer />
    </div>
  );
}
