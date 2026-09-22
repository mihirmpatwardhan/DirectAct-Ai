import React, { useState } from 'react';
import { Navbar } from '../components/Navbar/Navbar';
import { Footer } from '../components/Footer/Footer';

export function ContactPage() {
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [message, setMessage] = useState('');
  const [submitted, setSubmitted] = useState(false);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    // In a real app, send to backend. For now, show success.
    setSubmitted(true);
    setTimeout(() => setSubmitted(false), 5000);
    setName('');
    setEmail('');
    setMessage('');
  };

  return (
    <div className="public-page">
      <Navbar />

      <section className="section" style={{ paddingTop: 140 }}>
        <div className="section-header">
          <div className="section-label">📞 Contact</div>
          <h2 className="section-title">Get In Touch</h2>
          <p className="section-subtitle">
            Have questions about DirectAct-AI? We'd love to hear from you.
          </p>
        </div>

        <div className="contact-container">
          {/* Left — Info */}
          <div className="contact-info">
            <h2>Let's <span className="gradient-text">Connect</span></h2>
            <p>
              Whether you have questions about features, need technical support,
              or want to explore partnership opportunities — our team is ready
              to help.
            </p>

            <div className="contact-detail">
              <div className="contact-detail-icon">📧</div>
              <div className="contact-detail-text">
                <h4>Email</h4>
                <p>support@directact-ai.com</p>
              </div>
            </div>

            <div className="contact-detail">
              <div className="contact-detail-icon">📍</div>
              <div className="contact-detail-text">
                <h4>Location</h4>
                <p>Maharashtra, India</p>
              </div>
            </div>

            <div className="contact-detail">
              <div className="contact-detail-icon">🕐</div>
              <div className="contact-detail-text">
                <h4>Response Time</h4>
                <p>Within 24 hours</p>
              </div>
            </div>

            <div className="contact-detail">
              <div className="contact-detail-icon">🔗</div>
              <div className="contact-detail-text">
                <h4>GitHub</h4>
                <p>github.com/DirectAct-AI</p>
              </div>
            </div>
          </div>

          {/* Right — Form */}
          <form className="contact-form-card" onSubmit={handleSubmit}>
            <h3>Send a Message</h3>

            {submitted && (
              <div className="success-msg">
                ✅ Message sent successfully! We'll get back to you soon.
              </div>
            )}

            <div className="form-group">
              <label className="form-label" htmlFor="contact-name">Your Name</label>
              <input
                id="contact-name"
                className="form-input"
                type="text"
                placeholder="John Doe"
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
              />
            </div>

            <div className="form-group">
              <label className="form-label" htmlFor="contact-email">Email Address</label>
              <input
                id="contact-email"
                className="form-input"
                type="email"
                placeholder="you@example.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
              />
            </div>

            <div className="form-group">
              <label className="form-label" htmlFor="contact-message">Message</label>
              <textarea
                id="contact-message"
                className="form-textarea"
                placeholder="Tell us how we can help..."
                value={message}
                onChange={(e) => setMessage(e.target.value)}
                required
              />
            </div>

            <button
              type="submit"
              className="btn-submit"
              disabled={!name || !email || !message}
            >
              📨 Send Message
            </button>
          </form>
        </div>
      </section>

      <Footer />
    </div>
  );
}
