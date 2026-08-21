"use client";

/**
 * Shared data access.
 *
 * One hook, so every screen reports loading and failure the same way instead of
 * each inventing its own. `refresh` is exposed rather than polling: this is an
 * operator tool where a row changing under the cursor mid-review is worse than
 * a slightly stale list.
 *
 * `enabled` is load-bearing, not an optimisation. These hooks live above the
 * sign-in gate so their data is ready the moment the shell mounts, which means
 * without a gate they fire while there is still no bearer token — the requests
 * come back 401, the failure is cached into `error`, and the 401 handler fires
 * a sign-out event at the user who just signed in.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { apiJson } from "./api";
import type { Reference, RunRecord } from "./types";

export interface Async<T> {
  data: T | null;
  loading: boolean;
  /** Null unless the last attempt failed. Never a raw exception. */
  error: string | null;
  refresh: () => void;
}

function describe(err: unknown): string {
  if (err instanceof Error && err.message) return err.message;
  return "The server could not be reached.";
}

/** Shared fetch-with-state, so the two hooks below cannot drift apart. */
function useResource<T>(path: string, enabled: boolean, reloadKey: number): Async<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    if (!enabled) return;

    // Keep previous rows on screen while refetching; blanking a populated table
    // to skeletons on every refresh is disorienting.
    setError(null);
    let cancelled = false;

    apiJson<T>(path)
      .then((d) => !cancelled && alive.current && setData(d))
      .catch((e) => !cancelled && alive.current && setError(describe(e)))
      .finally(() => !cancelled && alive.current && setLoading(false));

    return () => {
      cancelled = true;
    };
  }, [path, enabled, reloadKey, nonce]);

  return {
    data,
    // Before it is enabled the resource is not idle — it is waiting on auth, and
    // reporting "loaded, empty" there would flash an empty state at sign-in.
    loading: !enabled || loading,
    error,
    refresh: useCallback(() => setNonce((n) => n + 1), []),
  };
}

export const useRuns = (reloadKey = 0, enabled = true) =>
  useResource<RunRecord[]>("/api/runs", enabled, reloadKey);

export const useReference = (enabled = true) =>
  useResource<Reference>("/api/reference", enabled, 0);

/* ------------------------------------------------------------- analytics
 * Phase H. One hook per endpoint, all sharing `useResource` above, so an
 * analytics panel reports loading and failure exactly the way every other
 * screen already does.
 *
 * The range is part of the PATH rather than a separate argument, which means
 * changing it changes `path` and `useResource`'s effect re-fires on its own --
 * no extra dependency to remember and no stale window left on screen under a
 * new label.
 */
export type RangeKey = "today" | "7d" | "30d" | "month" | "all";

const analyticsPath = (name: string, range: RangeKey) =>
  `/api/analytics/${name}?range=${encodeURIComponent(range)}`;

export const useAnalytics = <T,>(
  name: string,
  range: RangeKey,
  enabled = true,
  reloadKey = 0
) => useResource<T>(analyticsPath(name, range), enabled, reloadKey);
