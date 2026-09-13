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
import { ApiError, apiJson } from "./api";
import type { AnalyticsDashboard, Reference, RunRecord } from "./types";

export interface Async<T> {
  data: T | null;
  loading: boolean;
  /** Null unless the last attempt failed. Never a raw exception. */
  error: string | null;
  refresh: () => void;
  /**
   * Refetch without reporting `loading`, for a refresh nobody asked for.
   *
   * A background poll that flips `loading` makes every Refresh button on the
   * screen say "Refreshing…" and go disabled every few seconds, which reads as
   * a page that is permanently busy. A poll the user did not initiate should
   * be invisible until it has something new to show -- so this leaves
   * `loading` alone and lets the settled data swap itself in.
   *
   * It still reports errors, and it still goes through the same in-flight
   * coalescing `refresh` does, so polls cannot stack up behind a slow request.
   */
  refreshQuietly: () => void;
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
function useResource<T>(
  path: string,
  enabled: boolean,
  reloadKey: number,
  fetcher?: (signal: AbortSignal) => Promise<T>,
): Async<T> {
  // One object, so a settled request cannot land as three separate renders --
  // and, more to the point, so `loading` and `data` can never disagree about
  // which request they came from.
  const [state, setState] = useState<{ data: T | null; loading: boolean; error: string | null }>({
    data: null,
    loading: true,
    error: null,
  });
  const [nonce, setNonce] = useState(0);

  /**
   * A REFRESH ASKED FOR MID-FLIGHT IS REMEMBERED, NOT FIRED, AND NOT DROPPED.
   *
   * Pressing Refresh five times quickly used to start five requests. Each one
   * aborted the last, so four were wasted -- but they were still SENT, and the
   * server still had to accept, authenticate and begin serving every one of
   * them. On the Analytics screen, where one press fans out to seven endpoints,
   * five presses is thirty-five requests arriving at once. That burst is what
   * exhausted the API's database connection pool and turned ordinary reads into
   * 500s, and a 500 on /api/auth/me is what used to sign the user out (see
   * lib/auth.tsx).
   *
   * Two obvious fixes are both wrong. Ignoring a refresh while one is in flight
   * DROPS it -- and a refresh is not always a button: `page.tsx` calls it when a
   * run finishes and when a review lands, so discarding one leaves genuinely
   * stale rows on screen with nothing to trigger another. Debouncing on a timer
   * guesses at a delay and still fires while the last request is running.
   *
   * So a refresh requested while one is in flight sets a flag, and the settling
   * request fires exactly one more. However many times the button is pressed,
   * at most one request is queued behind the current one, and the last press
   * always results in a fetch that started after it. Bursts collapse; nothing
   * is lost.
   */
  const inFlight = useRef(false);
  const queued = useRef(false);
  /** Set by `refreshQuietly`, read and cleared by the run it belongs to. A ref
   *  rather than state because it must not itself cause a render, and because
   *  the effect below is the only reader. */
  const quiet = useRef(false);

  useEffect(() => {
    if (!enabled) return;

    const isQuiet = quiet.current;
    quiet.current = false;

    const controller = new AbortController();
    inFlight.current = true;
    // Keep previous rows on screen while refetching; blanking a populated table
    // to skeletons on every refresh is disorienting. `loading` still goes true,
    // so a caller that wants to show a quiet refreshing state can.
    //
    // A QUIET refresh with rows already on screen touches nothing at all --
    // not even to set `loading` -- so a poll is invisible until its data
    // lands. With no rows yet it falls through to the ordinary path, because
    // the very first load does need to say it is loading. Returning `s`
    // unchanged is what makes that free: React bails out of the render rather
    // than re-running every consumer of this hook on a timer.
    setState((s) =>
      isQuiet && s.data !== null ? s : { data: s.data, loading: true, error: null }
    );

    const settle = (next: (s: { data: T | null; loading: boolean; error: string | null }) =>
                    { data: T | null; loading: boolean; error: string | null }) => {
      inFlight.current = false;
      setState(next);
      if (queued.current) {
        queued.current = false;
        setNonce((n) => n + 1);
      }
    };

    (fetcher ? fetcher(controller.signal) : apiJson<T>(path, { signal: controller.signal }))
      .then((d) => settle(() => ({ data: d, loading: false, error: null })))
      .catch((e) => {
        // An abort is this hook superseding itself, not a failure. Reporting it
        // would put an error on screen for a request nobody is waiting for, and
        // the run that replaced it is already responsible for settling state --
        // including for anything queued behind it, which is why the flags are
        // left exactly as they are here.
        if (controller.signal.aborted) return;
        settle((s) => ({ data: s.data, loading: false, error: describe(e) }));
      });

    return () => controller.abort();
  }, [path, enabled, reloadKey, nonce]);

  return {
    data: state.data,
    // Before it is enabled the resource is not idle — it is waiting on auth, and
    // reporting "loaded, empty" there would flash an empty state at sign-in.
    loading: !enabled || state.loading,
    error: state.error,
    refresh: useCallback(() => {
      if (inFlight.current) {
        queued.current = true;
        return;
      }
      setNonce((n) => n + 1);
    }, []),
    refreshQuietly: useCallback(() => {
      // A poll that arrives mid-flight is DROPPED rather than queued. That is
      // the opposite of `refresh`'s rule, and deliberately so: a queued poll
      // would fire a second request the moment the first settled, for data
      // that is by then already current, and the next tick is only seconds
      // away regardless. Nothing is lost by skipping one.
      if (inFlight.current) return;
      quiet.current = true;
      setNonce((n) => n + 1);
    }, []),
  };
}

/**
 * Keep a resource current while the user is watching it.
 *
 * WHY THIS IS OPT-IN AND NOT THE DEFAULT. This module's own rule is that
 * `refresh` is exposed rather than polled, "because a row changing under the
 * cursor mid-review is worse than a slightly stale list" -- and that is still
 * right for the Invoices table and the review queue, where the row under the
 * pointer is the one being acted on.
 *
 * A dashboard is the other case. Overview exists to say what is happening
 * right now, and since invoices can arrive by email and process themselves
 * with no one touching the browser, "right now" cannot be established by
 * anything the user does. So polling is offered to the screens where a row
 * appearing is the point, and withheld from the ones where it is a hazard.
 *
 * Two things keep the cost honest: nothing is requested while the tab is in
 * the background, and a tab coming back to the foreground refreshes at once
 * instead of waiting out the rest of its interval -- which is the case that
 * actually matters here, since sending an invoice means leaving this tab for
 * a mail client and returning.
 */
export function useLiveRefresh(refreshQuietly: () => void, active: boolean, everyMs = 15000) {
  useEffect(() => {
    if (!active || typeof document === "undefined") return;

    const refreshIfWatching = () => {
      if (document.visibilityState === "visible") refreshQuietly();
    };
    const id = window.setInterval(refreshIfWatching, everyMs);
    document.addEventListener("visibilitychange", refreshIfWatching);
    return () => {
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", refreshIfWatching);
    };
  }, [refreshQuietly, active, everyMs]);
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

/**
 * THE ANALYTICS SCREEN, IN ONE REQUEST -- WITH A FALLBACK TO THE SEVEN.
 *
 * `/api/analytics/dashboard` returns all seven sections from one pass, which is
 * what took the screen from about thirteen seconds to about five. The fallback
 * exists because of a fact about how this application is DEPLOYED, not because
 * the endpoint is unreliable: the two halves ship separately and one of them
 * lags (see CLAUDE.md §2 -- Vercel auto-deploys on a push to main, Railway does
 * not). So there is a window in which this bundle is live and the API serving
 * it has not been redeployed yet and answers 404.
 *
 * Without the fallback that window is a BROKEN Analytics screen. With it, the
 * screen works exactly as it did before, at exactly its old speed, until the
 * API catches up -- and then silently gets fast. Only a 404 triggers it: a 401
 * ends the session, a 429 is the rate limiter, and a 500 is a real failure the
 * user needs to see rather than have papered over by seven more requests.
 *
 * This is deliberately temporary in spirit but permanent in code -- the same
 * window reopens on every future backend change, so removing it once the API
 * is redeployed would just reintroduce the problem next time.
 */
export function useAnalyticsDashboard(
  range: RangeKey,
  enabled = true,
  reloadKey = 0
): Async<AnalyticsDashboard> {
  return useResource<AnalyticsDashboard>(
    analyticsPath("dashboard", range),
    enabled,
    reloadKey,
    async (signal) => {
      try {
        return await apiJson<AnalyticsDashboard>(
          analyticsPath("dashboard", range), { signal });
      } catch (e) {
        if (!(e instanceof ApiError) || e.status !== 404) throw e;
        // The API predates the combined endpoint. Ask the way we used to.
        const [overview, trends, processing, reviews, vendors, users, email] =
          await Promise.all(([
            "overview", "trends", "processing", "reviews", "vendors", "users", "email",
          ] as const).map((name) =>
            apiJson<never>(analyticsPath(name, range), { signal })));
        return {
          range: (overview as { range: AnalyticsDashboard["range"] }).range,
          generated_at: new Date().toISOString(),
          overview, trends, processing, reviews, vendors, users, email,
        } as AnalyticsDashboard;
      }
    },
  );
}

export const useAnalytics = <T,>(
  name: string,
  range: RangeKey,
  enabled = true,
  reloadKey = 0
) => useResource<T>(analyticsPath(name, range), enabled, reloadKey);
