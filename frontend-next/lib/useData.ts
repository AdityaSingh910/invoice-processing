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
import { useCallback, useEffect, useState } from "react";
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

/**
 * Shared fetch-with-state, so the hooks below cannot drift apart.
 *
 * THE REQUEST IS ABORTED, NOT MERELY IGNORED. The previous version set a
 * `cancelled` flag in the effect cleanup and let the HTTP request run to
 * completion behind it. Superseding a request therefore cost a real round trip
 * every time -- and this application meters exactly these routes: every
 * /api/analytics/* and /api/logs* endpoint sits behind the reporting limiter
 * (§7e.4), which counts per user AND per IP. Changing the Analytics range four
 * times fires 28 requests and abandons 21 of them, all of which still count.
 * An AbortController makes an abandoned request stop being a request.
 *
 * ONE GATE, NOT TWO. There was also a component-lifetime `alive` ref checked
 * alongside `cancelled` before every state write, including the one that
 * clears `loading`. Two gates for one job, and the failure mode was ugly: any
 * path that left `alive` false while the effect was still current stranded the
 * panel on `loading: true` with no data and no error -- a skeleton that never
 * resolves and never says why. `cancelled` was always the more precise of the
 * two (it is per-effect-run, not per-component), and an aborted request needs
 * no flag at all, so the ref is gone. Writing state after unmount has been a
 * no-op since React 18; it was never the thing worth guarding.
 */
function useResource<T>(path: string, enabled: boolean, reloadKey: number): Async<T> {
  // One object, so a settled request cannot land as three separate renders --
  // and, more to the point, so `loading` and `data` can never disagree about
  // which request they came from.
  const [state, setState] = useState<{ data: T | null; loading: boolean; error: string | null }>({
    data: null,
    loading: true,
    error: null,
  });
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    if (!enabled) return;

    const controller = new AbortController();
    // Keep previous rows on screen while refetching; blanking a populated table
    // to skeletons on every refresh is disorienting. `loading` still goes true,
    // so a caller that wants to show a quiet refreshing state can.
    setState((s) => ({ data: s.data, loading: true, error: null }));

    apiJson<T>(path, { signal: controller.signal })
      .then((d) => setState({ data: d, loading: false, error: null }))
      .catch((e) => {
        // An abort is this hook superseding itself, not a failure. Reporting it
        // would put an error on screen for a request nobody is waiting for, and
        // the run that replaced it is already responsible for settling state.
        if (controller.signal.aborted) return;
        setState((s) => ({ data: s.data, loading: false, error: describe(e) }));
      });

    return () => controller.abort();
  }, [path, enabled, reloadKey, nonce]);

  return {
    data: state.data,
    // Before it is enabled the resource is not idle — it is waiting on auth, and
    // reporting "loaded, empty" there would flash an empty state at sign-in.
    loading: !enabled || state.loading,
    error: state.error,
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
