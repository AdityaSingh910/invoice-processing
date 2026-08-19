/**
 * Two serving modes, one relative-path contract.
 *
 * PRODUCTION  `next build` emits a static export into `out/`, which the existing
 *             FastAPI server mounts at `/`. The UI is then same-origin with the
 *             API, so `fetch("/api/...")` resolves exactly as the vanilla
 *             frontend did -- no CORS, no base URL, one process, one port.
 *
 * DEVELOPMENT `next dev` runs on :3000, so `/api/*` is proxied to the FastAPI
 *             server on :8000. Same relative paths, same code. This is what
 *             stops the "served from the wrong port, every call 404s" failure
 *             that a static file server produces.
 *
 * The app is a client-rendered SPA behind a login gate, so nothing here is
 * server-rendered and static export costs no functionality.
 */
const isDev = process.env.NODE_ENV === "development";

const API_ORIGIN = process.env.API_ORIGIN || "http://127.0.0.1:8000";

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  images: { unoptimized: true },
  // Emitted only for a production build; `output: export` and `rewrites` are
  // mutually exclusive, and dev is the only mode that needs the proxy.
  ...(isDev
    ? {
        async rewrites() {
          return [{ source: "/api/:path*", destination: `${API_ORIGIN}/api/:path*` }];
        },
      }
    : { output: "export" }),
};

export default nextConfig;
