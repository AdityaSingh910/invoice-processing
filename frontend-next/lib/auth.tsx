"use client";

/**
 * Session state for the browser tab.
 *
 * `can()` mirrors the scopes the server put in the token so the UI can hide an
 * action the user's token would not carry. That is courtesy, not enforcement:
 * every endpoint re-checks the same scope, so a hidden button and a forged
 * click reach the same 403.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { loadIdentity, readToken, signIn as apiSignIn, writeToken } from "./api";
import type { Identity } from "./types";

interface AuthState {
  user: Identity | null;
  /** Null while the stored token is still being validated on first paint. */
  ready: boolean;
  signIn: (username: string, password: string) => Promise<void>;
  signOut: (message?: string) => void;
  can: (scope: string) => boolean;
  notice: string | null;
}

const Ctx = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<Identity | null>(null);
  const [ready, setReady] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const signOut = useCallback((message?: string) => {
    writeToken(null);
    setUser(null);
    setNotice(message ?? null);
  }, []);

  // Resume an existing session if the tab still holds a usable token.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!readToken()) {
        if (!cancelled) setReady(true);
        return;
      }
      try {
        const me = await loadIdentity();
        if (!cancelled) setUser(me);
      } catch {
        writeToken(null);          // expired or revoked; fall back to the gate
      } finally {
        if (!cancelled) setReady(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // A 401 from anywhere in the app means this session is over. Listening for
  // an event keeps every caller from having to know about sign-out.
  useEffect(() => {
    const onExpired = () => signOut("Your session has expired. Please sign in again.");
    window.addEventListener("ip:unauthenticated", onExpired);
    return () => window.removeEventListener("ip:unauthenticated", onExpired);
  }, [signOut]);

  const doSignIn = useCallback(async (username: string, password: string) => {
    await apiSignIn(username, password);
    setUser(await loadIdentity());
    setNotice(null);
  }, []);

  const value = useMemo<AuthState>(
    () => ({
      user,
      ready,
      notice,
      signIn: doSignIn,
      signOut,
      can: (scope) => !!user?.scopes?.includes(scope),
    }),
    [user, ready, notice, doSignIn, signOut]
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
