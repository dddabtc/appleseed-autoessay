import { createContext, useContext } from 'react';

export interface AuthUser {
  id: string;
  username: string | null;
  email: string | null;
  display_name: string | null;
  picture_url: string | null;
}

export type LoginResult =
  | { ok: true }
  | {
      ok: false;
      // ``code`` is one of the discriminators the LoginPage maps to
      // i18n strings: ``invalid_credentials`` (401), ``rate_limited``
      // (429), ``network`` (fetch threw / non-JSON response).
      code: 'invalid_credentials' | 'rate_limited' | 'network';
      retryAfterSeconds?: number;
    };

export interface AuthContextValue {
  user: AuthUser | null;
  isLoading: boolean;
  login: (username: string, password: string) => Promise<LoginResult>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
}

export const AuthContext = createContext<AuthContextValue | null>(null);

export function useAuth() {
  const value = useContext(AuthContext);
  if (value === null) {
    throw new Error('useAuth must be used inside AuthProvider');
  }
  return value;
}
