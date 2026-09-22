import React from 'react';
import { Link } from 'react-router-dom';

export function Footer() {
  return (
    <footer className="pub-footer">
      <div className="footer-grid">
        <div className="footer-brand">
          <Link to="/" className="nav-logo" style={{ marginBottom: 8 }}>
            <div className="nav-logo-icon">⚡</div>
            <div className="nav-logo-text">
              Direct<span>Act</span>-AI
            </div>
          </Link>
          <p>
            Turning human goals into autonomous actions.
            AI-powered browser &amp; desktop automation with
            security-first architecture.
          </p>
        </div>

        <div className="footer-col">
          <h4>Product</h4>
          <Link to="/about">About</Link>
          <Link to="/#features">Features</Link>
          <Link to="/contact">Contact</Link>
        </div>

        <div className="footer-col">
          <h4>Platform</h4>
          <Link to="/login">Login</Link>
          <Link to="/signup">Sign Up</Link>
          <Link to="/dashboard">Dashboard</Link>
        </div>

        <div className="footer-col">
          <h4>Resources</h4>
          <a href="https://github.com" target="_blank" rel="noopener noreferrer">GitHub</a>
          <a href="#" onClick={(e) => e.preventDefault()}>Documentation</a>
          <a href="#" onClick={(e) => e.preventDefault()}>API Reference</a>
        </div>
      </div>

      <div className="footer-bottom">
        <span>© {new Date().getFullYear()} DirectAct-AI (Lakshya-AI Engine). All rights reserved.</span>
        <div className="footer-socials">
          <a href="https://github.com" target="_blank" rel="noopener noreferrer" title="GitHub">🔗</a>
          <a href="https://twitter.com" target="_blank" rel="noopener noreferrer" title="Twitter">𝕏</a>
          <a href="https://linkedin.com" target="_blank" rel="noopener noreferrer" title="LinkedIn">in</a>
        </div>
      </div>
    </footer>
  );
}
