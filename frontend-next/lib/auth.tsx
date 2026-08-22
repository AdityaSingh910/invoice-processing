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
import { ApiError, loadIdentity, readToken, signIn as apiSignIn, writeToken } from "./api";
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

  /**
   * Resume an existing session if the tab still holds a usable token.
   *
   * THREE STATES, NOT TWO -- and getting that wrong here is what signed people
   * out. This used to `writeToken(null)` on ANY failure, with a comment saying
   * "expired or revoked". A 500, a 429, a dropped connection and a request torn
   * down by the reload that superseded it all landed in that same catch, and
   * every one of them destroyed a perfectly good token. Pressing the browser's
   * reload button several times quickly was enough to do it: the bursts raced
   * each other into the API, one came back 500, and the session was gone.
   *
   * "The token is invalid" and "I could not check right now" are different
   * facts -- the same distinction Phase F insists on for an authentication
   * result (pass / fail / UNAVAILABLE, §7a.4), applied to our own session.
   * Only the first ends a session, and `apiFetch` already recognises it: a 401
   * clears the token and announces it, and the listener below signs out. So
   * everything reaching this catch is, by construction, the second kind.
   *
   * A transient failure is therefore RETRIED, and if it still will not settle
   * the token is LEFT ALONE. The user lands on the sign-in screen either way --
   * there is no identity to render a shell with -- but their token survives, so
   * a reload picks the session straight back up instead of demanding a
   * password the server never actually rejected.
   */
  useEffect(() => {
    let cancelled = false;

    (async () => {
      if (!readToken()) {
        if (!cancelled) setReady(true);
        return;
      }

      // Three attempts over roughly a second. Enough to ride out a burst
      // colliding at the API; short enough that a genuinely unreachable server
      // still shows the gate promptly rather than hanging on a spinner.
      for (let attempt = 0; attempt < 3; attempt++) {
        try {
          const me = await loadIdentity();
          if (!cancelled) setUser(me);
          break;
        } catch (err) {
          // A 401 means the token really is finished. apiFetch has already
          // cleared it and fired the sign-out event; retrying would only ask
          // the same question again with no credential at all.
          if (err instanceof ApiError && err.status === 401) break;
          if (cancelled) return;
          if (attempt === 2) break;          // out of attempts; keep the token
          await new Promise((r) => setTimeout(r, 250 * (attempt + 1)));
        }
      }

      if (!cancelled) setReady(true);
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
