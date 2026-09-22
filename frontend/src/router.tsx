import React, { useEffect } from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { useAuthStore } from './store/authStore';

// Pages
import { LandingPage } from './pages/LandingPage';
import { LoginPage } from './pages/LoginPage';
import { SignupPage } from './pages/SignupPage';
import { AboutPage } from './pages/AboutPage';
import { ContactPage } from './pages/ContactPage';

// Dashboard (existing App layout)
import DashboardLayout from './App';

// Import public pages CSS
import './pages/pages.css';

/**
 * Protected Route — redirects to /login if not authenticated.
 *
 * FIX: Previous logic allowed through if `token` existed even while
 * `checkAuth` was still in-flight, causing a race where the dashboard
 * rendered before the token was validated. Now we show a loading spinner
 * whenever isLoading is true (regardless of token presence) and only
 * redirect once we know for certain the user is not authenticated.
 */
function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isLoading } = useAuthStore();

  // Show loading while auth state is being resolved
  if (isLoading) {
    return (
      <div className="loading-screen">
        <div className="spinner" />
      </div>
    );
  }

  // Only redirect once loading is complete and user is confirmed unauthenticated
  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }

  return <>{children}</>;
}

/**
 * App Router — wraps all routes and handles auth check on mount.
 */
export function AppRouter() {
  const { checkAuth, token } = useAuthStore();

  // Check auth on mount if we have a stored token
  useEffect(() => {
    if (token) {
      checkAuth();
    }
  }, []);

  return (
    <BrowserRouter>
      <Routes>
        {/* Public Routes */}
        <Route path="/" element={<LandingPage />} />
        <Route path="/login" element={<LoginPage />} />
        <Route path="/signup" element={<SignupPage />} />
        <Route path="/about" element={<AboutPage />} />
        <Route path="/contact" element={<ContactPage />} />

        {/* Protected Route — Dashboard */}
        <Route
          path="/dashboard"
          element={
            <ProtectedRoute>
              <DashboardLayout />
            </ProtectedRoute>
          }
        />

        {/* Catch-all → redirect to home */}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
