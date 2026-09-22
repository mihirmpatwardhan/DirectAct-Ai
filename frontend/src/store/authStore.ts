/**
 * DirectAct-AI — Auth Store (Zustand)
 * Manages authentication state with localStorage persistence.
 */
import { create } from 'zustand';
import { immer } from 'zustand/middleware/immer';
import {
  apiRegister,
  apiLogin,
  apiGetMe,
  type AuthUser,
  type RegisterPayload,
  type LoginPayload,
} from '../lib/authApi';

interface AuthState {
  user: AuthUser | null;
  token: string | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  error: string | null;
}

interface AuthActions {
  register: (payload: RegisterPayload) => Promise<boolean>;
  login: (payload: LoginPayload) => Promise<boolean>;
  logout: () => void;
  checkAuth: () => Promise<void>;
  clearError: () => void;
}

const TOKEN_KEY = 'directact_token';

export const useAuthStore = create<AuthState & AuthActions>()(
  immer((set) => ({
    user: null,
    token: localStorage.getItem(TOKEN_KEY),
    isAuthenticated: false,
    isLoading: false,
    error: null,

    register: async (payload: RegisterPayload) => {
      set((s) => {
        s.isLoading = true;
        s.error = null;
      });
      try {
        const res = await apiRegister(payload);
        localStorage.setItem(TOKEN_KEY, res.token);
        set((s) => {
          s.user = res.user;
          s.token = res.token;
          s.isAuthenticated = true;
          s.isLoading = false;
        });
        return true;
      } catch (err: any) {
        const msg =
          err?.response?.data?.detail || 'Registration failed. Please try again.';
        set((s) => {
          s.isLoading = false;
          s.error = msg;
        });
        return false;
      }
    },

    login: async (payload: LoginPayload) => {
      set((s) => {
        s.isLoading = true;
        s.error = null;
      });
      try {
        const res = await apiLogin(payload);
        localStorage.setItem(TOKEN_KEY, res.token);
        set((s) => {
          s.user = res.user;
          s.token = res.token;
          s.isAuthenticated = true;
          s.isLoading = false;
        });
        return true;
      } catch (err: any) {
        const msg =
          err?.response?.data?.detail || 'Invalid email or password.';
        set((s) => {
          s.isLoading = false;
          s.error = msg;
        });
        return false;
      }
    },

    logout: () => {
      localStorage.removeItem(TOKEN_KEY);
      set((s) => {
        s.user = null;
        s.token = null;
        s.isAuthenticated = false;
        s.error = null;
      });
    },

    checkAuth: async () => {
      const token = localStorage.getItem(TOKEN_KEY);
      if (!token) {
        set((s) => {
          s.isAuthenticated = false;
          s.isLoading = false;
        });
        return;
      }
      set((s) => {
        s.isLoading = true;
      });
      try {
        const user = await apiGetMe();
        set((s) => {
          s.user = user;
          s.token = token;
          s.isAuthenticated = true;
          s.isLoading = false;
        });
      } catch {
        localStorage.removeItem(TOKEN_KEY);
        set((s) => {
          s.user = null;
          s.token = null;
          s.isAuthenticated = false;
          s.isLoading = false;
        });
      }
    },

    clearError: () => {
      set((s) => {
        s.error = null;
      });
    },
  }))
);
