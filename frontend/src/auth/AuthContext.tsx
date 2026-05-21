import { ReactNode, useCallback, useEffect, useMemo, useState } from 'react';
import { Navigate, useLocation } from 'react-router';

import { AuthContext, AuthUser, LoginResult, useAuth } from './AuthState';

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  const refresh = useCallback(async () => {
    setIsLoading(true);
    try {
      const response = await fetch('/api/auth/me', {
        credentials: 'same-origin',
        headers: { Accept: 'application/json' }
      });
      if (response.status === 401) {
        setUser(null);
        return;
      }
      if (!response.ok) {
        throw new Error(`Auth check failed: ${response.status}`);
      }
      setUser((await response.json()) as AuthUser);
    } catch {
      setUser(null);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const login = useCallback(
    async (username: string, password: string): Promise<LoginResult> => {
      let response: Response;
      try {
        response = await fetch('/api/auth/login', {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'Content-Type': 'application/json',
            Accept: 'application/json'
          },
          body: JSON.stringify({ username, password })
        });
      } catch {
        return { ok: false, code: 'network' };
      }
      if (response.status === 429) {
        const retryAfter = Number(response.headers.get('Retry-After') ?? '0');
        return {
          ok: false,
          code: 'rate_limited',
          retryAfterSeconds: Number.isFinite(retryAfter) ? retryAfter : undefined
        };
      }
      if (response.status === 401) {
        return { ok: false, code: 'invalid_credentials' };
      }
      if (!response.ok) {
        return { ok: false, code: 'network' };
      }
      try {
        const body = (await response.json()) as { user: AuthUser };
        setUser(body.user);
        return { ok: true };
      } catch {
        return { ok: false, code: 'network' };
      }
    },
    []
  );

  const logout = useCallback(async () => {
    await fetch('/api/auth/logout', {
      method: 'POST',
      credentials: 'same-origin'
    });
    setUser(null);
    window.location.assign('/login');
  }, []);

  const value = useMemo(
    () => ({
      user,
      isLoading,
      login,
      logout,
      refresh
    }),
    [user, isLoading, login, logout, refresh]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function AuthGate({ children }: { children: ReactNode }) {
  const { user, isLoading } = useAuth();
  const location = useLocation();
  if (isLoading) {
    return (
      <section
        className="flex min-h-60 items-center justify-center"
        aria-live="polite"
      >
        <span
          className="inline-block h-8 w-8 animate-spin rounded-full border-[3px] border-slate-200 border-t-[#114b5f]"
          aria-hidden="true"
        />
      </section>
    );
  }
  if (!user) {
    return (
      <Navigate
        to="/login"
        replace
        state={{ next: `${location.pathname}${location.search}` }}
      />
    );
  }
  return <>{children}</>;
}
