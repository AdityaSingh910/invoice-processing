/**
 * Client side of the API boundary.
 *
 * The server is the security boundary; this module is a convenience for the
 * human using the app. Every call carries the bearer token the server issued
 * for THIS user, and the server re-checks the scope on every request -- so
 * hiding a button in the UI spares someone a 403, it never grants anything.
 *
 * Nothing secret is stored here. The token comes from the user signing in with
 * their own credentials and lives in sessionStorage, so it dies with the tab
 * rather than persisting on a shared machine.
 *
 * WHERE THE API IS.
 *
 * Every call site in this app writes a RELATIVE path (`/api/...`), and that is
 * deliberate -- there is exactly one place, `apiUrl` below, that decides which
 * origin those resolve against, so a deployment change is one environment
 * variable rather than an edit to forty components.
 *
 *   same-origin (default)   FastAPI serves the static export itself, so
 *                           `/api/...` already points at the API. This is the
 *                           local demo and any single-process deployment.
 *   `next dev`              next.config.mjs proxies /api to the backend on
 *                           :8000, so relative paths still work unchanged.
 *   split deployment        the UI is on a static host and the API is
 *                           elsewhere. NEXT_PUBLIC_API_BASE_URL names that
 *                           origin and every path is prefixed with it.
 *
 * NEXT_PUBLIC_* is compiled into the browser bundle by design, so this value
 * must be a public origin and NOTHING ELSE. No key, no token, no secret is
 * read here or anywhere else on this side of the boundary.
 */
import { storedLocale } from "./i18n";
import type { Identity, PortalSubmission, ProcessingJob, RunEvent } from "./types";

export const TOKEN_KEY = "ip.token";

/**
 * The API origin, or "" for same-origin.
 *
 * Read once, at module load. `process.env.NEXT_PUBLIC_*` is substituted at
 * BUILD time by Next, so this is a literal in the emitted bundle rather than a
 * lookup -- which is also why changing it means rebuilding, not restarting.
 *
 * A trailing slash is stripped so `${API_BASE}${path}` never produces `//api`,
 * which some proxies and some routers treat as a different path entirely.
 */
export const API_BASE = (process.env.NEXT_PUBLIC_API_BASE_URL || "").replace(/\/+$/, "");

/** A relative API path resolved against wherever the API actually is. */
export function apiUrl(path: string): string {
  return API_BASE ? `${API_BASE}${path}` : path;
}

/**
 * Attach the reader's chosen language to a request (Phase L).
 *
 * `Accept-Language` is a forbidden header name -- `fetch` may not set it -- so
 * an explicit choice travels as `?lang=`, which is the parameter the server
 * gives precedence to. With no stored choice nothing is appended and the
 * browser's own Accept-Language decides, which is the right default for
 * someone who has never opened the picker.
 *
 * A locale never widens or narrows what comes back. The server resolves the
 * caller from the bearer token and filters in SQL before a row is read; this
 * only selects which words the sentences are written in.
 */
function withLocale(path: string): string {
  const tag = storedLocale();
  if (!tag) return path;
  const sep = path.includes("?") ? "&" : "?";
  return `${path}${sep}lang=${encodeURIComponent(tag)}`;
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

export function readToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function writeToken(token: string | null) {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* private mode; the session simply will not survive a reload */
  }
}

/** Raw fetch with the bearer token attached. Callers handle the status.
 *
 *  A 401 anywhere means this session is finished, so it is announced once here
 *  rather than handled at each of the dozen call sites. AuthProvider listens. */
export async function apiFetch(path: string, opts: RequestInit = {}): Promise<Response> {
  const token = readToken();
  const headers = new Headers(opts.headers || {});
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch(apiUrl(withLocale(path)), { ...opts, headers });
  if (res.status === 401) {
    writeToken(null);
    window.dispatchEvent(new Event("ip:unauthenticated"));
  }
  return res;
}

/** JSON GET/POST that throws ApiError on a non-2xx, so callers can branch on
 *  status rather than on a stringly-typed message. */
export async function apiJson<T>(path: string, opts: RequestInit = {}): Promise<T> {
  const res = await apiFetch(path, opts);
  if (!res.ok) throw new ApiError(`request failed (${res.status})`, res.status);
  return (await res.json()) as T;
}

/**
 * OAuth 2.0 password grant: form-encoded, as the spec requires.
 *
 * The error messages below are deliberately specific. The previous UI reported
 * *every* non-ok status as "Incorrect username or password", which blamed the
 * one thing that was fine when the real fault was the page being served from a
 * static file server that has no /api route at all. A wrong diagnosis in the
 * sign-in box is expensive: it sends you off resetting a password that works.
 */
export async function signIn(username: string, password: string): Promise<string> {
  const body = new URLSearchParams({ username, password });

  let res: Response;
  try {
    res = await fetch(apiUrl("/api/auth/token"), {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body,
    });
  } catch {
    // fetch only rejects on a transport failure -- the API is unreachable, or a
    // cross-origin request was refused before a response existed. Name the
    // origin actually being called, because "the backend" is ambiguous the
    // moment the UI and the API are not the same host, and a CORS refusal looks
    // identical to an outage from here.
    throw new Error(
      API_BASE
        ? `Could not reach the API at ${API_BASE}. It may be unreachable, or it ` +
          `may not list this site in CORS_ORIGINS.`
        : "Could not reach the server. Check that the backend is running on port 8000."
    );
  }

  if (res.ok) {
    const data = (await res.json()) as { access_token: string };
    writeToken(data.access_token);
    return data.access_token;
  }

  if (res.status === 401) throw new Error("Incorrect username or password.");
  if (res.status === 429) throw new Error("Too many attempts. Wait a moment and try again.");
  if (res.status === 404 || res.status === 405) {
    throw new Error(
      API_BASE
        ? `Reached ${API_BASE}, but not the API. Check NEXT_PUBLIC_API_BASE_URL ` +
          `points at the API host itself, with no path.`
        : "Reached a server, but not the API. This page is being served from the " +
          "wrong origin — open the app at http://127.0.0.1:8000 instead."
    );
  }
  throw new Error(`Sign-in failed (HTTP ${res.status}).`);
}

export async function loadIdentity(): Promise<Identity> {
  return apiJson<Identity>("/api/auth/me");
}

/**
 * Drive the pipeline and surface each stage as it lands.
 *
 * NO LONGER USED BY THIS UI. The Process screen went to `startRun` + `fetchJob`
 * below, because the pipeline running inside this response body is exactly why
 * a refresh used to kill it. Kept because `POST /api/runs/stream` is unchanged
 * and remains a working API for a caller that does hold the connection open --
 * removing it would be a breaking change the refresh fix does not need to make.
 *
 * This is a POST carrying a file, so it cannot use EventSource; the SSE frames
 * are read off the fetch body stream by hand. Frames are separated by a blank
 * line and may be split across chunk boundaries, so the tail of the buffer is
 * carried forward rather than parsed.
 */
export async function streamRun(
  file: File,
  onEvent: (evt: RunEvent) => void,
  signal?: AbortSignal
): Promise<void> {
  const fd = new FormData();
  fd.append("file", file);

  const res = await apiFetch("/api/runs/stream", { method: "POST", body: fd, signal });
  if (!res.ok) throw new ApiError(`the run could not be started (HTTP ${res.status})`, res.status);
  if (!res.body) throw new Error("this browser cannot read a streaming response");

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });

    const frames = buf.split("\n\n");
    buf = frames.pop() ?? "";        // incomplete tail stays buffered

    for (const frame of frames) {
      const line = frame.split("\n").find((l) => l.startsWith("data: "));
      if (!line) continue;
      try {
        onEvent(JSON.parse(line.slice(6)) as RunEvent);
      } catch {
        /* a malformed frame must not kill the rest of the run */
      }
    }
  }
}

/* ------------------------------------------------- background processing */

/**
 * Hand an invoice to the server and get back the job that will read it.
 *
 * WHY THIS REPLACED `streamRun` ON THIS SCREEN.
 *
 * `streamRun` reads the pipeline out of a response body, which means the
 * pipeline only advances while this fetch is alive. Refreshing the page aborts
 * the fetch; the server cancels the response task and the pipeline with it,
 * part-way through, before the one point at which a run is written. The upload
 * did not fail -- it stopped existing, which is why nothing was there after
 * the reload.
 *
 * This call returns as soon as the file has been accepted and recorded. The
 * reading happens on the server, in its own worker, and `fetchJob` below asks
 * how it is going. Nothing on this side keeps it alive, so nothing on this
 * side can kill it.
 *
 * DELIBERATELY NOT ABORTABLE. Every other request in this app carries an
 * AbortSignal so a superseded one stops being a request; this one must not,
 * because aborting it is precisely the bug. It is a short POST either way.
 */
export async function startRun(file: File): Promise<ProcessingJob> {
  const fd = new FormData();
  fd.append("file", file);
  const res = await apiFetch("/api/runs", { method: "POST", body: fd });
  if (!res.ok) {
    // The server's own message is worth showing: a rate-limit refusal or a
    // rejected file type tells the uploader something they can act on.
    let detail = "";
    try {
      detail = ((await res.json()) as { detail?: string }).detail || "";
    } catch {
      /* a non-JSON body is not worth a second failure */
    }
    throw new ApiError(detail || `the invoice could not be accepted (HTTP ${res.status})`,
                       res.status);
  }
  return (await res.json()) as ProcessingJob;
}

/** One job's current state, straight from the database. */
export async function fetchJob(jobId: string, signal?: AbortSignal): Promise<ProcessingJob> {
  return apiJson<ProcessingJob>(`/api/jobs/${encodeURIComponent(jobId)}`, { signal });
}

/** This user's uploads that are still queued or running.
 *
 *  What a page asks on mount when it has no memory of its own -- a new tab, or
 *  one reopened after the browser was closed. */
export async function fetchActiveJobs(signal?: AbortSignal): Promise<ProcessingJob[]> {
  return apiJson<ProcessingJob[]>("/api/jobs?active=1&mine=1", { signal });
}

/* ------------------------------------------------------- client portal (J) */

/**
 * Submit an invoice as an external client.
 *
 * Deliberately NOT `streamRun`. The internal upload streams SSE stage frames
 * so an employee can watch the pipeline work; those frames name internal
 * stages and carry their detail lines, so the portal endpoint returns a
 * finished result instead. There is nothing to stream here, which is why this
 * is an ordinary POST.
 */
export async function submitPortalInvoice(file: File): Promise<PortalSubmission> {
  const fd = new FormData();
  fd.append("file", file);
  const res = await apiFetch("/api/portal/invoices", { method: "POST", body: fd });
  if (!res.ok) {
    // The server's own message is worth showing here: a daily-limit or
    // rate-limit refusal tells the supplier something they can act on, and a
    // generic "upload failed" would send them retrying into the same wall.
    let detail = "";
    try {
      detail = ((await res.json()) as { detail?: string }).detail || "";
    } catch {
      /* a non-JSON body is not worth a second failure */
    }
    throw new ApiError(detail || `the invoice could not be submitted (HTTP ${res.status})`,
                       res.status);
  }
  return (await res.json()) as PortalSubmission;
}

/**
 * The stored PDF, fetched WITH the bearer token and handed to the browser as a
 * blob URL.
 *
 * The token travels in a header rather than in a URL, which is the same reason
 * the internal document preview works this way: a URL ends up in history, in
 * the Referer and in any log in front of the app, and a bearer token has no
 * business in any of them.
 */
export async function portalDocumentUrl(invoiceId: number): Promise<string> {
  const res = await apiFetch(`/api/portal/invoices/${invoiceId}/document/download?inline=1`);
  if (!res.ok) throw new ApiError(`document unavailable (HTTP ${res.status})`, res.status);
  return URL.createObjectURL(await res.blob());
}

/* --------------------------------------------------------- audit export */

/**
 * Fetch a file WITH the bearer token (same reason as `portalDocumentUrl` --
 * a token belongs in a header, never in a URL) and hand it to the browser as
 * an actual save, not a new tab. A momentary `<a download>` click is the only
 * way a `fetch`ed blob becomes a real download; the server already names the
 * file safely via `Content-Disposition`, but a caller-supplied `fallbackName`
 * covers the rare case that header is missing.
 */
export async function downloadFile(path: string, fallbackName: string): Promise<void> {
  const res = await apiFetch(path);
  if (!res.ok) {
    let detail = "";
    try {
      detail = ((await res.json()) as { detail?: string }).detail || "";
    } catch {
      /* not JSON; fall through to the generic message */
    }
    throw new ApiError(detail || `the file could not be downloaded (HTTP ${res.status})`,
                       res.status);
  }
  const disposition = res.headers.get("Content-Disposition") || "";
  const match = /filename="?([^";]+)"?/.exec(disposition);
  const filename = match?.[1] || fallbackName;
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
