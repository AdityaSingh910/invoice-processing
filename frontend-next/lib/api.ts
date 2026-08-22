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
 * All paths are RELATIVE. In production the static export is served by FastAPI
 * itself, so they are same-origin; in dev, next.config.mjs proxies /api to the
 * backend. Either way there is no base URL to get wrong.
 */
import type { Identity, PortalSubmission, RunEvent } from "./types";

export const TOKEN_KEY = "ip.token";

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
  const res = await fetch(path, { ...opts, headers });
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
    res = await fetch("/api/auth/token", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body,
    });
  } catch {
    // fetch only rejects on a transport failure -- the API is unreachable.
    throw new Error(
      "Could not reach the server. Check that the backend is running on port 8000."
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
      "Reached a server, but not the API. This page is being served from the " +
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
