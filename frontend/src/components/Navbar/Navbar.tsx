import React, { useState, useEffect } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { useAuthStore } from '../../store/authStore';

export function Navbar() {
  const [scrolled, setScrolled] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const location = useLocation();
  const navigate = useNavigate();
  const { isAuthenticated, user, logout } = useAuthStore();

  useEffect(() => {
    const handleScroll = () => setScrolled(window.scrollY > 20);
    window.addEventListener('scroll', handleScroll);
    return () => window.removeEventListener('scroll', handleScroll);
  }, []);

  // Close mobile nav on route change
  useEffect(() => {
    setMobileOpen(false);
  }, [location.pathname]);

  const isActive = (path: string) => location.pathname === path ? 'active' : '';

  const handleLogout = () => {
    logout();
    navigate('/');
  };

  return (
    <>
      <nav className={`nav-bar ${scrolled ? 'scrolled' : ''}`}>
        <Link to="/" className="nav-logo">
          <div className="nav-logo-icon">⚡</div>
          <div className="nav-logo-text">
            Direct<span>Act</span>-AI
          </div>
        </Link>

        <ul className="nav-links">
          <li><Link to="/" className={isActive('/')}>Home</Link></li>
          <li><Link to="/about" className={isActive('/about')}>About</Link></li>
          <li><Link to="/contact" className={isActive('/contact')}>Contact</Link></li>
        </ul>

        <div className="nav-auth">
          {isAuthenticated ? (
            <>
              <Link to="/dashboard" className="btn-nav-dashboard">⚡ Dashboard</Link>
              <div className="nav-user">
                <div className="nav-avatar">{user?.name?.charAt(0)?.toUpperCase() || 'U'}</div>
                <span className="nav-user-name">{user?.name || 'User'}</span>
              </div>
              <button className="btn-nav-logout" onClick={handleLogout}>Logout</button>
            </>
          ) : (
            <>
              <Link to="/login" className="btn-nav-login">Log In</Link>
              <Link to="/signup" className="btn-nav-signup">Get Started</Link>
            </>
          )}
        </div>

        <button
          className="nav-hamburger"
          onClick={() => setMobileOpen(!mobileOpen)}
          aria-label="Toggle menu"
        >
          {mobileOpen ? '✕' : '☰'}
        </button>
      </nav>

      {/* Mobile Menu */}
      <div className={`nav-mobile ${mobileOpen ? 'open' : ''}`}>
        <Link to="/" className={isActive('/')}>Home</Link>
        <Link to="/about" className={isActive('/about')}>About</Link>
        <Link to="/contact" className={isActive('/contact')}>Contact</Link>
        {isAuthenticated ? (
          <>
            <Link to="/dashboard" className="btn-nav-signup">⚡ Dashboard</Link>
            <a href="#" onClick={(e) => { e.preventDefault(); handleLogout(); }}>Logout</a>
          </>
        ) : (
          <>
            <Link to="/login">Log In</Link>
            <Link to="/signup" className="btn-nav-signup">Get Started</Link>
          </>
        )}
      </div>
    </>
  );
}
